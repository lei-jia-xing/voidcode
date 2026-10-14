from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from ...core.provider_turns import ProviderTurnProducer
from ...core.turns import TurnProducer
from ...provider.errors import ProviderExecutionError
from ...provider.models import BoundProviderModel, ResolvedProviderChain, ResolvedProviderModel
from ..config.loading import MODEL_ENV_VAR, RUNTIME_CONFIG_FILE_NAME
from .provider_fallback import fallback_allowed

if TYPE_CHECKING:
    from ..config.materializer import RunConfig
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
        "Configure a provider/model before running; no offline prompt interpreter is available."
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
    provider_model: ResolvedProviderModel,
    bound_provider_model: BoundProviderModel | None = None,
) -> TurnProducer:
    if bound_provider_model is None:
        selection = provider_model.selection
        raise ProviderExecutionError(
            kind="not_configured",
            provider_name=selection.provider or "unknown",
            model_name=selection.model or selection.raw_model or "unknown",
            message=provider_model_required_message(),
            retryable=False,
            fallback_allowed=True,
        )
    return ProviderTurnProducer(
        provider=bound_provider_model.provider.turn_provider(),
        provider_model=provider_model,
    )


def select_turn_producer_for_config(
    *,
    config: RunConfig,
    provider_attempt: int = 0,
) -> RuntimeTurnProducerSelection:
    provider_target = config.resolved_provider.target_chain.target_at(provider_attempt)
    if provider_target is None:
        provider_target = config.resolved_provider.active_target
        provider_attempt = 0
    bound_config = config.bound_provider
    bound_target: BoundProviderModel | None = None
    if bound_config is not None:
        bound_target = bound_config.target_chain.target_at(provider_attempt)
        if bound_target is None or bound_target.selection != provider_target.selection:
            raise ValueError("bound provider target does not match its frozen selection")
    return RuntimeTurnProducerSelection(
        producer=build_runtime_turn_producer(provider_model=provider_target, bound_provider_model=bound_target),
        provider_attempt=provider_attempt,
        provider_target=provider_target,
    )


def provider_target_window_tokens(target: ResolvedProviderModel | None) -> int | None:
    """Return a target's explicitly reported input-token limit, if known."""
    metadata = None if target is None else target.metadata
    return None if metadata is None else metadata.max_input_tokens


@dataclass(frozen=True, slots=True)
class ContextLimitPromotion:
    """Window-aware promotion choice for one ``context_limit`` turn."""

    selection: RuntimeTurnProducerSelection | None
    window_tokens_before: int | None
    window_tokens_after: int | None
    candidate_count: int
    #: ``larger_window`` (a strictly larger candidate was picked),
    #: ``no_known_larger_candidate`` means no candidate had a known input cap
    #: strictly above the active target; unknown caps remain incomparable.
    promotion_reason: Literal["larger_window", "no_known_larger_candidate", "unavailable"]


def context_limit_promotion_for_provider_error(
    *,
    error: ProviderExecutionError,
    provider_chain: ResolvedProviderChain,
    config: RunConfig,
    provider_attempt: int,
) -> ContextLimitPromotion:
    """Choose a context-limit fallback using comparable explicit input caps.

    The first candidate with a known ``max_input_tokens`` strictly above the
    failing target wins. Unknown caps are not compared; if no upgrade is proven,
    the existing chain-order fallback remains available and is labeled as such.
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
        bound_config = config.bound_provider
        bound_target = None if bound_config is None else bound_config.target_chain.target_at(chosen_attempt)
        if bound_config is not None and (bound_target is None or bound_target.selection != target.selection):
            raise ValueError("bound provider target does not match its frozen fallback selection")
        return ContextLimitPromotion(
            selection=RuntimeTurnProducerSelection(
                producer=build_runtime_turn_producer(provider_model=target, bound_provider_model=bound_target),
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
        promotion_reason="no_known_larger_candidate" if selection is not None else "unavailable",
    )


def fallback_turn_producer_for_provider_error(
    *,
    error: ProviderExecutionError,
    provider_chain: ResolvedProviderChain,
    config: RunConfig,
    provider_attempt: int,
) -> RuntimeTurnProducerSelection | None:
    next_attempt = provider_attempt + 1
    next_target = provider_chain.target_at(next_attempt)
    if not fallback_allowed(error):
        return None
    if next_target is None:
        return None
    bound_config = config.bound_provider
    bound_target = None if bound_config is None else bound_config.target_chain.target_at(next_attempt)
    if bound_config is not None and (bound_target is None or bound_target.selection != next_target.selection):
        raise ValueError("bound provider target does not match its frozen fallback selection")
    return RuntimeTurnProducerSelection(
        producer=build_runtime_turn_producer(provider_model=next_target, bound_provider_model=bound_target),
        provider_attempt=next_attempt,
        provider_target=next_target,
    )
