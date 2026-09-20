from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from ...graph.contracts import RuntimeGraph
from ...graph.deterministic_graph import DeterministicGraph
from ...graph.provider_graph import ProviderGraph
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
class RuntimeGraphSelection:
    graph: RuntimeGraph
    provider_attempt: int
    provider_target: ResolvedProviderModel


@dataclass(frozen=True, slots=True)
class RuntimeSessionRouting:
    session_id: str
    parent_session_id: str | None
    requested_session_id: str | None
    allocate_session_id: bool


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
        return RuntimeSessionRouting(
            session_id=requested_session_id,
            parent_session_id=request.parent_session_id,
            requested_session_id=requested_session_id,
            allocate_session_id=request.allocate_session_id,
        )
    if request.allocate_session_id or request.parent_session_id is not None:
        return RuntimeSessionRouting(
            session_id=f"session-{uuid4().hex}",
            parent_session_id=request.parent_session_id,
            requested_session_id=None,
            allocate_session_id=request.allocate_session_id,
        )
    return RuntimeSessionRouting(
        session_id="local-cli-session",
        parent_session_id=request.parent_session_id,
        requested_session_id=None,
        allocate_session_id=request.allocate_session_id,
    )


def build_runtime_graph(
    *,
    engine_name: ExecutionEngineName,
    provider_model: ResolvedProviderModel,
) -> RuntimeGraph:
    if engine_name == "deterministic":
        return DeterministicGraph()
    if provider_model.provider is None:
        raise ValueError(provider_model_required_message())
    return ProviderGraph(
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
    # Deliberately excluded: providers endpoint configs (they reach the graph
    # only through the resolved chain selections below), approval/permission
    # policy, tools, and context window (never read by graph construction, so
    # sharing across them is correct, not a collision).
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


def select_graph_for_effective_config(
    *,
    config: EffectiveRuntimeConfig,
    provider_attempt: int = 0,
    cache: dict[tuple[ExecutionEngineName, str], RuntimeGraph] | None = None,
    force_rebuild: bool = False,
) -> RuntimeGraphSelection:
    provider_target = config.resolved_provider.target_chain.target_at(provider_attempt)
    if provider_target is None:
        provider_target = config.resolved_provider.active_target
        provider_attempt = 0
    # Key on the effective attempt after clamping, so a clamped request can
    # never collide with a genuinely different attempt.
    cache_key = cache_key_for_effective_config(config, provider_attempt=provider_attempt)
    if cache is not None and not force_rebuild and cache_key in cache:
        cached = cache[cache_key]
        return RuntimeGraphSelection(graph=cached, provider_attempt=provider_attempt, provider_target=provider_target)
    selection = RuntimeGraphSelection(
        graph=build_runtime_graph(
            engine_name=config.execution_engine,
            provider_model=provider_target,
        ),
        provider_attempt=provider_attempt,
        provider_target=provider_target,
    )
    if cache is not None and not force_rebuild:
        cache[cache_key] = selection.graph
    return selection


def fallback_graph_for_provider_error(
    *,
    error: ProviderExecutionError,
    provider_chain: ResolvedProviderChain,
    config: EffectiveRuntimeConfig,
    provider_attempt: int,
) -> RuntimeGraphSelection | None:
    next_attempt = provider_attempt + 1
    next_target = provider_chain.target_at(next_attempt)
    if not fallback_allowed(error):
        return None
    if next_target is None:
        return None
    return RuntimeGraphSelection(
        graph=build_runtime_graph(
            engine_name=config.execution_engine,
            provider_model=next_target,
        ),
        provider_attempt=next_attempt,
        provider_target=next_target,
    )
