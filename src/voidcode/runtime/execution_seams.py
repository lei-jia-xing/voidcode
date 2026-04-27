from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from ..graph.contracts import RuntimeGraph
from ..graph.deterministic_graph import DeterministicGraph
from ..graph.provider_graph import ProviderGraph
from ..provider.errors import ProviderExecutionError
from ..provider.models import ResolvedProviderChain, ResolvedProviderModel
from .config import ExecutionEngineName, serialize_runtime_agent_config

if TYPE_CHECKING:
    from .contracts import RuntimeRequest
    from .service import EffectiveRuntimeConfig


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
    max_steps: int | None,
) -> RuntimeGraph:
    if engine_name == "deterministic":
        return DeterministicGraph(max_steps=max_steps or 4)
    if provider_model.provider is None:
        raise ValueError(
            "provider execution requires a configured provider/model. "
            "Run 'voidcode config init' and set model to 'provider/model' (or set VOIDCODE_MODEL), "
            "or explicitly use execution_engine='deterministic' for test/dev workflows."
        )
    return ProviderGraph(
        provider=provider_model.provider.turn_provider(),
        provider_model=provider_model,
        max_steps=max_steps,
    )


def cache_key_for_effective_config(
    config: EffectiveRuntimeConfig,
) -> tuple[ExecutionEngineName, str]:
    model_str = config.model if config.model is not None else ""
    agent_payload = serialize_runtime_agent_config(config.agent)
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
    return (
        config.execution_engine,
        f"{model_str}::{provider_fallback_key}::{config.max_steps}::{agent_key}",
    )


def select_graph_for_effective_config(
    *,
    config: EffectiveRuntimeConfig,
    provider_attempt: int = 0,
) -> RuntimeGraphSelection:
    provider_target = config.resolved_provider.target_chain.target_at(provider_attempt)
    if provider_target is None:
        provider_target = config.resolved_provider.active_target
        provider_attempt = 0
    return RuntimeGraphSelection(
        graph=build_runtime_graph(
            engine_name=config.execution_engine,
            provider_model=provider_target,
            max_steps=config.max_steps,
        ),
        provider_attempt=provider_attempt,
        provider_target=provider_target,
    )


def fallback_graph_for_provider_error(
    *,
    error: ProviderExecutionError,
    provider_chain: ResolvedProviderChain,
    config: EffectiveRuntimeConfig,
    provider_attempt: int,
) -> RuntimeGraphSelection | None:
    next_attempt = provider_attempt + 1
    next_target = provider_chain.target_at(next_attempt)
    if error.kind not in {"rate_limit", "invalid_model", "transient_failure"}:
        return None
    if next_target is None:
        return None
    return RuntimeGraphSelection(
        graph=build_runtime_graph(
            engine_name=config.execution_engine,
            provider_model=next_target,
            max_steps=config.max_steps,
        ),
        provider_attempt=next_attempt,
        provider_target=next_target,
    )
