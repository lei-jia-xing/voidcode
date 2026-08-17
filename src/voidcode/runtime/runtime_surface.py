"""Narrow ``RuntimeSurface`` protocol implemented by :class:`VoidCodeRuntime`.

The three runtime collaborators (``RuntimeBackgroundTaskSupervisor``,
``RuntimeRunLoopCoordinator``, ``RuntimeResumeCoordinator``) no longer pierce
``runtime._xxx`` private members. Pure data dependencies are constructor-injected
(session store / workspace / config / peers); what remains are governance,
config-composition, and runtime-state calls that must stay owned by the runtime
(see ``docs/collaborator-contract-design.md`` §3 / §5 Phase 4).

Every method here maps 1:1 to a public method on ``VoidCodeRuntime`` (renamed
from its private ``_xxx`` form); the bodies are unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from ..graph.contracts import GraphRunRequest, RuntimeGraph
from ..provider.protocol import ProviderAbortSignal
from ..skills.registry import SkillRegistry
from ..tools.contracts import Tool, ToolCall, ToolDefinition, ToolResult
from .config import RuntimeAgentConfig
from .config_materializer import EffectiveRuntimeConfig
from .context_window import (
    ContextWindowPolicy,
    RuntimeAssembledContext,
    RuntimeContextSegment,
    RuntimeContextWindow,
)
from .contracts import (
    RuntimeProviderContextPolicyDecision,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeStreamChunk,
)
from .permission import PendingApproval, PermissionPolicy, PermissionResolution
from .session import SessionState
from .skills import SkillExecutionSnapshot
from .tool_registry import ToolPolicyDecision, ToolRegistry


@dataclass(frozen=True, slots=True)
class PermissionOutcome:
    """Streaming outcome of a permission / approval resolution.

    Promoted from ``service._PermissionOutcome`` (Phase 4). Distinct from
    ``permission.PermissionOutcome`` (the policy-level decision).
    """

    chunks: tuple[RuntimeStreamChunk, ...]
    last_sequence: int
    pending_approval: PendingApproval | None = None
    denied: bool = False
    denied_approval: PendingApproval | None = None


class RuntimeSurface(Protocol):
    # --- config truth (reads _config / registries / graph-override state) ---
    def effective_runtime_config_from_metadata(self, metadata: dict[str, object] | None) -> EffectiveRuntimeConfig: ...

    def runtime_config_for_request(self, request: RuntimeRequest) -> EffectiveRuntimeConfig: ...

    # --- permission / tool governance (runtime owns uniformly) ---
    def resolve_permission(
        self,
        *,
        session: SessionState,
        tool: ToolDefinition,
        tool_instance: Tool,
        tool_call: ToolCall,
        permission_policy: PermissionPolicy,
        sequence: int,
    ) -> PermissionOutcome: ...

    def approval_resolution_outcome(
        self,
        *,
        session: SessionState,
        pending: PendingApproval,
        decision: PermissionResolution,
        sequence: int,
    ) -> PermissionOutcome: ...

    def tool_policy_denial(
        self,
        *,
        session: SessionState,
        tool_name: str,
    ) -> ToolPolicyDecision | None: ...

    def delegation_tool_policy_error(
        self,
        *,
        session: SessionState,
        tool_name: str,
    ) -> str | None: ...

    # --- provider context assembly orchestration (service keeps composition) ---
    def prepare_provider_context_window(
        self,
        *,
        prompt: str,
        tool_results: tuple[ToolResult, ...],
        session_metadata: dict[str, object],
        policy: ContextWindowPolicy | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> RuntimeContextWindow: ...

    def assemble_provider_context(
        self,
        *,
        prompt: str,
        tool_results: tuple[ToolResult, ...],
        session_metadata: dict[str, object],
        skill_prompt_context: str = "",
        preserved_system_segments: tuple[str, ...] = (),
        replayed_conversation_segments: tuple[RuntimeContextSegment, ...] = (),
    ) -> RuntimeAssembledContext: ...

    # --- tool / skill registry composition (resume-oriented) ---
    def tool_registry_for_effective_config(
        self,
        effective_config: EffectiveRuntimeConfig,
        metadata: dict[str, object] | None = None,
    ) -> ToolRegistry: ...

    def skill_registry_for_effective_config(self, effective_config: EffectiveRuntimeConfig) -> SkillRegistry: ...

    def build_skill_snapshot(
        self,
        skill_registry: SkillRegistry,
        *,
        metadata: dict[str, object] | None,
        agent: RuntimeAgentConfig | None,
        source: Literal["run", "resume", "replay"],
    ) -> SkillExecutionSnapshot: ...

    def provider_tool_definitions(
        self,
        tool_registry: ToolRegistry,
        effective_config: EffectiveRuntimeConfig,
    ) -> tuple[ToolDefinition, ...]: ...

    # --- graph selection (reads _graph_override / _graph_cache) ---
    def graph_for_session_metadata(self, metadata: dict[str, object] | None) -> RuntimeGraph: ...

    # --- provider context policy decision (runtime-owned config/tool composition) ---
    def provider_context_policy_decision_for_graph_request(
        self,
        *,
        graph_request: GraphRunRequest,
        effective_config: EffectiveRuntimeConfig,
    ) -> RuntimeProviderContextPolicyDecision | None: ...

    # --- MCP lifecycle (resume-oriented; runtime owns refresh / startup gates) ---
    def should_skip_mcp_startup_for_request(
        self,
        *,
        request_metadata: Mapping[str, object],
        effective_config: EffectiveRuntimeConfig,
    ) -> bool: ...

    def refresh_mcp_tools_for_session(
        self,
        *,
        session: SessionState,
        sequence: int,
        failure_kind: str,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int, RuntimeStreamChunk | None]: ...

    def reset_tool_registry_to_base(self) -> None: ...

    # --- run entry / persistence (supervisor-oriented) ---
    def run_with_persistence(
        self,
        request: RuntimeRequest,
        *,
        allow_internal_metadata: bool = False,
    ) -> Iterator[RuntimeStreamChunk]: ...

    def persist_response(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
    ) -> None: ...
