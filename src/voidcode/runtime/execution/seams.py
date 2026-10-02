from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from ...core.deterministic_turns import DeterministicTurnProducer
from ...core.provider_turns import ProviderTurnProducer
from ...core.turns import TurnProducer
from ...provider.errors import ProviderExecutionError
from ...provider.models import ResolvedProviderChain, ResolvedProviderModel
from ..config import (
    MODEL_ENV_VAR,
    RUNTIME_CONFIG_FILE_NAME,
    ExecutionEngineName,
    serialize_runtime_agent_config,
)
from .provider_fallback import fallback_allowed

if TYPE_CHECKING:
    from ..config_materializer import EffectiveRuntimeConfig
    from ..contracts import RuntimeRequest


@dataclass(frozen=True, slots=True)
class RuntimeTurnProducerSelection:
    producer: TurnProducer
    provider_attempt: int
    provider_target: ResolvedProviderModel


@dataclass(frozen=True, slots=True)
class RuntimeSessionRouting:
    session_id: str


def provider_model_required_message() -> str:
    return (
        "provider execution requires a configured provider/model. "
        f'Set "model": "<provider>/<model>" in {RUNTIME_CONFIG_FILE_NAME} (or {MODEL_ENV_VAR}), '
        "for example \"openai/gpt-4o\", or run 'voidcode config init --model <provider>/<model>'. "
        "For test/dev workflows without a provider, use the deterministic test harness env var."
    )


def resolve_runtime_session_routing(request: RuntimeRequest) -> RuntimeSessionRouting:
    requested_session_id = request.session_id
    if requested_session_id is not None:
        return RuntimeSessionRouting(session_id=requested_session_id)
    if request.allocate_session_id or request.parent_session_id is not None:
        return RuntimeSessionRouting(session_id=f"session-{uuid4().hex}")
    return RuntimeSessionRouting(session_id="local-cli-session")


def build_runtime_turn_producer(
    *,
    engine_name: ExecutionEngineName,
    provider_model: ResolvedProviderModel,
) -> TurnProducer:
    if engine_name == "deterministic":
        return DeterministicTurnProducer()
    if provider_model.provider is None:
        raise ValueError(provider_model_required_message())
    return ProviderTurnProducer(
        provider=provider_model.provider.turn_provider(),
        provider_model=provider_model,
    )


def cache_key_for_effective_config(
    config: EffectiveRuntimeConfig,
    *,
    provider_attempt: int = 0,
) -> tuple[ExecutionEngineName, str]:
    # Key must cover every config field that selection/build reads: engine,
    # model identity, fallback chain, resolved chain selections, and agent.
    # Deliberately excluded: provider endpoint configs (they reach producer selection
    # only through the resolved chain selections below), approval/permission policy,
    # tools, and context window (never read by producer selection, so sharing across
    # them is correct, not a collision).
    agent_payload = serialize_runtime_agent_config(config.agent, include_runtime_internal=True)
    agent_key = "" if agent_payload is None else str(sorted(agent_payload.items()))
    provider_fallback_key = (
        ""
        if config.provider_fallback is None
        else "|".join(
            (
                config.provider_fallback.preferred_model,
                *config.provider_fallback.fallback_models,
            )
        )
    )
    chain_key = "|".join(
        f"{target.selection.provider}/{target.selection.model}/{target.selection.raw_model}"
        for target in config.resolved_provider.target_chain.all_targets
    )
    model_key = config.model or ""
    reasoning_key = config.reasoning_effort or ""
    return (config.execution_engine, f"{provider_attempt}::{model_key}::{reasoning_key}::{provider_fallback_key}::{chain_key}::{agent_key}")


def select_turn_producer_for_effective_config(
    *,
    config: EffectiveRuntimeConfig,
    provider_attempt: int = 0,
    cache: dict[tuple[ExecutionEngineName, str], TurnProducer] | None = None,
    force_rebuild: bool = False,
) -> RuntimeTurnProducerSelection:
    provider_target = config.resolved_provider.target_chain.target_at(provider_attempt)
    if provider_target is None:
        provider_target = config.resolved_provider.active_target
        provider_attempt = 0
    # Key on the effective attempt after clamping, so a clamped request can
    # never collide with a genuinely different attempt.
    cache_key = cache_key_for_effective_config(config, provider_attempt=provider_attempt)
    if cache is not None and not force_rebuild and cache_key in cache:
        cached = cache[cache_key]
        return RuntimeTurnProducerSelection(producer=cached, provider_attempt=provider_attempt, provider_target=provider_target)
    selection = RuntimeTurnProducerSelection(
        producer=build_runtime_turn_producer(
            engine_name=config.execution_engine,
            provider_model=provider_target,
        ),
        provider_attempt=provider_attempt,
        provider_target=provider_target,
    )
    if cache is not None and not force_rebuild:
        cache[cache_key] = selection.producer
    return selection


def provider_target_window_tokens(target: ResolvedProviderModel | None) -> int | None:
    """Effective input window of one resolved provider target.

    Reads the catalog's ``max_input_tokens``, which ``ProviderModelMetadata``
    already derives from ``context_window - max_output_tokens`` when upstream
    carries no explicit input limit. ``None`` means the catalog does not describe
    the model, so two targets cannot be compared.
    """
    metadata = None if target is None else target.metadata
    if metadata is None:
        return None
    return metadata.max_input_tokens or metadata.context_window


@dataclass(frozen=True, slots=True)
class ContextLimitPromotion:
    """Window-aware promotion choice for one ``context_limit`` turn."""

    selection: RuntimeTurnProducerSelection | None
    window_tokens_before: int | None
    window_tokens_after: int | None
    candidate_count: int
    #: ``larger_window`` (a strictly larger candidate was picked),
    #: ``no_larger_candidate`` (none existed; the generic chain-order target is
    #: kept) or ``unavailable`` (fallback not permitted / chain exhausted).
    promotion_reason: Literal["larger_window", "no_larger_candidate", "unavailable"]


def context_limit_promotion_for_provider_error(
    *,
    error: ProviderExecutionError,
    provider_chain: ResolvedProviderChain,
    config: EffectiveRuntimeConfig,
    provider_attempt: int,
) -> ContextLimitPromotion:
    """Choose the escalation target for a ``context_limit`` failure, window-aware.

    Candidates are exactly the chain entries the generic fallback considers, in
    chain order; the first candidate whose effective catalog window is *strictly*
    larger than the failing target's wins. A same-or-smaller window is never
    selected just because it comes first in the chain. With no strictly larger
    candidate the generic chain-order selection is returned unchanged (and the
    event says so), so every other provider error keeps its existing ordering.
    """
    before = provider_target_window_tokens(provider_chain.target_at(provider_attempt))
    next_attempt = provider_attempt + 1
    candidate_count = 0
    chosen_attempt: int | None = None
    for index in range(next_attempt, len(provider_chain.all_targets)):
        target = provider_chain.target_at(index)
        if target is None:
            break
        candidate_count += 1
        window = provider_target_window_tokens(target)
        if before is not None and window is not None and window > before:
            chosen_attempt = index
            break
    if chosen_attempt is not None:
        target = provider_chain.target_at(chosen_attempt)
        assert target is not None
        return ContextLimitPromotion(
            selection=RuntimeTurnProducerSelection(
                producer=build_runtime_turn_producer(engine_name=config.execution_engine, provider_model=target),
                provider_attempt=chosen_attempt,
                provider_target=target,
            ),
            window_tokens_before=before,
            window_tokens_after=provider_target_window_tokens(target),
            candidate_count=candidate_count,
            promotion_reason="larger_window",
        )
    selection = fallback_turn_producer_for_provider_error(
        error=error,
        provider_chain=provider_chain,
        config=config,
        provider_attempt=provider_attempt,
    )
    return ContextLimitPromotion(
        selection=selection,
        window_tokens_before=before,
        window_tokens_after=None if selection is None else provider_target_window_tokens(selection.provider_target),
        candidate_count=candidate_count,
        promotion_reason="no_larger_candidate" if selection is not None else "unavailable",
    )


def fallback_turn_producer_for_provider_error(
    *,
    error: ProviderExecutionError,
    provider_chain: ResolvedProviderChain,
    config: EffectiveRuntimeConfig,
    provider_attempt: int,
) -> RuntimeTurnProducerSelection | None:
    next_attempt = provider_attempt + 1
    next_target = provider_chain.target_at(next_attempt)
    if not fallback_allowed(error):
        return None
    if next_target is None:
        return None
    return RuntimeTurnProducerSelection(
        producer=build_runtime_turn_producer(
            engine_name=config.execution_engine,
            provider_model=next_target,
        ),
        provider_attempt=next_attempt,
        provider_target=next_target,
    )
