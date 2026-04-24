from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast, final
from uuid import uuid4

from ..acp import AcpDelegatedExecution, AcpEventEnvelope, AcpRequestEnvelope, AcpResponseEnvelope
from ..agent import get_builtin_agent_manifest
from ..graph.contracts import GraphEvent, GraphRunRequest, RuntimeGraph
from ..hook.config import RuntimeHookSurface
from ..hook.executor import (
    HookExecutionOutcome,
    HookExecutionRequest,
    LifecycleHookExecutionRequest,
    run_lifecycle_hooks,
    run_tool_hooks,
)
from ..provider.auth import ProviderAuthResolver
from ..provider.errors import (
    SingleAgentContextLimitError,
    classify_provider_error,
    format_fallback_exhausted_error,
    format_invalid_provider_config_error,
)
from ..provider.models import (
    ResolvedProviderChain,
    ResolvedProviderConfig,
    ResolvedProviderModel,
)
from ..provider.registry import ModelProviderRegistry
from ..provider.resolution import resolve_provider_config
from ..provider.snapshot import (
    parse_resolved_provider_snapshot,
    resolved_provider_snapshot,
)
from ..skills import SkillRegistry
from ..tools.background_cancel import BackgroundCancelTool
from ..tools.background_output import BackgroundOutputTool
from ..tools.contracts import (
    RuntimeTimeoutAwareTool,
    RuntimeToolTimeoutError,
    Tool,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
)
from ..tools.guidance import definition_with_guidance
from ..tools.question import QuestionTool
from ..tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context
from ..tools.skill import SkillTool
from ..tools.task import TaskTool
from .acp import AcpAdapter, AcpAdapterState, build_acp_adapter
from .config import (
    ExecutionEngineName,
    RuntimeAgentConfig,
    RuntimeConfig,
    RuntimeHooksConfig,
    RuntimePlanConfig,
    RuntimeProviderFallbackConfig,
    RuntimeSkillsConfig,
    RuntimeWebSettings,
    load_global_web_settings,
    load_runtime_config,
    parse_provider_fallback_payload,
    parse_runtime_agent_payload,
    parse_runtime_plan_payload,
    save_global_web_settings,
    serialize_provider_fallback_config,
    serialize_runtime_agent_config,
    serialize_runtime_plan_config,
)
from .context_window import (
    ContextWindowPolicy,
    RuntimeContextWindow,
    RuntimeContinuityState,
    prepare_provider_context,
)
from .contracts import (
    BackgroundTaskResult,
    InternalRuntimeRequestMetadata,
    NoPendingQuestionError,
    RuntimeNotification,
    RuntimeRequest,
    RuntimeRequestError,
    RuntimeRequestMetadataPayload,
    RuntimeResponse,
    RuntimeSessionResult,
    RuntimeStreamChunk,
    UnknownSessionError,
    runtime_subagent_route_from_metadata,
    validate_runtime_request_metadata,
    validate_session_id,
    validate_session_reference_id,
)
from .events import (
    RUNTIME_ACP_CONNECTED,
    RUNTIME_ACP_DELEGATED_LIFECYCLE,
    RUNTIME_ACP_DISCONNECTED,
    RUNTIME_ACP_FAILED,
    RUNTIME_APPROVAL_REQUESTED,
    RUNTIME_BACKGROUND_TASK_CANCELLED,
    RUNTIME_BACKGROUND_TASK_COMPLETED,
    RUNTIME_BACKGROUND_TASK_FAILED,
    RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
    RUNTIME_FAILED,
    RUNTIME_LSP_SERVER_FAILED,
    RUNTIME_LSP_SERVER_REUSED,
    RUNTIME_LSP_SERVER_STARTED,
    RUNTIME_LSP_SERVER_STARTUP_REJECTED,
    RUNTIME_LSP_SERVER_STOPPED,
    RUNTIME_MCP_SERVER_FAILED,
    RUNTIME_MCP_SERVER_STARTED,
    RUNTIME_MCP_SERVER_STOPPED,
    RUNTIME_MEMORY_REFRESHED,
    RUNTIME_QUESTION_ANSWERED,
    RUNTIME_QUESTION_REQUESTED,
    RUNTIME_SKILLS_APPLIED,
    RUNTIME_SKILLS_BINDING_MISMATCH,
    RUNTIME_SKILLS_LOADED,
    RUNTIME_TOOL_STARTED,
    EventEnvelope,
)
from .execution_seams import (
    cache_key_for_effective_config,
    fallback_graph_for_provider_error,
    resolve_runtime_session_routing,
    select_graph_for_effective_config,
)
from .lsp import LspManager, LspManagerState, LspRequest, LspRequestResult, build_lsp_manager
from .mcp import McpManager, build_mcp_manager
from .permission import (
    DelegationGovernance,
    PendingApproval,
    PermissionDecision,
    PermissionPolicy,
    PermissionResolution,
    resolve_permission,
)
from .plan import (
    PlanContributor,
    apply_plan_patch,
    build_plan_contributor,
)
from .provider_protocol import ProviderExecutionError
from .question import PendingQuestion, QuestionResponse
from .session import SessionRef, SessionState, SessionStatus, StoredSessionSummary
from .skills import (
    SkillExecutionSnapshot,
    SkillRuntimeContext,
    build_runtime_context,
    build_runtime_contexts,
    build_skill_execution_snapshot,
    runtime_context_from_payload,
    snapshot_from_payload,
    snapshot_payload,
)
from .storage import SessionEventAppender, SessionStore, SqliteSessionStore
from .task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    BackgroundTaskStatus,
    StoredBackgroundTaskSummary,
    validate_background_task_id,
)
from .tool_provider import BuiltinToolProvider

if TYPE_CHECKING:
    from ..tools.lsp import FormatTool
    from .execution_seams import RuntimeGraphSelection, RuntimeSessionRouting

logger = logging.getLogger(__name__)

_EXECUTABLE_AGENT_PRESETS = frozenset({"leader"})
_EXECUTABLE_SUBAGENT_PRESETS = frozenset({"advisor", "explore", "product", "researcher", "worker"})
_BUILTIN_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "ast_grep_preview",
        "ast_grep_replace",
        "ast_grep_search",
        "background_cancel",
        "background_output",
        "code_search",
        "edit",
        "format_file",
        "glob",
        "grep",
        "list",
        "lsp",
        "multi_edit",
        "read_file",
        "question",
        "shell_exec",
        "skill",
        "task",
        "todo_write",
        "web_fetch",
        "web_search",
        "write_file",
    }
)

_SKILL_BINDING_SCOPE_KEYS = (
    "approval_mode",
    "execution_engine",
    "max_steps",
    "tool_timeout_seconds",
    "model",
    "provider_fallback",
    "resolved_provider",
    "plan",
    "agent",
    "lsp",
    "mcp",
)


@dataclass(frozen=True, slots=True)
class _ActiveSessionKey:
    workspace: Path
    session_id: str


class _ActiveSessionRegistry:
    def __init__(self) -> None:
        self._counts: dict[_ActiveSessionKey, int] = {}
        self._metadata: dict[_ActiveSessionKey, dict[str, object]] = {}
        self._lock = threading.Lock()

    def register(self, *, workspace: Path, session_id: str) -> None:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def remember_metadata(
        self,
        *,
        workspace: Path,
        session_id: str,
        metadata: dict[str, object],
    ) -> None:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            if key not in self._counts:
                return
            self._metadata[key] = dict(metadata)

    def unregister(self, *, workspace: Path, session_id: str) -> None:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            count = self._counts.get(key)
            if count is None:
                return
            if count <= 1:
                self._counts.pop(key, None)
                self._metadata.pop(key, None)
                return
            self._counts[key] = count - 1

    def contains(self, *, workspace: Path, session_id: str) -> bool:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            return key in self._counts

    def metadata(self, *, workspace: Path, session_id: str) -> dict[str, object] | None:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            metadata = self._metadata.get(key)
            return dict(metadata) if metadata is not None else None


_ACTIVE_SESSION_REGISTRY = _ActiveSessionRegistry()
_DELEGATION_GOVERNANCE = DelegationGovernance()


def _coerce_bool_like(value: object | None, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in {"false", "0", "no", "off", ""}


def _coerce_int_like(value: object | None, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class ToolRegistry:
    """Small in-memory registry used by the runtime boundary."""

    tools: dict[str, Tool] = field(default_factory=dict)

    @classmethod
    def from_tools(cls, tools: Iterable[Tool]) -> ToolRegistry:
        return cls(tools={tool.definition.name: tool for tool in tools})

    @classmethod
    def with_defaults(
        cls,
        *,
        lsp_tool: Tool | None = None,
        format_tool: Tool | None = None,
        mcp_tools: tuple[Tool, ...] = (),
        hooks_config: RuntimeHooksConfig | None = None,
        skill_tool: Tool | None = None,
        task_tool: Tool | None = None,
        question_tool: Tool | None = None,
        background_output_tool: Tool | None = None,
        background_cancel_tool: Tool | None = None,
    ) -> ToolRegistry:
        return cls.from_tools(
            BuiltinToolProvider(
                lsp_tool=lsp_tool,
                format_tool=format_tool,
                mcp_tools=mcp_tools,
                hooks_config=hooks_config,
                skill_tool=skill_tool,
                task_tool=task_tool,
                question_tool=question_tool,
                background_output_tool=background_output_tool,
                background_cancel_tool=background_cancel_tool,
            ).provide_tools()
        )

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(definition_with_guidance(tool.definition) for tool in self.tools.values())

    def resolve(self, tool_name: str) -> Tool:
        try:
            return self.tools[tool_name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {tool_name}") from exc

    def filtered(self, patterns: Iterable[str]) -> ToolRegistry:
        normalized_patterns = tuple(pattern for pattern in patterns if pattern)
        return ToolRegistry(
            tools={
                name: tool
                for name, tool in self.tools.items()
                if any(fnmatchcase(name, pattern) for pattern in normalized_patterns)
            }
        )

    def excluding(self, tool_names: Iterable[str]) -> ToolRegistry:
        excluded = frozenset(tool_names)
        return ToolRegistry(
            tools={name: tool for name, tool in self.tools.items() if name not in excluded}
        )


@final
class VoidCodeRuntime:
    """Headless runtime entrypoint for one local deterministic request."""

    _workspace: Path
    _base_tool_registry: ToolRegistry
    _tool_registry: ToolRegistry
    _graph: RuntimeGraph
    _graph_override: RuntimeGraph | None
    _config: RuntimeConfig
    _initial_effective_config: EffectiveRuntimeConfig
    _permission_policy: PermissionPolicy
    _session_store: SessionStore
    _model_provider_registry: ModelProviderRegistry
    _provider_model: ResolvedProviderModel
    _provider_chain: ResolvedProviderChain
    _provider_auth_resolver: ProviderAuthResolver
    _skill_registry: SkillRegistry
    _skill_registry_is_injected: bool
    _lsp_manager: LspManager
    _mcp_manager: McpManager
    _acp_adapter: AcpAdapter
    _graph_cache: dict[tuple[ExecutionEngineName, str], RuntimeGraph]
    _plan_contributor: PlanContributor
    _background_task_threads: dict[str, threading.Thread]
    _background_tasks_reconciled: bool
    _hook_recursion_env_var = "VOIDCODE_RUNNING_TOOL_HOOK"
    _default_context_window_policy = ContextWindowPolicy()

    def __init__(
        self,
        *,
        workspace: Path,
        tool_registry: ToolRegistry | None = None,
        graph: RuntimeGraph | None = None,
        config: RuntimeConfig | None = None,
        permission_policy: PermissionPolicy | None = None,
        session_store: SessionStore | None = None,
        model_provider_registry: ModelProviderRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
        lsp_manager: LspManager | None = None,
        mcp_manager: McpManager | None = None,
        acp_adapter: AcpAdapter | None = None,
        context_window_policy: ContextWindowPolicy | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._config = config or load_runtime_config(self._workspace)
        self._model_provider_registry = (
            model_provider_registry
            or ModelProviderRegistry.with_defaults(provider_configs=self._config.providers)
        )
        initial_agent = self._config.agent
        if initial_agent is not None:
            initial_agent = parse_runtime_agent_payload(
                serialize_runtime_agent_config(initial_agent),
                source="runtime config agent",
            )
            assert initial_agent is not None
            self._validate_runtime_agent_for_execution(
                initial_agent,
                source="runtime config agent",
            )
        initial_model = (
            initial_agent.model
            if initial_agent is not None and initial_agent.model is not None
            else self._config.model
        )
        initial_execution_engine = (
            initial_agent.execution_engine
            if initial_agent is not None and initial_agent.execution_engine is not None
            else self._config.execution_engine
        )
        initial_provider_fallback = (
            initial_agent.provider_fallback
            if initial_agent is not None and initial_agent.provider_fallback is not None
            else self._config.provider_fallback
        )
        self._resolved_provider_config = resolve_provider_config(
            initial_model,
            initial_provider_fallback,
            registry=self._model_provider_registry,
        )
        self._provider_model = self._resolved_provider_config.active_target
        self._provider_chain = self._resolved_provider_config.target_chain
        self._provider_auth_resolver = ProviderAuthResolver(
            providers=self._config.providers,
            env=os.environ,
        )
        self._lsp_manager = lsp_manager or build_lsp_manager(self._config.lsp)
        self._mcp_manager = mcp_manager or build_mcp_manager(self._config.mcp)
        self._skill_registry_is_injected = skill_registry is not None
        self._skill_registry = skill_registry or self._build_skill_registry(self._config.skills)
        self._base_tool_registry = tool_registry or ToolRegistry.with_defaults(
            lsp_tool=self._build_lsp_tool(),
            format_tool=self._build_format_tool(),
            hooks_config=self._config.hooks or RuntimeHooksConfig(),
            skill_tool=SkillTool(
                list_skills=self._skill_registry.all,
                resolve_skill=self._skill_registry.resolve,
            ),
            task_tool=TaskTool(runtime=self),
            question_tool=QuestionTool(),
            background_output_tool=BackgroundOutputTool(runtime=self),
            background_cancel_tool=BackgroundCancelTool(runtime=self),
        )
        self._tool_registry = self._base_tool_registry
        self._graph_override = graph
        self._graph_cache = {}
        self._initial_effective_config = EffectiveRuntimeConfig(
            approval_mode=self._config.approval_mode,
            model=initial_model,
            execution_engine=initial_execution_engine,
            max_steps=self._config.max_steps,
            tool_timeout_seconds=self._config.tool_timeout_seconds,
            provider_fallback=initial_provider_fallback,
            plan=self._config.plan,
            resolved_provider=self._resolved_provider_config,
            agent=initial_agent,
        )
        self._graph = graph or self._build_graph_for_engine_from_config(
            self._initial_effective_config
        )
        self._permission_policy = permission_policy or PermissionPolicy(
            mode=self._config.approval_mode
        )
        self._session_store = session_store or SqliteSessionStore()
        self._acp_adapter = acp_adapter or build_acp_adapter(self._config.acp)
        self._plan_contributor = build_plan_contributor(self._workspace, self._config.plan)
        self._background_task_threads = {}
        self._background_tasks_reconciled = False
        self._default_context_window_policy = context_window_policy or ContextWindowPolicy()

    def __enter__(self) -> VoidCodeRuntime:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = exc_type, exc, tb
        _ = self.disconnect_acp()
        _ = self.shutdown_mcp()
        _ = self.shutdown_lsp()

    @staticmethod
    def _session_with_metadata(session: SessionState, metadata: dict[str, object]) -> SessionState:
        return SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata=metadata,
        )

    def _runtime_state_metadata_with_acp_state(
        self,
        metadata: dict[str, object],
        acp_state: AcpAdapterState,
    ) -> dict[str, object]:
        runtime_state = metadata.get("runtime_state")
        if runtime_state is None:
            runtime_state_metadata: dict[str, object] = {}
        elif isinstance(runtime_state, dict):
            runtime_state_metadata = dict(cast(dict[str, object], runtime_state))
        else:
            runtime_state_metadata = {}
        runtime_state_metadata["acp"] = {
            "mode": acp_state.mode,
            "configured_enabled": acp_state.configuration.configured_enabled,
            "status": acp_state.status,
            "available": acp_state.available,
            "last_error": acp_state.last_error,
            "last_request_type": acp_state.last_request_type,
            "last_request_id": acp_state.last_request_id,
            "last_event_type": acp_state.last_event_type,
            "last_delegation": (
                acp_state.last_delegation.as_payload()
                if acp_state.last_delegation is not None
                else None
            ),
        }
        return {**metadata, "runtime_state": runtime_state_metadata}

    def _session_with_current_acp_metadata(self, session: SessionState) -> SessionState:
        return self._session_with_metadata(
            session,
            self._runtime_state_metadata_with_acp_state(
                session.metadata,
                self._acp_adapter.current_state(),
            ),
        )

    @staticmethod
    def _plan_state_from_metadata(
        metadata: dict[str, object],
        *,
        status: str | None = None,
        approval_request_id: str | None = None,
        blocked_tool: str | None = None,
        error: str | None = None,
    ) -> dict[str, object] | None:
        existing_plan_state = metadata.get("plan_state")
        if isinstance(existing_plan_state, dict):
            plan_state: dict[str, object] = dict(cast(dict[str, object], existing_plan_state))
        else:
            serialized_plan_artifact_obj = metadata.get("plan_artifact")
            if not isinstance(serialized_plan_artifact_obj, dict):
                return None
            serialized_plan_artifact = cast(dict[str, object], serialized_plan_artifact_obj)
            raw_steps_obj = serialized_plan_artifact.get("steps")
            raw_steps = (
                cast(list[object], raw_steps_obj) if isinstance(raw_steps_obj, list) else None
            )
            first_step = (
                cast(dict[str, object], raw_steps[0])
                if raw_steps is not None and bool(raw_steps) and isinstance(raw_steps[0], dict)
                else None
            )
            step_count = len(raw_steps) if raw_steps is not None else 0
            current_step_order = (
                first_step.get("order")
                if first_step is not None and isinstance(first_step.get("order"), int)
                else 1
            )
            current_step_title = (
                cast(str, first_step.get("title"))
                if first_step is not None and isinstance(first_step.get("title"), str)
                else None
            )
            plan_state = {
                "mode": "plan_first",
                "status": "planned",
                "step_count": step_count,
                "current_step_index": 0,
                "current_step_order": current_step_order,
            }
            if current_step_title is not None:
                plan_state["current_step_title"] = current_step_title

        if status is not None:
            plan_state["status"] = status

        if approval_request_id is not None:
            plan_state["approval_request_id"] = approval_request_id
        else:
            plan_state.pop("approval_request_id", None)

        if blocked_tool is not None:
            plan_state["blocked_tool"] = blocked_tool
        else:
            plan_state.pop("blocked_tool", None)

        if error is not None:
            plan_state["last_error"] = error
        else:
            plan_state.pop("last_error", None)

        return plan_state

    def _session_with_plan_state(
        self,
        session: SessionState,
        *,
        status: str | None = None,
        approval_request_id: str | None = None,
        blocked_tool: str | None = None,
        error: str | None = None,
    ) -> SessionState:
        plan_state = self._plan_state_from_metadata(
            session.metadata,
            status=status,
            approval_request_id=approval_request_id,
            blocked_tool=blocked_tool,
            error=error,
        )
        if plan_state is None:
            return session
        return self._session_with_metadata(
            session,
            {
                **session.metadata,
                "plan_state": plan_state,
            },
        )

    def _disconnect_acp_for_session_state(self, session: SessionState) -> SessionState:
        _ = self._acp_adapter.disconnect()
        return self._session_with_current_acp_metadata(session)

    def _reload_persisted_session(self, *, session_id: str) -> SessionState:
        response = self._session_store.load_session(
            workspace=self._workspace, session_id=session_id
        )
        return response.session

    @staticmethod
    def _resequence_event(event: EventEnvelope, *, sequence: int) -> EventEnvelope:
        return EventEnvelope(
            session_id=event.session_id,
            sequence=sequence,
            event_type=event.event_type,
            source=event.source,
            payload=event.payload,
        )

    def _emit_acp_events(
        self,
        *,
        session: SessionState,
        start_sequence: int,
        acp_events: tuple[object, ...],
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int]:
        emitted: list[RuntimeStreamChunk] = []
        current_session = session
        sequence = start_sequence - 1
        for acp_event in self._envelopes_for_acp_events(
            session_id=session.session.id,
            start_sequence=start_sequence,
            acp_events=acp_events,
        ):
            sequence = acp_event.sequence
            current_session = self._session_with_current_acp_metadata(current_session)
            emitted.append(
                RuntimeStreamChunk(kind="event", session=current_session, event=acp_event)
            )
        return tuple(emitted), current_session, sequence

    def _emit_current_acp_drain(
        self,
        *,
        session: SessionState,
        start_sequence: int,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int]:
        return self._emit_acp_events(
            session=session,
            start_sequence=start_sequence,
            acp_events=self._acp_adapter.drain_events(),
        )

    def _start_run_acp(
        self,
        *,
        session: SessionState,
        sequence: int,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int, RuntimeStreamChunk | None]:
        if self.current_acp_state().configuration.configured_enabled is not True:
            return (), session, sequence, None
        try:
            acp_events = self._acp_adapter.connect()
        except Exception as exc:
            emitted, updated_session, last_sequence = self._emit_current_acp_drain(
                session=session,
                start_sequence=sequence + 1,
            )
            failed_session = self._session_with_current_acp_metadata(updated_session)
            failed_chunk = self._failed_chunk(
                session=failed_session,
                sequence=last_sequence + 1,
                error=str(exc),
                payload={"kind": "acp_startup_failed"},
            )
            return emitted, failed_session, last_sequence + 1, failed_chunk
        emitted, updated_session, last_sequence = self._emit_acp_events(
            session=session,
            start_sequence=sequence + 1,
            acp_events=acp_events,
        )
        if not emitted:
            updated_session = self._session_with_current_acp_metadata(updated_session)
        return emitted, updated_session, last_sequence or sequence, None

    def _finalize_run_acp(
        self,
        *,
        session: SessionState,
        sequence: int,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int]:
        if self.current_acp_state().configuration.configured_enabled is not True:
            return (), session, sequence
        emitted, updated_session, last_sequence = self._emit_acp_events(
            session=session,
            start_sequence=sequence + 1,
            acp_events=self._acp_adapter.disconnect(),
        )
        if not emitted:
            updated_session = self._session_with_current_acp_metadata(updated_session)
        return emitted, updated_session, last_sequence or sequence

    def _build_graph_for_engine_from_config(self, config: EffectiveRuntimeConfig) -> RuntimeGraph:
        cache_key = cache_key_for_effective_config(config)
        if cache_key in self._graph_cache:
            return self._graph_cache[cache_key]

        graph = select_graph_for_effective_config(config=config).graph
        self._graph_cache[cache_key] = graph
        return graph

    def _graph_selection_for_effective_config(
        self,
        config: EffectiveRuntimeConfig,
        *,
        provider_attempt: int = 0,
    ) -> RuntimeGraphSelection:
        return select_graph_for_effective_config(
            config=config,
            provider_attempt=provider_attempt,
        )

    def _fallback_graph_selection(
        self,
        *,
        error: ProviderExecutionError,
        session_metadata: dict[str, object],
        provider_attempt: int,
    ) -> RuntimeGraphSelection | None:
        return fallback_graph_for_provider_error(
            error=error,
            provider_chain=self._provider_chain_for_session_metadata(session_metadata),
            config=self._effective_runtime_config_from_metadata(session_metadata),
            provider_attempt=provider_attempt,
        )

    def _session_routing_for_request(self, request: RuntimeRequest) -> RuntimeSessionRouting:
        return resolve_runtime_session_routing(request)

    def _runtime_config_for_request(self, request: RuntimeRequest) -> EffectiveRuntimeConfig:
        resolved = self._effective_runtime_config_from_metadata(None)
        request_agent = request.metadata.get("agent")
        if request_agent is not None:
            resolved = self._config_with_request_agent_override(
                resolved,
                request_agent,
                allow_subagent_presets=request.subagent_routing is not None,
            )
        request_max_steps = request.metadata.get("max_steps")
        if request_max_steps is not None:
            assert isinstance(request_max_steps, int)
            return EffectiveRuntimeConfig(
                approval_mode=resolved.approval_mode,
                model=resolved.model,
                execution_engine=resolved.execution_engine,
                max_steps=request_max_steps,
                tool_timeout_seconds=resolved.tool_timeout_seconds,
                provider_fallback=resolved.provider_fallback,
                plan=resolved.plan,
                resolved_provider=resolved.resolved_provider,
                agent=resolved.agent,
            )
        return resolved

    def _build_skill_registry(self, skills_config: RuntimeSkillsConfig | None) -> SkillRegistry:
        if skills_config is None or skills_config.enabled is not True:
            return SkillRegistry()
        if skills_config.paths:
            return SkillRegistry.discover(
                workspace=self._workspace,
                search_paths=skills_config.paths,
            )
        return SkillRegistry.discover(workspace=self._workspace)

    def _skills_config_for_effective_config(
        self,
        effective_config: EffectiveRuntimeConfig,
    ) -> RuntimeSkillsConfig | None:
        if effective_config.agent is not None and effective_config.agent.skills is not None:
            return effective_config.agent.skills
        return self._config.skills

    def _skill_registry_for_effective_config(
        self,
        effective_config: EffectiveRuntimeConfig,
    ) -> SkillRegistry:
        if self._skill_registry_is_injected:
            return self._skill_registry
        return self._build_skill_registry(
            self._skills_config_for_effective_config(effective_config)
        )

    def _build_lsp_tool(self) -> Tool | None:
        if self._lsp_manager.current_state().mode != "managed":
            return None
        from ..tools.lsp import LspTool

        return LspTool(requester=self.request_lsp)

    def _build_format_tool(self) -> FormatTool:
        from ..tools.lsp import FormatTool

        return FormatTool(self._config.hooks or RuntimeHooksConfig(), self._workspace)

    def _build_mcp_tools(self) -> tuple[Tool, ...]:
        if self._mcp_manager.current_state().mode != "managed":
            return ()
        from ..tools.mcp import McpTool

        return tuple(
            McpTool(
                server_name=tool.server_name,
                tool_name=tool.tool_name,
                description=tool.description,
                input_schema=tool.input_schema,
                requester=self.request_mcp_tool,
            )
            for tool in self._mcp_manager.list_tools(workspace=self._workspace)
        )

    def _refresh_mcp_tools(self) -> None:
        if self._mcp_manager.current_state().mode != "managed":
            return
        merged_tools = dict(self._base_tool_registry.tools)
        for tool in self._build_mcp_tools():
            merged_tools[tool.definition.name] = tool
        self._tool_registry = ToolRegistry(tools=merged_tools)

    def _refresh_mcp_tools_for_session(
        self,
        *,
        session: SessionState,
        sequence: int,
        failure_kind: str,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int, RuntimeStreamChunk | None]:
        try:
            self._refresh_mcp_tools()
        except Exception as exc:
            emitted_events = self._envelopes_for_mcp_events(
                session_id=session.session.id,
                start_sequence=sequence + 1,
                mcp_events=self._mcp_manager.drain_events(),
            )
            emitted = tuple(
                RuntimeStreamChunk(kind="event", session=session, event=event)
                for event in emitted_events
            )
            last_sequence = emitted_events[-1].sequence if emitted_events else sequence
            failed_chunk = self._failed_chunk(
                session=session,
                sequence=last_sequence + 1,
                error=str(exc),
                payload={"kind": failure_kind},
            )
            return emitted, session, last_sequence, failed_chunk
        return (), session, sequence, None

    def _tool_registry_for_effective_config(
        self,
        effective_config: EffectiveRuntimeConfig,
    ) -> ToolRegistry:
        agent = effective_config.agent
        if agent is None:
            return self._tool_registry

        scoped_registry = self._tool_registry
        manifest = get_builtin_agent_manifest(agent.preset)
        if manifest is not None and manifest.tool_allowlist:
            scoped_registry = scoped_registry.filtered(manifest.tool_allowlist)

        if agent.tools is not None:
            if agent.tools.builtin is not None and agent.tools.builtin.enabled is False:
                scoped_registry = scoped_registry.excluding(_BUILTIN_TOOL_NAMES)
            if agent.tools.allowlist is not None:
                scoped_registry = scoped_registry.filtered(agent.tools.allowlist)
            if agent.tools.default is not None:
                scoped_registry = scoped_registry.filtered(agent.tools.default)

        return scoped_registry

    def current_lsp_state(self) -> LspManagerState:
        return self._lsp_manager.current_state()

    @property
    def provider_auth_resolver(self) -> ProviderAuthResolver:
        return self._provider_auth_resolver

    def request_lsp(
        self,
        *,
        server_name: str | None,
        method: str,
        params: dict[str, object],
        workspace: Path,
    ) -> LspRequestResult:
        return self._lsp_manager.request(
            LspRequest(
                server_name=server_name,
                method=method,
                params=params,
                workspace=workspace,
            )
        )

    def request_mcp_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
    ):
        return self._mcp_manager.call_tool(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            workspace=workspace,
        )

    def shutdown_mcp(self) -> tuple[EventEnvelope, ...]:
        return self._envelopes_for_mcp_events(
            session_id="runtime",
            start_sequence=1,
            mcp_events=self._mcp_manager.shutdown(),
        )

    def shutdown_lsp(self) -> tuple[EventEnvelope, ...]:
        return self._envelopes_for_lsp_events(
            session_id="runtime",
            start_sequence=1,
            lsp_events=self._lsp_manager.shutdown(),
        )

    def current_acp_state(self):
        return self._acp_adapter.current_state()

    def connect_acp(self) -> tuple[EventEnvelope, ...]:
        return self._envelopes_for_acp_events(
            session_id="runtime",
            start_sequence=1,
            acp_events=self._acp_adapter.connect(),
        )

    def disconnect_acp(self) -> tuple[EventEnvelope, ...]:
        return self._envelopes_for_acp_events(
            session_id="runtime",
            start_sequence=1,
            acp_events=self._acp_adapter.disconnect(),
        )

    def request_acp(self, *, request_type: str, payload: dict[str, object]) -> AcpResponseEnvelope:
        return self._acp_adapter.request(
            AcpRequestEnvelope(request_type=request_type, payload=payload)
        )

    def request_delegated_acp(
        self,
        *,
        request_type: str,
        task_id: str,
        payload: dict[str, object],
    ) -> AcpResponseEnvelope:
        task = self.load_background_task(task_id)
        return self._acp_adapter.request(
            AcpRequestEnvelope(
                request_type=request_type,
                request_id=task.task.id,
                session_id=task.session_id,
                parent_session_id=task.parent_session_id,
                delegation=self._delegated_execution_for_task(
                    task=task,
                    lifecycle_status=(
                        "waiting_approval"
                        if task.status == "running" and task.approval_request_id
                        else task.status
                    ),
                ),
                payload=payload,
            )
        )

    def _delegated_execution_for_task(
        self,
        *,
        task: BackgroundTaskState,
        lifecycle_status: str,
        approval_blocked: bool | None = None,
        result_available: bool | None = None,
    ) -> AcpDelegatedExecution:
        routing = task.routing_identity
        delegation_metadata = task.request.metadata.get("delegation")
        delegation_dict = (
            cast(dict[str, object], delegation_metadata)
            if isinstance(delegation_metadata, dict)
            else {}
        )
        return AcpDelegatedExecution(
            parent_session_id=task.parent_session_id,
            requested_child_session_id=task.request.session_id,
            child_session_id=task.session_id,
            delegated_task_id=task.task.id,
            approval_request_id=task.approval_request_id,
            question_request_id=task.question_request_id,
            routing_mode=routing.mode if routing is not None else None,
            routing_category=routing.category if routing is not None else None,
            routing_subagent_type=routing.subagent_type if routing is not None else None,
            routing_description=routing.description if routing is not None else None,
            routing_command=routing.command if routing is not None else None,
            selected_preset=(
                cast(str, delegation_dict["selected_preset"])
                if isinstance(delegation_dict.get("selected_preset"), str)
                else None
            ),
            selected_execution_engine=(
                cast(str, delegation_dict["selected_execution_engine"])
                if isinstance(delegation_dict.get("selected_execution_engine"), str)
                else None
            ),
            lifecycle_status=cast(
                Literal[
                    "queued",
                    "running",
                    "waiting_approval",
                    "completed",
                    "failed",
                    "cancelled",
                ],
                lifecycle_status,
            ),
            approval_blocked=(
                approval_blocked if approval_blocked is not None else task.status == "running"
            ),
            result_available=(
                result_available if result_available is not None else task.result_available
            ),
            cancellation_cause=task.cancellation_cause,
        )

    def _publish_delegated_acp_event(
        self,
        *,
        task: BackgroundTaskState,
        lifecycle_status: str,
        payload: dict[str, object],
        approval_blocked: bool | None = None,
        result_available: bool | None = None,
    ) -> None:
        if self.current_acp_state().status != "connected":
            return
        delegation = self._delegated_execution_for_task(
            task=task,
            lifecycle_status=lifecycle_status,
            approval_blocked=approval_blocked,
            result_available=result_available,
        )
        response = self._acp_adapter.publish(
            AcpEventEnvelope(
                event_type=RUNTIME_ACP_DELEGATED_LIFECYCLE,
                session_id=task.session_id,
                parent_session_id=task.parent_session_id,
                delegation=delegation,
                payload=payload,
            )
        )
        if response.status != "ok":
            logger.debug("skipping ACP delegated lifecycle event: %s", response.error)

    def _append_parent_acp_delegated_lifecycle_event(
        self,
        *,
        task: BackgroundTaskState,
        lifecycle_status: str,
        payload: dict[str, object],
        approval_blocked: bool | None = None,
        result_available: bool | None = None,
    ) -> None:
        parent_session_id = task.parent_session_id
        if parent_session_id is None:
            return
        session_event_appender = self._session_store
        if not isinstance(session_event_appender, SessionEventAppender):
            return
        delegation = self._delegated_execution_for_task(
            task=task,
            lifecycle_status=lifecycle_status,
            approval_blocked=approval_blocked,
            result_available=result_available,
        )
        correlation_id = (
            task.approval_request_id or task.question_request_id or task.session_id or "none"
        )
        try:
            _ = session_event_appender.append_session_event(
                workspace=self._workspace,
                session_id=parent_session_id,
                event_type=RUNTIME_ACP_DELEGATED_LIFECYCLE,
                source="runtime",
                payload={
                    "session_id": task.session_id,
                    "parent_session_id": parent_session_id,
                    "delegation": delegation.as_payload(),
                    **payload,
                },
                dedupe_key=(
                    f"{RUNTIME_ACP_DELEGATED_LIFECYCLE}:{task.task.id}:{lifecycle_status}:"
                    f"{correlation_id}"
                ),
            )
        except UnknownSessionError:
            logger.debug(
                "skipping ACP delegated lifecycle event for unavailable parent session: %s",
                parent_session_id,
            )

    def fail_acp(self, message: str) -> tuple[EventEnvelope, ...]:
        return self._envelopes_for_acp_events(
            session_id="runtime",
            start_sequence=1,
            acp_events=self._acp_adapter.fail(message),
        )

    def _run_with_persistence(
        self,
        request: RuntimeRequest,
        *,
        allow_internal_metadata: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        request = self._validated_request(
            request,
            allow_internal_metadata=allow_internal_metadata,
        )
        session_id = self._resolve_session_id(request)
        self._register_active_session_id(
            session_id,
            metadata=dict(request.metadata),
        )
        try:
            events: list[EventEnvelope] = []
            output: str | None = None
            final_session: SessionState | None = None

            try:
                for chunk in self._stream_chunks(request, session_id=session_id):
                    final_session = chunk.session
                    if chunk.event is not None:
                        events.append(chunk.event)
                    if chunk.kind == "output":
                        if output is not None:
                            raise ValueError("runtime stream emitted multiple output chunks")
                        output = chunk.output
                    yield chunk
            except Exception:
                if final_session is not None and final_session.status == "failed":
                    final_session = self._disconnect_acp_for_session_state(final_session)
                    response = RuntimeResponse(
                        session=final_session, events=tuple(events), output=output
                    )
                    self._persist_response(request=request, response=response)
                raise

            if final_session is None:
                raise ValueError("runtime stream emitted no chunks")

            if final_session.status == "waiting":
                final_session = self._disconnect_acp_for_session_state(final_session)

            response = RuntimeResponse(session=final_session, events=tuple(events), output=output)
            self._persist_response(request=request, response=response)
        finally:
            self._unregister_active_session_id(session_id)

    def run(self, request: RuntimeRequest) -> RuntimeResponse:
        events: list[EventEnvelope] = []
        output: str | None = None
        final_session: SessionState | None = None

        for chunk in self._run_with_persistence(request):
            final_session = chunk.session
            if chunk.event is not None:
                events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output

        if final_session is None:
            raise ValueError("runtime stream emitted no chunks")

        if final_session.status == "waiting":
            final_session = self._reload_persisted_session(session_id=final_session.session.id)

        return RuntimeResponse(session=final_session, events=tuple(events), output=output)

    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        if "provider_stream" in request.metadata:
            return self._run_with_persistence(request)

        request_with_stream = RuntimeRequest(
            prompt=request.prompt,
            session_id=request.session_id,
            parent_session_id=request.parent_session_id,
            metadata=validate_runtime_request_metadata(
                {**request.metadata, "provider_stream": True}
            ),
            allocate_session_id=request.allocate_session_id,
        )
        return self._run_with_persistence(request_with_stream)

    def _stream_chunks(
        self,
        request: RuntimeRequest,
        *,
        session_id: str | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        resolved_session_id = session_id or self._resolve_session_id(request)
        effective_config = self._runtime_config_for_request(request)
        request_metadata = self._fresh_request_metadata(request.metadata)
        session = SessionState(
            session=SessionRef(id=resolved_session_id, parent_id=request.parent_session_id),
            status="running",
            turn=1,
            metadata={
                **request_metadata,
                "workspace": str(self._workspace),
                "runtime_config": self._runtime_config_metadata(effective_config),
                "runtime_state": self._runtime_state_metadata(),
            },
        )
        sequence = 1

        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence,
                event_type="runtime.request_received",
                source="runtime",
                payload={
                    "prompt": request.prompt,
                    **(
                        {"agent_preset": active_agent.preset}
                        if (active_agent := effective_config.agent) is not None
                        else {}
                    ),
                },
            ),
        )

        (
            mcp_startup_chunks,
            session,
            sequence,
            mcp_failed_chunk,
        ) = self._refresh_mcp_tools_for_session(
            session=session,
            sequence=sequence,
            failure_kind="mcp_startup_failed",
        )
        for chunk in mcp_startup_chunks:
            sequence = cast(EventEnvelope, chunk.event).sequence
            yield chunk
        if mcp_failed_chunk is not None:
            yield mcp_failed_chunk
            return

        tool_registry = self._tool_registry_for_effective_config(effective_config)
        skill_registry = self._skill_registry_for_effective_config(effective_config)
        skills_config = self._skills_config_for_effective_config(effective_config)

        start_hook_outcome = self._run_lifecycle_hooks(
            session=session,
            sequence=sequence,
            surface="session_start",
            payload={"prompt": request.prompt},
        )
        yield from start_hook_outcome.chunks
        sequence = start_hook_outcome.last_sequence
        if start_hook_outcome.failed_error is not None:
            yield self._failed_chunk(
                session=session,
                sequence=sequence + 1,
                error=start_hook_outcome.failed_error,
            )
            return

        sequence += 1
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence,
                event_type=RUNTIME_SKILLS_LOADED,
                source="runtime",
                payload={"skills": self._loaded_skill_names(skill_registry)},
            ),
        )

        startup_chunks, session, sequence, startup_failed_chunk = self._start_run_acp(
            session=session,
            sequence=sequence,
        )
        for chunk in startup_chunks:
            yield chunk
        if startup_failed_chunk is not None:
            yield startup_failed_chunk
            return

        skill_snapshot = self._build_skill_snapshot(
            skill_registry,
            metadata=session.metadata,
            agent=effective_config.agent,
            source="run",
        )
        frozen_applied_skills = skill_snapshot.applied_skill_payloads
        skill_prompt_context = skill_snapshot.skill_prompt_context
        if skills_config is not None and skills_config.enabled is True:
            session = SessionState(
                session=session.session,
                status=session.status,
                turn=session.turn,
                metadata={
                    **session.metadata,
                    **self._snapshot_to_session_metadata(skill_snapshot),
                },
            )
        if skill_snapshot.applied_skill_payloads:
            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type=RUNTIME_SKILLS_APPLIED,
                    source="runtime",
                    payload={
                        "skills": list(skill_snapshot.selected_skill_names),
                        "count": len(skill_snapshot.applied_skill_payloads),
                        "prompt_context_built": bool(skill_prompt_context),
                        "prompt_context_length": len(skill_prompt_context),
                    },
                ),
            )

        active_agent = effective_config.agent

        planned_prompt, planned_metadata = apply_plan_patch(
            contributor=self._plan_contributor,
            prompt=request.prompt,
            metadata=request_metadata,
        )

        graph_request = GraphRunRequest(
            session=session,
            prompt=planned_prompt,
            available_tools=tool_registry.definitions(),
            applied_skills=frozen_applied_skills,
            skill_prompt_context=skill_prompt_context,
            context_window=self._prepare_provider_context_window(
                prompt=planned_prompt,
                tool_results=(),
                session_metadata=session.metadata,
            ),
            metadata={
                **planned_metadata,
                "agent_preset": serialize_runtime_agent_config(
                    self._effective_runtime_config_from_metadata(session.metadata).agent
                ),
                "provider_attempt": 0,
                "provider_stream": _coerce_bool_like(
                    planned_metadata.get("provider_stream", False),
                    False,
                ),
            },
        )
        tool_results: list[ToolResult] = []
        graph = self._graph_for_session_metadata(session.metadata)

        last_chunk: RuntimeStreamChunk | None = None
        last_sequence = sequence
        for chunk in self._execute_graph_loop(
            graph=graph,
            tool_registry=tool_registry,
            session=session,
            sequence=sequence,
            graph_request=graph_request,
            tool_results=tool_results,
            permission_policy=self._permission_policy,
        ):
            last_chunk = chunk
            if chunk.event is not None:
                last_sequence = chunk.event.sequence
            yield chunk

        if last_chunk is None:
            return

        if last_chunk.session.status == "waiting":
            idle_hook_outcome = self._run_lifecycle_hooks(
                session=last_chunk.session,
                sequence=last_sequence,
                surface="session_idle",
                payload={"reason": "waiting_for_approval"},
            )
            yield from idle_hook_outcome.chunks
            if idle_hook_outcome.failed_error is not None:
                failed_session = self._disconnect_acp_for_session_state(last_chunk.session)
                yield self._failed_chunk(
                    session=failed_session,
                    sequence=idle_hook_outcome.last_sequence + 1,
                    error=idle_hook_outcome.failed_error,
                )
            return

        final_chunks, finalized_session, final_sequence = self._finalize_run_acp(
            session=last_chunk.session,
            sequence=last_sequence,
        )
        for chunk in final_chunks:
            yield chunk
        end_hook_outcome = self._run_lifecycle_hooks(
            session=finalized_session,
            sequence=final_sequence,
            surface="session_end",
            payload={"session_status": finalized_session.status},
        )
        yield from end_hook_outcome.chunks
        if end_hook_outcome.failed_error is not None:
            logger.warning(
                "session_end hook failed for %s: %s",
                session.session.id,
                end_hook_outcome.failed_error,
            )

    def _execute_graph_loop(
        self,
        *,
        graph: RuntimeGraph,
        tool_registry: ToolRegistry,
        session: SessionState,
        sequence: int,
        graph_request: GraphRunRequest,
        tool_results: list[ToolResult],
        approval_resolution: tuple[PendingApproval, PermissionResolution] | None = None,
        permission_policy: PermissionPolicy | None = None,
        preserved_continuity_state: RuntimeContinuityState | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        active_permission_policy = permission_policy or self._permission_policy
        # Continuity memory reinjection boundary: allow a continuity state to be
        # carried into the next iteration after a memory compaction (if any).
        continuity_to_reinject: RuntimeContinuityState | None = preserved_continuity_state
        provider_attempt = self._provider_attempt_from_metadata(graph_request.metadata)
        while True:
            current_session = session
            base_context = self._prepare_provider_context_window(
                prompt=graph_request.prompt,
                tool_results=tuple(tool_results),
                session_metadata=current_session.metadata,
            )
            reinjected_continuity = continuity_to_reinject
            # Apply reinjected continuity state if present, giving reinjection
            # semantics after a compaction boundary.
            if reinjected_continuity is not None:
                context_window = RuntimeContextWindow(
                    prompt=base_context.prompt,
                    tool_results=base_context.tool_results,
                    compacted=base_context.compacted,
                    compaction_reason=base_context.compaction_reason,
                    original_tool_result_count=base_context.original_tool_result_count,
                    retained_tool_result_count=base_context.retained_tool_result_count,
                    max_tool_result_count=base_context.max_tool_result_count,
                    continuity_state=reinjected_continuity,
                )
            else:
                context_window = base_context
            continuity_to_reinject = None
            session = self._session_with_context_window_metadata(current_session, context_window)
            graph_request = GraphRunRequest(
                session=session,
                prompt=graph_request.prompt,
                available_tools=graph_request.available_tools,
                applied_skills=graph_request.applied_skills,
                skill_prompt_context=graph_request.skill_prompt_context,
                context_window=context_window,
                metadata=graph_request.metadata,
            )
            if context_window.compacted and reinjected_continuity is None:
                sequence += 1
                yield RuntimeStreamChunk(
                    kind="event",
                    session=session,
                    event=EventEnvelope(
                        session_id=session.session.id,
                        sequence=sequence,
                        event_type=RUNTIME_MEMORY_REFRESHED,
                        source="runtime",
                        payload={
                            "reason": context_window.compaction_reason,
                            "original_tool_result_count": context_window.original_tool_result_count,
                            "retained_tool_result_count": context_window.retained_tool_result_count,
                            "compacted": True,
                            "continuity_state": (
                                context_window.continuity_state.metadata_payload()
                                if context_window.continuity_state is not None
                                else None
                            ),
                        },
                    ),
                )
            try:
                graph_step = graph.step(
                    graph_request,
                    tool_results=tuple(tool_results),
                    session=session,
                )
            except Exception as exc:
                current_provider_attempt = self._provider_attempt_from_metadata(
                    {"provider_attempt": provider_attempt}
                )
                provider_error = exc if isinstance(exc, ProviderExecutionError) else None
                if provider_error is not None:
                    if provider_error.kind == "cancelled":
                        yield self._failed_chunk(
                            session=session,
                            sequence=sequence + 1,
                            error=str(provider_error),
                            payload={
                                "provider_error_kind": provider_error.kind,
                                "provider": provider_error.provider_name,
                                "model": provider_error.model_name,
                                "cancelled": True,
                            },
                        )
                        return
                    fallback_selection = self._fallback_graph_selection(
                        error=provider_error,
                        session_metadata=session.metadata,
                        provider_attempt=current_provider_attempt,
                    )
                    next_attempt: int = current_provider_attempt + 1
                    if fallback_selection is not None:
                        next_target = fallback_selection.provider_target
                        logger.info(
                            (
                                "provider fallback for session %s: %s/%s -> %s/%s "
                                "(reason=%s, attempt=%s)"
                            ),
                            session.session.id,
                            provider_error.provider_name,
                            provider_error.model_name,
                            next_target.selection.provider,
                            next_target.selection.model,
                            provider_error.kind,
                            next_attempt,
                        )
                        sequence += 1
                        yield RuntimeStreamChunk(
                            kind="event",
                            session=session,
                            event=EventEnvelope(
                                session_id=session.session.id,
                                sequence=sequence,
                                event_type="runtime.provider_fallback",
                                source="runtime",
                                payload={
                                    "reason": provider_error.kind,
                                    "from_provider": provider_error.provider_name,
                                    "from_model": provider_error.model_name,
                                    "to_provider": next_target.selection.provider,
                                    "to_model": next_target.selection.model,
                                    "attempt": next_attempt,
                                    **(
                                        {"provider_error_details": provider_error.details}
                                        if provider_error.details is not None
                                        else {}
                                    ),
                                },
                            ),
                        )
                        provider_attempt = fallback_selection.provider_attempt
                        session = SessionState(
                            session=session.session,
                            status=session.status,
                            turn=session.turn,
                            metadata={
                                **session.metadata,
                                "provider_attempt": provider_attempt,
                            },
                        )
                        graph = fallback_selection.graph
                        graph_request = GraphRunRequest(
                            session=session,
                            prompt=graph_request.prompt,
                            available_tools=graph_request.available_tools,
                            applied_skills=graph_request.applied_skills,
                            skill_prompt_context=graph_request.skill_prompt_context,
                            metadata={
                                **graph_request.metadata,
                                "provider_attempt": provider_attempt,
                            },
                        )
                        continue
                    if provider_error.kind in {"rate_limit", "invalid_model", "transient_failure"}:
                        yield self._failed_chunk(
                            session=session,
                            sequence=sequence + 1,
                            error=format_fallback_exhausted_error(
                                provider_name=provider_error.provider_name,
                                model_name=provider_error.model_name,
                                attempt=next_attempt,
                            ),
                            payload={
                                "provider_error_kind": provider_error.kind,
                                "provider": provider_error.provider_name,
                                "model": provider_error.model_name,
                                "fallback_exhausted": True,
                                **(
                                    {"provider_error_details": provider_error.details}
                                    if provider_error.details is not None
                                    else {}
                                ),
                            },
                        )
                        return
                if provider_error is not None:
                    yield self._failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error=str(provider_error),
                        payload={
                            "provider_error_kind": provider_error.kind,
                            "provider": provider_error.provider_name,
                            "model": provider_error.model_name,
                            **(
                                {"provider_error_details": provider_error.details}
                                if provider_error.details is not None
                                else {}
                            ),
                        },
                    )
                    return
                classified_error = classify_provider_error(exc)
                yield self._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=str(exc),
                    payload=(
                        {"kind": "provider_context_limit"}
                        if isinstance(classified_error, SingleAgentContextLimitError)
                        else None
                    ),
                )
                if isinstance(classified_error, SingleAgentContextLimitError):
                    return
                raise

            is_final_step = (
                getattr(graph_step, "is_finished", False)
                or getattr(graph_step, "output", None) is not None
            )
            current_chunk_session = session
            if is_final_step:
                current_chunk_session = self._session_with_plan_state(
                    SessionState(
                        session=session.session,
                        status="completed",
                        turn=session.turn,
                        metadata=session.metadata,
                    ),
                    status="completed",
                )

            for event in self._renumber_events(
                getattr(graph_step, "events", ()),
                session_id=session.session.id,
                start_sequence=sequence + 1,
            ):
                sequence = event.sequence
                yield RuntimeStreamChunk(kind="event", session=current_chunk_session, event=event)

            if is_final_step:
                if getattr(graph_step, "output", None) is not None:
                    yield RuntimeStreamChunk(
                        kind="output",
                        session=current_chunk_session,
                        output=graph_step.output,
                    )
                break

            plan_tool_call = getattr(graph_step, "tool_call", None)
            if plan_tool_call is None:
                yield self._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="graph step did not produce a tool call or output",
                )
                raise ValueError("graph step did not produce a tool call or output")

            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type="graph.tool_request_created",
                    source="graph",
                    payload={
                        "tool": plan_tool_call.tool_name,
                        "arguments": dict(plan_tool_call.arguments),
                        **(
                            {"path": path}
                            if isinstance((path := plan_tool_call.arguments.get("path")), str)
                            else {}
                        ),
                    },
                ),
            )

            try:
                tool = tool_registry.resolve(plan_tool_call.tool_name)
            except Exception as exc:
                yield self._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
                raise

            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type="runtime.tool_lookup_succeeded",
                    source="runtime",
                    payload={"tool": plan_tool_call.tool_name},
                ),
            )

            sequence += 1
            if approval_resolution is not None:
                pending, decision = approval_resolution
                if (
                    plan_tool_call.tool_name == pending.tool_name
                    and dict(plan_tool_call.arguments) == pending.arguments
                ):
                    sequence += 1
                    permission_chunks = self._approval_resolution_outcome(
                        session=session,
                        pending=pending,
                        decision=decision,
                        sequence=sequence,
                    )
                    approval_resolution = None
                else:
                    msg = (
                        f"graph step produced a different tool call "
                        f"({plan_tool_call.tool_name}) than the pending "
                        f"approval ({pending.tool_name})"
                    )
                    raise ValueError(msg)
            else:
                permission_chunks = self._resolve_permission(
                    session=session,
                    tool=tool.definition,
                    tool_call=plan_tool_call,
                    sequence=sequence,
                    permission_policy=active_permission_policy,
                )
            yield from permission_chunks.chunks
            if permission_chunks.chunks:
                session = permission_chunks.chunks[-1].session
            if permission_chunks.pending_approval is not None:
                return
            if permission_chunks.denied:
                return

            sequence = permission_chunks.last_sequence

            pre_hook_outcome = self._run_tool_hooks(
                session=session,
                sequence=sequence,
                tool_name=plan_tool_call.tool_name,
                phase="pre",
            )
            yield from pre_hook_outcome.chunks
            sequence = pre_hook_outcome.last_sequence
            if pre_hook_outcome.failed_error is not None:
                yield self._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=pre_hook_outcome.failed_error,
                )
                raise RuntimeError(pre_hook_outcome.failed_error)

            _tool_timeout = self._effective_runtime_config_from_metadata(
                session.metadata
            ).tool_timeout_seconds
            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type=RUNTIME_TOOL_STARTED,
                    source="runtime",
                    payload={"tool": plan_tool_call.tool_name},
                ),
            )
            try:
                with bind_runtime_tool_context(
                    RuntimeToolInvocationContext(
                        session_id=session.session.id,
                        parent_session_id=session.session.parent_id,
                        delegation_depth=self._delegation_depth_from_metadata(session.metadata),
                        remaining_spawn_budget=self._remaining_spawn_budget_from_metadata(
                            session.metadata
                        ),
                    )
                ):
                    if _tool_timeout is None:
                        tool_result = tool.invoke(plan_tool_call, workspace=self._workspace)
                    elif isinstance(tool, RuntimeTimeoutAwareTool):
                        tool_result = tool.invoke_with_runtime_timeout(
                            plan_tool_call,
                            workspace=self._workspace,
                            timeout_seconds=_tool_timeout,
                        )
                    else:
                        tool_result = tool.invoke(plan_tool_call, workspace=self._workspace)
            except Exception as exc:
                for acp_event in self._envelopes_for_acp_events(
                    session_id=session.session.id,
                    start_sequence=sequence + 1,
                    acp_events=self._acp_adapter.drain_events(),
                ):
                    sequence = acp_event.sequence
                    session = self._session_with_current_acp_metadata(session)
                    yield RuntimeStreamChunk(kind="event", session=session, event=acp_event)
                for mcp_event in self._envelopes_for_mcp_events(
                    session_id=session.session.id,
                    start_sequence=sequence + 1,
                    mcp_events=self._mcp_manager.drain_events(),
                ):
                    sequence = mcp_event.sequence
                    yield RuntimeStreamChunk(kind="event", session=session, event=mcp_event)
                for lsp_event in self._envelopes_for_lsp_events(
                    session_id=session.session.id,
                    start_sequence=sequence + 1,
                    lsp_events=self._lsp_manager.drain_events(),
                ):
                    sequence = lsp_event.sequence
                    yield RuntimeStreamChunk(kind="event", session=session, event=lsp_event)
                if isinstance(exc, RuntimeToolTimeoutError):
                    sequence += 1
                    yield RuntimeStreamChunk(
                        kind="event",
                        session=session,
                        event=EventEnvelope(
                            session_id=session.session.id,
                            sequence=sequence,
                            event_type="runtime.tool_timeout",
                            source="runtime",
                            payload={
                                "tool": plan_tool_call.tool_name,
                                "timeout_seconds": _tool_timeout,
                            },
                        ),
                    )
                    yield self._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
                    return
                yield self._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
                raise

            for acp_event in self._envelopes_for_acp_events(
                session_id=session.session.id,
                start_sequence=sequence + 1,
                acp_events=self._acp_adapter.drain_events(),
            ):
                sequence = acp_event.sequence
                session = self._session_with_current_acp_metadata(session)
                yield RuntimeStreamChunk(kind="event", session=session, event=acp_event)

            for mcp_event in self._envelopes_for_mcp_events(
                session_id=session.session.id,
                start_sequence=sequence + 1,
                mcp_events=self._mcp_manager.drain_events(),
            ):
                sequence = mcp_event.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=mcp_event)

            for lsp_event in self._envelopes_for_lsp_events(
                session_id=session.session.id,
                start_sequence=sequence + 1,
                lsp_events=self._lsp_manager.drain_events(),
            ):
                sequence = lsp_event.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=lsp_event)

            if plan_tool_call.tool_name == QuestionTool.definition.name:
                pending_question = PendingQuestion(
                    request_id=f"question-{uuid4().hex}",
                    tool_name=plan_tool_call.tool_name,
                    arguments=dict(plan_tool_call.arguments),
                    prompts=QuestionTool.parse_prompts(plan_tool_call.arguments),
                )
                waiting_session = self._session_with_plan_state(
                    SessionState(
                        session=session.session,
                        status="waiting",
                        turn=session.turn,
                        metadata=session.metadata,
                    ),
                    status="waiting_question",
                    blocked_tool=pending_question.tool_name,
                )
                sequence += 1
                yield RuntimeStreamChunk(
                    kind="event",
                    session=waiting_session,
                    event=EventEnvelope(
                        session_id=session.session.id,
                        sequence=sequence,
                        event_type=RUNTIME_QUESTION_REQUESTED,
                        source="runtime",
                        payload={
                            "request_id": pending_question.request_id,
                            "tool": pending_question.tool_name,
                            "question_count": len(pending_question.prompts),
                            "questions": [
                                {
                                    "header": prompt.header,
                                    "question": prompt.question,
                                    "multiple": prompt.multiple,
                                    "options": [
                                        {
                                            "label": option.label,
                                            "description": option.description,
                                        }
                                        for option in prompt.options
                                    ],
                                }
                                for prompt in pending_question.prompts
                            ],
                        },
                    ),
                )
                return

            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type="runtime.tool_completed",
                    source="tool",
                    payload={
                        "tool": tool_result.tool_name,
                        "status": tool_result.status,
                        "content": tool_result.content,
                        "error": tool_result.error,
                        **tool_result.data,
                    },
                ),
            )

            post_hook_outcome = self._run_tool_hooks(
                session=session,
                sequence=sequence,
                tool_name=plan_tool_call.tool_name,
                phase="post",
            )
            yield from post_hook_outcome.chunks
            sequence = post_hook_outcome.last_sequence
            if post_hook_outcome.failed_error is not None:
                yield self._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=post_hook_outcome.failed_error,
                )
                raise RuntimeError(post_hook_outcome.failed_error)

            tool_results.append(tool_result)

    def _run_lifecycle_hooks(
        self,
        *,
        session: SessionState,
        sequence: int,
        surface: RuntimeHookSurface,
        payload: dict[str, object] | None = None,
    ) -> _HookOutcome:
        outcome: HookExecutionOutcome = run_lifecycle_hooks(
            LifecycleHookExecutionRequest(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session_id=session.session.id,
                surface=surface,
                recursion_env_var=self._hook_recursion_env_var,
                environment=os.environ,
                sequence_start=sequence,
                payload=payload or {},
            )
        )
        emitted_chunks = tuple(
            RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=event.sequence,
                    event_type=event.event_type,
                    source="runtime",
                    payload=event.payload,
                ),
            )
            for event in outcome.events
        )
        return _HookOutcome(
            chunks=emitted_chunks,
            last_sequence=outcome.last_sequence,
            failed_error=outcome.failed_error,
        )

    def _run_tool_hooks(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_name: str,
        phase: Literal["pre", "post"],
    ) -> _HookOutcome:
        outcome: HookExecutionOutcome = run_tool_hooks(
            HookExecutionRequest(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session_id=session.session.id,
                tool_name=tool_name,
                phase=phase,
                recursion_env_var=self._hook_recursion_env_var,
                environment=os.environ,
                sequence_start=sequence,
            )
        )
        emitted_chunks = tuple(
            RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=event.sequence,
                    event_type=event.event_type,
                    source="runtime",
                    payload=event.payload,
                ),
            )
            for event in outcome.events
        )
        return _HookOutcome(
            chunks=emitted_chunks,
            last_sequence=outcome.last_sequence,
            failed_error=outcome.failed_error,
        )

    def _failed_chunk(
        self,
        *,
        session: SessionState,
        sequence: int,
        error: str,
        payload: dict[str, object] | None = None,
    ) -> RuntimeStreamChunk:
        failed_session = self._session_with_plan_state(
            SessionState(
                session=session.session,
                status="failed",
                turn=session.turn,
                metadata=session.metadata,
            ),
            status="failed",
            error=error,
        )
        failure_payload = {"error": error, **(payload or {})}
        return RuntimeStreamChunk(
            kind="event",
            session=failed_session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence,
                event_type="runtime.failed",
                source="runtime",
                payload=failure_payload,
            ),
        )

    def _persist_response(self, *, request: RuntimeRequest, response: RuntimeResponse) -> None:
        if response.session.status == "waiting":
            pending_question = self._pending_question_from_response(response)
            if pending_question is not None:
                self._session_store.save_pending_question(
                    workspace=self._workspace,
                    request=request,
                    response=response,
                    pending_question=pending_question,
                )
                return
            pending_approval = self._pending_approval_from_response(response)
            self._session_store.save_pending_approval(
                workspace=self._workspace,
                request=request,
                response=response,
                pending_approval=pending_approval,
            )
            return
        self._session_store.save_run(workspace=self._workspace, request=request, response=response)

    def _resolve_permission(
        self,
        *,
        session: SessionState,
        tool: ToolDefinition,
        tool_call: ToolCall,
        sequence: int,
        permission_policy: PermissionPolicy,
    ) -> _PermissionOutcome:
        permission = resolve_permission(
            tool,
            tool_call,
            policy=permission_policy,
            owner_session_id=session.session.id,
            owner_parent_session_id=session.session.parent_id,
            delegated_task_id=(
                cast(str, session.metadata["background_task_id"])
                if isinstance(session.metadata.get("background_task_id"), str)
                else None
            ),
        )
        if tool.read_only:
            return _PermissionOutcome(
                chunks=(
                    RuntimeStreamChunk(
                        kind="event",
                        session=session,
                        event=EventEnvelope(
                            session_id=session.session.id,
                            sequence=sequence,
                            event_type="runtime.permission_resolved",
                            source="runtime",
                            payload={"tool": tool_call.tool_name, "decision": "allow"},
                        ),
                    ),
                ),
                last_sequence=sequence,
            )

        pending_approval = permission.pending_approval
        if pending_approval is None:
            raise ValueError("non-read-only permission decisions require pending approval data")
        if permission.decision in ("allow", "deny"):
            return self._approval_resolution_outcome(
                session=session,
                pending=pending_approval,
                decision=permission.decision,
                sequence=sequence,
            )

        pending = pending_approval
        waiting_session = self._session_with_plan_state(
            SessionState(
                session=session.session,
                status="waiting",
                turn=session.turn,
                metadata=session.metadata,
            ),
            status="waiting_approval",
            approval_request_id=pending.request_id,
            blocked_tool=pending.tool_name,
        )
        request_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type="runtime.approval_requested",
            source="runtime",
            payload={
                "request_id": pending.request_id,
                "tool": pending.tool_name,
                "decision": "ask",
                "arguments": pending.arguments,
                "target_summary": pending.target_summary,
                "reason": pending.reason,
                "policy": {"mode": pending.policy_mode},
                "owner_session_id": pending.owner_session_id,
                "owner_parent_session_id": pending.owner_parent_session_id,
                "delegated_task_id": pending.delegated_task_id,
            },
        )
        return _PermissionOutcome(
            chunks=(
                RuntimeStreamChunk(kind="event", session=waiting_session, event=request_event),
            ),
            last_sequence=sequence,
            pending_approval=pending_approval,
        )

    def _approval_resolution_outcome(
        self,
        *,
        session: SessionState,
        pending: PendingApproval,
        decision: PermissionResolution,
        sequence: int,
    ) -> _PermissionOutcome:
        resolution_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type="runtime.approval_resolved",
            source="runtime",
            payload={"request_id": pending.request_id, "decision": decision},
        )
        if decision == "deny":
            failed_session = self._session_with_plan_state(
                SessionState(
                    session=session.session,
                    status="failed",
                    turn=session.turn,
                    metadata=session.metadata,
                ),
                status="failed",
                error=f"permission denied for tool: {pending.tool_name}",
            )
            failed_event = EventEnvelope(
                session_id=session.session.id,
                sequence=sequence + 1,
                event_type="runtime.failed",
                source="runtime",
                payload={"error": f"permission denied for tool: {pending.tool_name}"},
            )
            return _PermissionOutcome(
                chunks=(
                    RuntimeStreamChunk(kind="event", session=session, event=resolution_event),
                    RuntimeStreamChunk(kind="event", session=failed_session, event=failed_event),
                ),
                last_sequence=sequence + 1,
                denied=True,
            )
        return _PermissionOutcome(
            chunks=(
                RuntimeStreamChunk(
                    kind="event",
                    session=self._session_with_plan_state(session, status="in_progress"),
                    event=resolution_event,
                ),
            ),
            last_sequence=sequence,
        )

    def list_sessions(self) -> tuple[StoredSessionSummary, ...]:
        return self._session_store.list_sessions(workspace=self._workspace)

    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState:
        self._reconcile_background_tasks_if_needed()
        validated_request = self._validated_request(request)
        task_id = f"task-{uuid4().hex}"
        initial_state = BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="queued",
            request=BackgroundTaskRequestSnapshot(
                prompt=validated_request.prompt,
                session_id=validated_request.session_id,
                parent_session_id=validated_request.parent_session_id,
                metadata=dict(validated_request.metadata),
                allocate_session_id=validated_request.allocate_session_id,
            ),
        )
        self._session_store.create_background_task(workspace=self._workspace, task=initial_state)
        worker = threading.Thread(
            target=self._run_background_task_worker,
            args=(task_id,),
            name=f"voidcode-background-task-{task_id}",
            daemon=True,
        )
        self._background_task_threads[task_id] = worker
        worker.start()
        return self.load_background_task(task_id)

    def load_background_task(self, task_id: str) -> BackgroundTaskState:
        self._reconcile_background_tasks_if_needed()
        validate_background_task_id(task_id)
        return self._session_store.load_background_task(workspace=self._workspace, task_id=task_id)

    def load_background_task_result(self, task_id: str) -> BackgroundTaskResult:
        task = self.load_background_task(task_id)
        self._backfill_parent_background_task_event(task=task)
        return self._background_task_result(task=task)

    def list_background_tasks(self) -> tuple[StoredBackgroundTaskSummary, ...]:
        self._reconcile_background_tasks_if_needed()
        return self._session_store.list_background_tasks(workspace=self._workspace)

    def list_background_tasks_by_parent_session(
        self, *, parent_session_id: str
    ) -> tuple[StoredBackgroundTaskSummary, ...]:
        self._reconcile_background_tasks_if_needed()
        validated_parent_session_id = validate_session_reference_id(
            parent_session_id,
            field_name="parent_session_id",
        )
        return self._session_store.list_background_tasks_by_parent_session(
            workspace=self._workspace,
            parent_session_id=validated_parent_session_id,
        )

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        validate_background_task_id(task_id)
        self._reconcile_background_tasks_if_needed()
        previous_task = self._session_store.load_background_task(
            workspace=self._workspace,
            task_id=task_id,
        )
        task = self._session_store.request_background_task_cancel(
            workspace=self._workspace,
            task_id=task_id,
        )
        if task.status == "running" and task.session_id is not None:
            child_response = self._load_background_task_child_response(task=task)
            if child_response is not None and child_response.session.status == "waiting":
                self._session_store.clear_pending_approval(
                    workspace=self._workspace,
                    session_id=task.session_id,
                )
                self._session_store.clear_pending_question(
                    workspace=self._workspace,
                    session_id=task.session_id,
                )
                cancelled_metadata = dict(child_response.session.metadata)
                cancelled_metadata["abort_requested"] = True
                cancelled_response = RuntimeResponse(
                    session=SessionState(
                        session=child_response.session.session,
                        status="failed",
                        turn=child_response.session.turn,
                        metadata=cancelled_metadata,
                    ),
                    events=child_response.events
                    + (
                        EventEnvelope(
                            session_id=task.session_id,
                            sequence=(
                                child_response.events[-1].sequence if child_response.events else 0
                            )
                            + 1,
                            event_type=RUNTIME_FAILED,
                            source="runtime",
                            payload={
                                "error": "cancelled by parent while child session was waiting",
                                "cancelled": True,
                                "delegated_task_id": task.task.id,
                            },
                        ),
                    ),
                    output=child_response.output,
                )
                self._session_store.save_run(
                    workspace=self._workspace,
                    request=RuntimeRequest(
                        prompt=self._prompt_from_events(child_response.events),
                        session_id=task.session_id,
                        parent_session_id=task.parent_session_id,
                        metadata=cast(RuntimeRequestMetadataPayload, cancelled_metadata),
                    ),
                    response=cancelled_response,
                )
                task = self._session_store.mark_background_task_terminal(
                    workspace=self._workspace,
                    task_id=task_id,
                    status="cancelled",
                    error="cancelled by parent while child session was waiting",
                )
        if previous_task.status != "cancelled" and task.status == "cancelled":
            self._run_background_task_lifecycle_hook(task)
        return task

    def session_result(self, *, session_id: str) -> RuntimeSessionResult:
        validate_session_id(session_id)
        result = self._session_store.load_session_result(
            workspace=self._workspace,
            session_id=session_id,
        )
        self._validate_session_workspace(result.session, session_id=session_id)
        self._reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
        result = self._session_store.load_session_result(
            workspace=self._workspace,
            session_id=session_id,
        )
        self._validate_session_workspace(result.session, session_id=session_id)
        return result

    def list_notifications(self) -> tuple[RuntimeNotification, ...]:
        notifications = self._session_store.list_notifications(workspace=self._workspace)
        return tuple(
            notification
            for notification in notifications
            if self._session_belongs_to_workspace(notification.session.id)
        )

    def acknowledge_notification(self, *, notification_id: str) -> RuntimeNotification:
        if not notification_id:
            raise ValueError("notification_id must be a non-empty string")
        notification = self._session_store.acknowledge_notification(
            workspace=self._workspace,
            notification_id=notification_id,
        )
        if not self._session_belongs_to_workspace(notification.session.id):
            raise ValueError(f"unknown notification: {notification_id}")
        return notification

    def effective_runtime_config(self, *, session_id: str | None = None) -> EffectiveRuntimeConfig:
        if session_id is None:
            return self._effective_runtime_config_from_metadata(None)
        validate_session_id(session_id)
        response = self._session_store.load_session(
            workspace=self._workspace, session_id=session_id
        )
        self._validate_session_workspace(response.session, session_id=session_id)
        return self._effective_runtime_config_from_metadata(response.session.metadata)

    def refresh_provider_models(self, provider_name: str) -> tuple[str, ...]:
        if not provider_name or "/" in provider_name:
            raise ValueError("provider_name must be a non-empty provider id without '/'")
        _ = self._model_provider_registry.resolve(provider_name)
        return self._model_provider_registry.refresh_available_models(provider_name)

    def provider_models(self, provider_name: str) -> tuple[str, ...]:
        if not provider_name or "/" in provider_name:
            raise ValueError("provider_name must be a non-empty provider id without '/'")
        return self._model_provider_registry.available_models(provider_name)

    def provider_model_catalog(self, provider_name: str) -> dict[str, object] | None:
        if not provider_name or "/" in provider_name:
            raise ValueError("provider_name must be a non-empty provider id without '/'")
        catalog = self._model_provider_registry.provider_catalog(provider_name)
        if catalog is None:
            return None
        return {
            "provider": catalog.provider,
            "models": list(catalog.models),
            "refreshed": catalog.refreshed,
            "source": catalog.source,
            "last_refresh_status": catalog.last_refresh_status,
            "last_error": catalog.last_error,
        }

    def web_settings(self) -> dict[str, object]:
        settings = load_global_web_settings()
        effective_config = self._effective_runtime_config_from_metadata(None)
        return {
            "provider": settings.provider,
            "provider_api_key_present": settings.provider_api_key_present,
            "model": effective_config.model,
        }

    def update_web_settings(
        self,
        *,
        provider: str | None = None,
        provider_api_key: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]:
        save_global_web_settings(
            RuntimeWebSettings(
                provider=provider,
                provider_api_key=provider_api_key,
            )
        )
        if model is not None:
            config_path = self._workspace / ".voidcode.json"
            payload = self._read_json_object(config_path)
            payload["model"] = model
            config_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self._reload_runtime_config_state()
        return self.web_settings()

    @staticmethod
    def _read_json_object(config_path: Path) -> dict[str, object]:
        if not config_path.exists():
            return {}
        raw_payload = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw_payload, dict):
            raise ValueError(f"runtime config file must contain a JSON object: {config_path}")
        return cast(dict[str, object], raw_payload)

    def _reload_runtime_config_state(self) -> None:
        self._config = load_runtime_config(self._workspace)
        self._model_provider_registry = ModelProviderRegistry.with_defaults(
            provider_configs=self._config.providers
        )
        self._provider_auth_resolver = ProviderAuthResolver(
            providers=self._config.providers,
            env=os.environ,
        )
        initial_agent = self._config.agent
        if initial_agent is not None:
            initial_agent = parse_runtime_agent_payload(
                serialize_runtime_agent_config(initial_agent),
                source="runtime config agent",
            )
            assert initial_agent is not None
            self._validate_runtime_agent_for_execution(
                initial_agent,
                source="runtime config agent",
            )
        initial_model = (
            initial_agent.model
            if initial_agent is not None and initial_agent.model is not None
            else self._config.model
        )
        initial_execution_engine = (
            initial_agent.execution_engine
            if initial_agent is not None and initial_agent.execution_engine is not None
            else self._config.execution_engine
        )
        initial_provider_fallback = (
            initial_agent.provider_fallback
            if initial_agent is not None and initial_agent.provider_fallback is not None
            else self._config.provider_fallback
        )
        self._resolved_provider_config = resolve_provider_config(
            initial_model,
            initial_provider_fallback,
            registry=self._model_provider_registry,
        )
        self._provider_model = self._resolved_provider_config.active_target
        self._provider_chain = self._resolved_provider_config.target_chain
        self._initial_effective_config = EffectiveRuntimeConfig(
            approval_mode=self._config.approval_mode,
            model=initial_model,
            execution_engine=initial_execution_engine,
            max_steps=self._config.max_steps,
            tool_timeout_seconds=self._config.tool_timeout_seconds,
            provider_fallback=initial_provider_fallback,
            plan=self._config.plan,
            resolved_provider=self._resolved_provider_config,
            agent=initial_agent,
        )
        self._graph_cache = {}
        self._graph = self._graph_override or self._build_graph_for_engine_from_config(
            self._initial_effective_config
        )

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> RuntimeResponse:
        validate_session_id(session_id)
        if approval_request_id is None and approval_decision is None:
            response = self._session_store.load_session(
                workspace=self._workspace, session_id=session_id
            )
            self._validate_session_workspace(response.session, session_id=session_id)
            self._reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
            response = self._session_store.load_session(
                workspace=self._workspace, session_id=session_id
            )
            self._validate_session_workspace(response.session, session_id=session_id)
            return response
        if approval_request_id is None or approval_decision is None:
            raise ValueError("approval resume requires request id and decision")
        self._validate_resume_targets_owned_request(
            session_id=session_id,
            approval_request_id=approval_request_id,
        )
        _, response = self._resume_pending_approval_response(
            session_id=session_id,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
        )
        self._finalize_background_task_from_session_response(session_response=response)
        return response

    def resume_stream(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        validate_session_id(session_id)
        if approval_request_id is None and approval_decision is None:
            response = self._session_store.load_session(
                workspace=self._workspace, session_id=session_id
            )
            self._validate_session_workspace(response.session, session_id=session_id)
            self._reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
            response = self._session_store.load_session(
                workspace=self._workspace, session_id=session_id
            )
            self._validate_session_workspace(response.session, session_id=session_id)
            yield from self._replay_response(response)
            return
        if approval_request_id is None or approval_decision is None:
            raise ValueError("approval resume requires request id and decision")
        self._validate_resume_targets_owned_request(
            session_id=session_id,
            approval_request_id=approval_request_id,
        )
        yield from self._resume_pending_approval_stream(
            session_id=session_id,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
            finalize_background_task=True,
        )

    def _validate_resume_targets_owned_request(
        self,
        *,
        session_id: str,
        approval_request_id: str,
    ) -> None:
        list_by_parent = cast(
            Callable[..., tuple[StoredBackgroundTaskSummary, ...]] | None,
            getattr(
                self._session_store,
                "list_background_tasks_by_parent_session",
                None,
            ),
        )
        if callable(list_by_parent):
            for task_summary in list_by_parent(
                workspace=self._workspace,
                parent_session_id=session_id,
            ):
                task = self._session_store.load_background_task(
                    workspace=self._workspace,
                    task_id=task_summary.task.id,
                )
                child_response = self._load_background_task_child_response(task=task)
                owned_request_id = (
                    self._approval_request_id_from_waiting_response(child_response)
                    if child_response is not None
                    else task.approval_request_id
                )
                if owned_request_id == approval_request_id:
                    raise ValueError(
                        "approval resume must target the child session that owns "
                        "the approval request"
                    )

        pending = self._session_store.load_pending_approval(
            workspace=self._workspace,
            session_id=session_id,
        )
        if pending is None:
            return
        if pending.owner_session_id is not None and pending.owner_session_id != session_id:
            raise ValueError(
                "approval resume must target the child session that owns the approval request"
            )
        if pending.request_id != approval_request_id:
            return

    def answer_question(
        self,
        session_id: str,
        *,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> RuntimeResponse:
        validate_session_id(session_id)
        _, response = self._answer_pending_question_response(
            session_id=session_id,
            question_request_id=question_request_id,
            responses=responses,
        )
        self._finalize_background_task_from_session_response(session_response=response)
        return response

    def answer_question_stream(
        self,
        session_id: str,
        *,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> Iterator[RuntimeStreamChunk]:
        validate_session_id(session_id)
        yield from self._answer_pending_question_stream(
            session_id=session_id,
            question_request_id=question_request_id,
            responses=responses,
            finalize_background_task=True,
        )

    def _pending_approval_from_response(self, response: RuntimeResponse) -> PendingApproval:
        approval_event = next(
            (
                event
                for event in reversed(response.events)
                if event.event_type == "runtime.approval_requested"
            ),
            None,
        )
        if approval_event is None:
            raise ValueError("waiting runtime response must include an approval event")
        payload = approval_event.payload
        raw_policy = cast(dict[str, object], payload.get("policy", {}))
        raw_policy_mode = raw_policy.get("mode", "ask")
        if raw_policy_mode not in ("allow", "deny", "ask"):
            raise ValueError(f"invalid approval policy mode: {raw_policy_mode}")
        return PendingApproval(
            request_id=str(payload["request_id"]),
            tool_name=str(payload["tool"]),
            arguments=cast(dict[str, object], payload.get("arguments", {})),
            target_summary=str(payload.get("target_summary", "")),
            reason=str(payload.get("reason", "")),
            policy_mode=raw_policy_mode,
            owner_session_id=(
                str(payload["owner_session_id"])
                if payload.get("owner_session_id") is not None
                else None
            ),
            owner_parent_session_id=(
                str(payload["owner_parent_session_id"])
                if payload.get("owner_parent_session_id") is not None
                else None
            ),
            delegated_task_id=(
                str(payload["delegated_task_id"])
                if payload.get("delegated_task_id") is not None
                else None
            ),
        )

    def _pending_question_from_response(self, response: RuntimeResponse) -> PendingQuestion | None:
        answered_request_ids = {
            str(event.payload.get("request_id"))
            for event in response.events
            if event.event_type == RUNTIME_QUESTION_ANSWERED and event.payload.get("request_id")
        }
        for event in reversed(response.events):
            if event.event_type != RUNTIME_QUESTION_REQUESTED:
                continue
            payload = event.payload
            request_id = str(payload["request_id"])
            if request_id in answered_request_ids:
                continue
            raw_questions = payload.get("questions")
            if not isinstance(raw_questions, list):
                raise ValueError("waiting runtime response must include question prompts")
            return PendingQuestion(
                request_id=request_id,
                tool_name=str(payload.get("tool", QuestionTool.definition.name)),
                arguments={},
                prompts=QuestionTool.parse_prompts(
                    {"questions": cast(list[object], raw_questions)}
                ),
            )
        return None

    def _resume_pending_approval_stream(
        self,
        *,
        session_id: str,
        approval_request_id: str,
        approval_decision: PermissionResolution,
        finalize_background_task: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        stored_response = self._session_store.load_session(
            workspace=self._workspace, session_id=session_id
        )
        pending = self._session_store.load_pending_approval(
            workspace=self._workspace, session_id=session_id
        )
        checkpoint = self._load_resume_checkpoint(session_id=session_id)
        if pending is None:
            raise ValueError(f"no pending approval for session: {session_id}")
        if pending.request_id != approval_request_id:
            raise ValueError("approval request id does not match pending session approval")
        streamed_events: list[EventEnvelope] = []
        output: str | None = None
        final_session: SessionState | None = None
        for chunk in self._resume_pending_approval_impl(
            stored=stored_response,
            pending=pending,
            approval_decision=approval_decision,
            checkpoint=checkpoint,
        ):
            final_session = chunk.session
            if chunk.event is not None:
                streamed_events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output
            yield chunk
        if finalize_background_task:
            response = self._response_from_resumed_chunks(
                stored_response=stored_response,
                streamed_events=streamed_events,
                output=output,
                final_session=final_session,
            )
            self._finalize_background_task_from_session_response(session_response=response)

    def _resume_pending_approval_response(
        self,
        *,
        session_id: str,
        approval_request_id: str,
        approval_decision: PermissionResolution,
    ) -> tuple[tuple[EventEnvelope, ...], RuntimeResponse]:
        stored_response = self._session_store.load_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        pending = self._session_store.load_pending_approval(
            workspace=self._workspace, session_id=session_id
        )
        checkpoint = self._load_resume_checkpoint(session_id=session_id)
        if pending is None:
            raise ValueError(f"no pending approval for session: {session_id}")
        if pending.request_id != approval_request_id:
            raise ValueError("approval request id does not match pending session approval")

        stored_events = stored_response.events
        streamed_events: list[EventEnvelope] = []
        output: str | None = None
        final_session: SessionState | None = None

        for chunk in self._resume_pending_approval_impl(
            stored=stored_response,
            pending=pending,
            approval_decision=approval_decision,
            checkpoint=checkpoint,
        ):
            final_session = chunk.session
            if chunk.event is not None:
                streamed_events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output

        response = self._response_from_resumed_chunks(
            stored_response=stored_response,
            streamed_events=streamed_events,
            output=output,
            final_session=final_session,
        )
        return stored_events, response

    def _answer_pending_question_stream(
        self,
        *,
        session_id: str,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
        finalize_background_task: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        stored_response = self._session_store.load_session(
            workspace=self._workspace, session_id=session_id
        )
        pending = self._session_store.load_pending_question(
            workspace=self._workspace, session_id=session_id
        )
        checkpoint = self._load_resume_checkpoint(session_id=session_id)
        if pending is None:
            raise NoPendingQuestionError(f"no pending question for session: {session_id}")
        if pending.request_id != question_request_id:
            raise ValueError("question request id does not match pending session question")
        normalized_responses = QuestionTool.validate_responses(pending.prompts, responses)
        streamed_events: list[EventEnvelope] = []
        output: str | None = None
        final_session: SessionState | None = None
        for chunk in self._answer_pending_question_impl(
            stored=stored_response,
            pending=pending,
            responses=normalized_responses,
            checkpoint=checkpoint,
        ):
            final_session = chunk.session
            if chunk.event is not None:
                streamed_events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output
            yield chunk
        if finalize_background_task:
            response = self._response_from_resumed_chunks(
                stored_response=stored_response,
                streamed_events=streamed_events,
                output=output,
                final_session=final_session,
            )
            self._finalize_background_task_from_session_response(session_response=response)

    def _answer_pending_question_response(
        self,
        *,
        session_id: str,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> tuple[tuple[EventEnvelope, ...], RuntimeResponse]:
        stored_response = self._session_store.load_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        pending = self._session_store.load_pending_question(
            workspace=self._workspace, session_id=session_id
        )
        checkpoint = self._load_resume_checkpoint(session_id=session_id)
        if pending is None:
            raise NoPendingQuestionError(f"no pending question for session: {session_id}")
        if pending.request_id != question_request_id:
            raise ValueError("question request id does not match pending session question")
        normalized_responses = QuestionTool.validate_responses(pending.prompts, responses)

        streamed_events: list[EventEnvelope] = []
        output: str | None = None
        final_session: SessionState | None = None

        for chunk in self._answer_pending_question_impl(
            stored=stored_response,
            pending=pending,
            responses=normalized_responses,
            checkpoint=checkpoint,
        ):
            final_session = chunk.session
            if chunk.event is not None:
                streamed_events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output

        response = self._response_from_resumed_chunks(
            stored_response=stored_response,
            streamed_events=streamed_events,
            output=output,
            final_session=final_session,
        )
        return stored_response.events, response

    def _answer_pending_question_impl(
        self,
        *,
        stored: RuntimeResponse,
        pending: PendingQuestion,
        responses: tuple[QuestionResponse, ...],
        checkpoint: dict[str, object] | None,
    ) -> Iterator[RuntimeStreamChunk]:
        session = SessionState(
            session=stored.session.session,
            status="running",
            turn=stored.session.turn,
            metadata=stored.session.metadata,
        )
        max_stored_sequence = stored.events[-1].sequence if stored.events else 0
        question_answer_result = QuestionTool.answer_tool_result(responses)

        checkpoint_state = self._question_resume_state_from_checkpoint(
            checkpoint=checkpoint,
            pending=pending,
            stored_metadata=stored.session.metadata,
        )
        if checkpoint_state is not None:
            prompt = checkpoint_state.prompt
            session = SessionState(
                session=stored.session.session,
                status="running",
                turn=stored.session.turn,
                metadata=checkpoint_state.session_metadata,
            )
            tool_results: list[ToolResult] = list(checkpoint_state.tool_results)
        else:
            prompt = self._prompt_from_events(stored.events)
            tool_results = []
            for event in stored.events:
                if event.event_type == "runtime.tool_completed":
                    error_value = event.payload.get("error")
                    raw_content = event.payload.get("content")
                    is_err = error_value is not None
                    tool_results.append(
                        ToolResult(
                            tool_name=str(event.payload.get("tool", "unknown")),
                            content=(
                                str(raw_content) if raw_content is not None and not is_err else None
                            ),
                            status="error" if is_err else "ok",
                            data=event.payload,
                            error=str(error_value) if is_err else None,
                        )
                    )
        tool_results.append(question_answer_result)

        sequence = max_stored_sequence + 1
        answered_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type=RUNTIME_QUESTION_ANSWERED,
            source="runtime",
            payload={
                "request_id": pending.request_id,
                "responses": [
                    {"header": response.header, "answers": list(response.answers)}
                    for response in responses
                ],
            },
        )
        yield RuntimeStreamChunk(kind="event", session=session, event=answered_event)
        sequence += 1
        loop_events = [answered_event]
        tool_completed_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type="runtime.tool_completed",
            source="tool",
            payload={
                "tool": question_answer_result.tool_name,
                "status": question_answer_result.status,
                "content": question_answer_result.content,
                "error": question_answer_result.error,
                **question_answer_result.data,
            },
        )
        yield RuntimeStreamChunk(kind="event", session=session, event=tool_completed_event)
        loop_events.append(tool_completed_event)

        effective_config = self._effective_runtime_config_from_metadata(session.metadata)
        tool_registry = self._tool_registry_for_effective_config(effective_config)
        skill_registry = self._skill_registry_for_effective_config(effective_config)
        resumed_skill_snapshot = self._build_skill_snapshot(
            skill_registry,
            metadata=session.metadata,
            agent=effective_config.agent,
            source="resume",
        )
        graph_request = GraphRunRequest(
            session=session,
            prompt=prompt,
            available_tools=tool_registry.definitions(),
            applied_skills=resumed_skill_snapshot.applied_skill_payloads,
            skill_prompt_context=resumed_skill_snapshot.skill_prompt_context,
            context_window=self._prepare_provider_context_window(
                prompt=prompt,
                tool_results=tuple(tool_results),
                session_metadata=session.metadata,
            ),
            metadata={
                **session.metadata,
                "agent_preset": serialize_runtime_agent_config(effective_config.agent),
                "provider_attempt": (
                    session.metadata.get("provider_attempt", 0)
                    if isinstance(session.metadata.get("provider_attempt", 0), int)
                    else 0
                ),
            },
        )
        graph = self._graph_for_session_metadata(session.metadata)
        output: str | None = None
        final_session = session
        last_sequence = sequence
        try:
            for chunk in self._execute_graph_loop(
                graph=graph,
                tool_registry=tool_registry,
                session=session,
                sequence=sequence,
                graph_request=graph_request,
                tool_results=tool_results,
                permission_policy=self._permission_policy_for_session(session.metadata),
                preserved_continuity_state=self._continuity_state_from_session_metadata(
                    session.metadata
                ),
            ):
                final_session = chunk.session
                if chunk.event is not None:
                    last_sequence = chunk.event.sequence
                    loop_events.append(chunk.event)
                if chunk.kind == "output":
                    output = chunk.output
                yield chunk
        except Exception:
            if final_session.status == "failed":
                response = RuntimeResponse(
                    session=final_session,
                    events=stored.events + tuple(loop_events),
                    output=output,
                )
                request = RuntimeRequest(
                    prompt=prompt,
                    session_id=stored.session.session.id,
                    parent_session_id=stored.session.session.parent_id,
                )
                self._persist_response(request=request, response=response)
                return
            raise

        if final_session.status == "waiting":
            final_session = self._disconnect_acp_for_session_state(final_session)
            waiting_response = RuntimeResponse(
                session=final_session,
                events=stored.events + tuple(loop_events),
                output=output,
            )
            idle_reason = self._resume_waiting_reason(waiting_response)
            idle_hook_outcome = self._run_lifecycle_hooks(
                session=final_session,
                sequence=last_sequence,
                surface="session_idle",
                payload={"reason": idle_reason, "resume": True},
            )
            for hook_chunk in idle_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                last_sequence = hook_event.sequence
                loop_events.append(hook_event)
                yield hook_chunk
            if idle_hook_outcome.failed_error is not None:
                failed_chunk = self._failed_chunk(
                    session=final_session,
                    sequence=idle_hook_outcome.last_sequence + 1,
                    error=idle_hook_outcome.failed_error,
                )
                failed_event = cast(EventEnvelope, failed_chunk.event)
                loop_events.append(failed_event)
                final_session = failed_chunk.session
                yield failed_chunk
        else:
            final_chunks, final_session, final_sequence = self._finalize_run_acp(
                session=final_session,
                sequence=last_sequence,
            )
            for chunk in final_chunks:
                if chunk.event is not None:
                    last_sequence += 1
                    resequenced_event = self._resequence_event(chunk.event, sequence=last_sequence)
                    loop_events.append(resequenced_event)
                    yield RuntimeStreamChunk(
                        kind="event", session=chunk.session, event=resequenced_event
                    )
            end_hook_outcome = self._run_lifecycle_hooks(
                session=final_session,
                sequence=max(last_sequence, final_sequence),
                surface="session_end",
                payload={"session_status": final_session.status, "resume": True},
            )
            for hook_chunk in end_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                last_sequence = hook_event.sequence
                loop_events.append(hook_event)
                yield hook_chunk
            if end_hook_outcome.failed_error is not None:
                logger.warning(
                    "session_end hook failed for %s during question resume: %s",
                    final_session.session.id,
                    end_hook_outcome.failed_error,
                )

        response = RuntimeResponse(
            session=final_session,
            events=stored.events + tuple(loop_events),
            output=output,
        )
        request = RuntimeRequest(
            prompt=prompt,
            session_id=stored.session.session.id,
            parent_session_id=stored.session.session.parent_id,
        )
        self._persist_response(request=request, response=response)

    def _resume_waiting_reason(self, response: RuntimeResponse) -> str:
        try:
            self._pending_approval_from_response(response)
        except ValueError:
            pass
        else:
            return "waiting_for_approval"
        if self._pending_question_from_response(response) is not None:
            return "waiting_for_question"
        return "waiting"

    def _response_from_resumed_chunks(
        self,
        *,
        stored_response: RuntimeResponse,
        streamed_events: list[EventEnvelope],
        output: str | None,
        final_session: SessionState | None,
    ) -> RuntimeResponse:
        if final_session is None:
            raise ValueError("runtime stream emitted no chunks")
        if final_session.status == "waiting":
            final_session = self._reload_persisted_session(session_id=final_session.session.id)
        return RuntimeResponse(
            session=final_session,
            events=stored_response.events + tuple(streamed_events),
            output=output,
        )

    def _resume_pending_approval_impl(
        self,
        *,
        stored: RuntimeResponse,
        pending: PendingApproval,
        approval_decision: PermissionResolution,
        checkpoint: dict[str, object] | None,
    ) -> Iterator[RuntimeStreamChunk]:
        session = SessionState(
            session=stored.session.session,
            status="running",
            turn=stored.session.turn,
            metadata=stored.session.metadata,
        )

        max_stored_sequence = stored.events[-1].sequence if stored.events else 0
        loop_events: list[EventEnvelope] = []
        output: str | None = None

        checkpoint_state = self._approval_resume_state_from_checkpoint(
            checkpoint=checkpoint,
            pending=pending,
            stored_metadata=stored.session.metadata,
        )
        binding_mismatch_payload: dict[str, object] | None = None
        if checkpoint is not None:
            checkpoint_binding = checkpoint.get("skill_binding_snapshot")
            checkpoint_binding_payload = (
                cast(dict[str, object], checkpoint_binding)
                if isinstance(checkpoint_binding, dict)
                else None
            )
            if checkpoint_binding_payload is not None:
                stored_snapshot_payload = cast(
                    dict[str, object] | None,
                    stored.session.metadata.get("skill_snapshot"),
                )
                stored_binding_payload = (
                    cast(dict[str, object], stored_snapshot_payload.get("binding_snapshot"))
                    if isinstance(stored_snapshot_payload, dict)
                    and isinstance(stored_snapshot_payload.get("binding_snapshot"), dict)
                    else None
                )
                mismatch_payload = self._skill_binding_mismatch_payload(
                    checkpoint_binding_payload,
                    stored_binding_payload,
                )
                if cast(bool, mismatch_payload["mismatch"]):
                    binding_mismatch_payload = mismatch_payload
        if checkpoint_state is not None:
            prompt = checkpoint_state.prompt
            session = SessionState(
                session=stored.session.session,
                status="running",
                turn=stored.session.turn,
                metadata=checkpoint_state.session_metadata,
            )
            tool_results: list[ToolResult] = list(checkpoint_state.tool_results)
        else:
            prompt = self._prompt_from_events(stored.events)
            tool_results = []
            for event in stored.events:
                if event.event_type == "runtime.tool_completed":
                    error_value = event.payload.get("error")
                    raw_content = event.payload.get("content")
                    is_err = error_value is not None
                    tool_results.append(
                        ToolResult(
                            tool_name=str(event.payload.get("tool", "unknown")),
                            content=(
                                str(raw_content) if raw_content is not None and not is_err else None
                            ),
                            status="error" if is_err else "ok",
                            data=event.payload,
                            error=str(error_value) if is_err else None,
                        )
                    )

        session = self._session_with_current_acp_metadata(session)
        preserved_continuity_state = self._continuity_state_from_session_metadata(session.metadata)
        mcp_startup_chunks, session, _, mcp_failed_chunk = self._refresh_mcp_tools_for_session(
            session=session,
            sequence=max_stored_sequence,
            failure_kind="mcp_startup_failed",
        )
        effective_config = self._effective_runtime_config_from_metadata(session.metadata)
        tool_registry = self._tool_registry_for_effective_config(effective_config)
        skill_registry = self._skill_registry_for_effective_config(effective_config)

        resumed_skill_snapshot = self._build_skill_snapshot(
            skill_registry,
            metadata=session.metadata,
            agent=effective_config.agent,
            source="resume",
        )
        resumed_applied_skills = resumed_skill_snapshot.applied_skill_payloads
        graph_request = GraphRunRequest(
            session=session,
            prompt=prompt,
            available_tools=tool_registry.definitions(),
            applied_skills=resumed_applied_skills,
            skill_prompt_context=resumed_skill_snapshot.skill_prompt_context,
            context_window=self._prepare_provider_context_window(
                prompt=prompt,
                tool_results=tuple(tool_results),
                session_metadata=session.metadata,
            ),
            metadata={
                **session.metadata,
                "agent_preset": serialize_runtime_agent_config(effective_config.agent),
                "provider_attempt": (
                    session.metadata.get("provider_attempt", 0)
                    if isinstance(session.metadata.get("provider_attempt", 0), int)
                    else 0
                ),
            },
        )
        provider_attempt = self._provider_attempt_from_metadata(graph_request.metadata)
        graph = self._graph_for_session_metadata(session.metadata)
        if provider_attempt > 0:
            graph = self._graph_selection_for_effective_config(
                self._effective_runtime_config_from_metadata(session.metadata),
                provider_attempt=provider_attempt,
            ).graph

        emitted_sequence = max_stored_sequence
        if binding_mismatch_payload is not None:
            emitted_sequence += 1
            mismatch_event = EventEnvelope(
                session_id=session.session.id,
                sequence=emitted_sequence,
                event_type=RUNTIME_SKILLS_BINDING_MISMATCH,
                source="runtime",
                payload={
                    **binding_mismatch_payload,
                    "resume": True,
                    "approval_request_id": pending.request_id,
                },
            )
            loop_events.append(mismatch_event)
            yield RuntimeStreamChunk(kind="event", session=session, event=mismatch_event)
        for chunk in mcp_startup_chunks:
            emitted_sequence += 1
            resequenced_event = self._resequence_event(
                cast(EventEnvelope, chunk.event), sequence=emitted_sequence
            )
            resequenced_chunk = RuntimeStreamChunk(
                kind="event", session=chunk.session, event=resequenced_event
            )
            loop_events.append(resequenced_event)
            yield resequenced_chunk
        if mcp_failed_chunk is not None:
            emitted_sequence += 1
            resequenced_failed = self._resequence_event(
                cast(EventEnvelope, mcp_failed_chunk.event), sequence=emitted_sequence
            )
            failed_chunk = RuntimeStreamChunk(
                kind="event",
                session=mcp_failed_chunk.session,
                event=resequenced_failed,
            )
            loop_events.append(resequenced_failed)
            response = RuntimeResponse(
                session=mcp_failed_chunk.session,
                events=stored.events + tuple(loop_events),
                output=output,
            )
            request = RuntimeRequest(
                prompt=prompt,
                session_id=stored.session.session.id,
                parent_session_id=stored.session.session.parent_id,
            )
            self._persist_response(request=request, response=response)
            yield failed_chunk
            return

        deferred_startup_acp_events: tuple[object, ...] = ()
        if self.current_acp_state().configuration.configured_enabled is True:
            try:
                deferred_startup_acp_events = self._acp_adapter.connect()
            except Exception as exc:
                startup_chunks, session, last_sequence = self._emit_current_acp_drain(
                    session=session,
                    start_sequence=max_stored_sequence + 1,
                )
                startup_failed_chunk = self._failed_chunk(
                    session=self._session_with_current_acp_metadata(session),
                    sequence=last_sequence + 1,
                    error=str(exc),
                    payload={"kind": "acp_startup_failed"},
                )
            else:
                session = self._session_with_current_acp_metadata(session)
                startup_chunks = ()
                startup_failed_chunk = None
        else:
            startup_chunks = ()
            startup_failed_chunk = None
        for chunk in startup_chunks:
            emitted_sequence += 1
            resequenced_event = self._resequence_event(
                cast(EventEnvelope, chunk.event), sequence=emitted_sequence
            )
            resequenced_chunk = RuntimeStreamChunk(
                kind="event", session=chunk.session, event=resequenced_event
            )
            loop_events.append(resequenced_event)
            yield resequenced_chunk
        if startup_failed_chunk is not None:
            emitted_sequence += 1
            resequenced_failed = self._resequence_event(
                cast(EventEnvelope, startup_failed_chunk.event),
                sequence=emitted_sequence,
            )
            failed_chunk = RuntimeStreamChunk(
                kind="event",
                session=startup_failed_chunk.session,
                event=resequenced_failed,
            )
            loop_events.append(resequenced_failed)
            response = RuntimeResponse(
                session=startup_failed_chunk.session,
                events=stored.events + tuple(loop_events),
                output=output,
            )
            request = RuntimeRequest(
                prompt=prompt,
                session_id=stored.session.session.id,
                parent_session_id=stored.session.session.parent_id,
            )
            self._persist_response(request=request, response=response)
            yield failed_chunk
            return

        sequence = max_stored_sequence
        try:
            for chunk in self._execute_graph_loop(
                graph=graph,
                tool_registry=tool_registry,
                session=session,
                sequence=sequence,
                graph_request=graph_request,
                tool_results=tool_results,
                approval_resolution=(pending, approval_decision),
                permission_policy=self._permission_policy_for_session(session.metadata),
                preserved_continuity_state=preserved_continuity_state,
            ):
                if deferred_startup_acp_events and (
                    (
                        chunk.event is not None
                        and chunk.event.event_type
                        in {"runtime.approval_resolved", "runtime.failed"}
                    )
                    or chunk.kind == "output"
                ):
                    startup_chunks, updated_session, _ = self._emit_acp_events(
                        session=chunk.session,
                        start_sequence=emitted_sequence + 1,
                        acp_events=deferred_startup_acp_events,
                    )
                    deferred_startup_acp_events = ()
                    for startup_chunk in startup_chunks:
                        startup_event = cast(EventEnvelope, startup_chunk.event)
                        emitted_sequence = startup_event.sequence
                        loop_events.append(startup_event)
                        yield startup_chunk
                    if chunk.event is not None:
                        chunk = RuntimeStreamChunk(
                            kind="event",
                            session=updated_session,
                            event=chunk.event,
                        )
                    elif chunk.kind == "output":
                        chunk = RuntimeStreamChunk(
                            kind="output",
                            session=updated_session,
                            output=chunk.output,
                        )
                if chunk.event is not None:
                    emitted_sequence += 1
                    resequenced_event = self._resequence_event(
                        chunk.event, sequence=emitted_sequence
                    )
                    loop_events.append(resequenced_event)
                    yield RuntimeStreamChunk(
                        kind="event", session=chunk.session, event=resequenced_event
                    )
                if chunk.kind == "output":
                    output = chunk.output
                    yield chunk
                session = chunk.session
        except Exception:
            if session.status == "failed":
                response = RuntimeResponse(
                    session=session,
                    events=stored.events + tuple(loop_events),
                    output=output,
                )
                request = RuntimeRequest(
                    prompt=prompt,
                    session_id=stored.session.session.id,
                    parent_session_id=stored.session.session.parent_id,
                )
                self._persist_response(request=request, response=response)
                return
            raise

        if deferred_startup_acp_events:
            startup_chunks, session, _ = self._emit_acp_events(
                session=session,
                start_sequence=emitted_sequence + 1,
                acp_events=deferred_startup_acp_events,
            )
            deferred_startup_acp_events = ()
            for startup_chunk in startup_chunks:
                startup_event = cast(EventEnvelope, startup_chunk.event)
                emitted_sequence = startup_event.sequence
                loop_events.append(startup_event)
                yield startup_chunk

        last_sequence = emitted_sequence
        if session.status == "waiting":
            session = self._disconnect_acp_for_session_state(session)
            idle_hook_outcome = self._run_lifecycle_hooks(
                session=session,
                sequence=last_sequence,
                surface="session_idle",
                payload={"reason": "waiting_for_approval", "resume": True},
            )
            for hook_chunk in idle_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                emitted_sequence = hook_event.sequence
                loop_events.append(hook_event)
                yield hook_chunk
            if idle_hook_outcome.failed_error is not None:
                failed_chunk = self._failed_chunk(
                    session=session,
                    sequence=idle_hook_outcome.last_sequence + 1,
                    error=idle_hook_outcome.failed_error,
                )
                failed_event = cast(EventEnvelope, failed_chunk.event)
                loop_events.append(failed_event)
                session = failed_chunk.session
                yield failed_chunk
        else:
            final_chunks, session, _ = self._finalize_run_acp(
                session=session,
                sequence=last_sequence,
            )
            for chunk in final_chunks:
                if chunk.event is not None:
                    emitted_sequence += 1
                    resequenced_event = self._resequence_event(
                        chunk.event, sequence=emitted_sequence
                    )
                    loop_events.append(resequenced_event)
                    yield RuntimeStreamChunk(
                        kind="event", session=chunk.session, event=resequenced_event
                    )
            end_hook_outcome = self._run_lifecycle_hooks(
                session=session,
                sequence=emitted_sequence,
                surface="session_end",
                payload={"session_status": session.status, "resume": True},
            )
            for hook_chunk in end_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                emitted_sequence = hook_event.sequence
                loop_events.append(hook_event)
                yield hook_chunk
            if end_hook_outcome.failed_error is not None:
                logger.warning(
                    "session_end hook failed for %s during resume: %s",
                    session.session.id,
                    end_hook_outcome.failed_error,
                )

        response = RuntimeResponse(
            session=session,
            events=stored.events + tuple(loop_events),
            output=output,
        )

        request = RuntimeRequest(
            prompt=prompt,
            session_id=stored.session.session.id,
            parent_session_id=stored.session.session.parent_id,
        )
        self._persist_response(request=request, response=response)
        return

    def _approval_resume_state_from_checkpoint(
        self,
        *,
        checkpoint: dict[str, object] | None,
        pending: PendingApproval,
        stored_metadata: dict[str, object],
    ) -> _ApprovalResumeCheckpointState | None:
        if checkpoint is None:
            return None
        if checkpoint.get("kind") != "approval_wait":
            return None
        if checkpoint.get("version") != 1:
            return None
        if checkpoint.get("pending_approval_request_id") != pending.request_id:
            return None
        checkpoint_snapshot_hash = checkpoint.get("skill_snapshot_hash")
        stored_snapshot_payload = cast(
            dict[str, object] | None,
            stored_metadata.get("skill_snapshot"),
        )
        stored_snapshot_hash = (
            stored_snapshot_payload.get("snapshot_hash")
            if isinstance(stored_snapshot_payload, dict)
            else None
        )
        if (
            checkpoint_snapshot_hash is not None
            and stored_snapshot_hash is not None
            and checkpoint_snapshot_hash != stored_snapshot_hash
        ):
            return None
        prompt = checkpoint.get("prompt")
        session_metadata = checkpoint.get("session_metadata")
        raw_tool_results = checkpoint.get("tool_results")
        if not isinstance(prompt, str) or not isinstance(session_metadata, dict):
            return None
        if cast(dict[str, object], session_metadata) != stored_metadata:
            return None
        if not isinstance(raw_tool_results, list):
            return None
        checkpoint_tool_results = cast(list[object], raw_tool_results)
        try:
            tool_results = self._tool_results_from_checkpoint(checkpoint_tool_results)
        except (TypeError, ValueError):
            return None
        return _ApprovalResumeCheckpointState(
            prompt=prompt,
            session_metadata=cast(dict[str, object], session_metadata),
            tool_results=tool_results,
        )

    def _question_resume_state_from_checkpoint(
        self,
        *,
        checkpoint: dict[str, object] | None,
        pending: PendingQuestion,
        stored_metadata: dict[str, object],
    ) -> _ApprovalResumeCheckpointState | None:
        if checkpoint is None:
            return None
        if checkpoint.get("kind") != "question_wait":
            return None
        if checkpoint.get("version") != 1:
            return None
        if checkpoint.get("pending_question_request_id") != pending.request_id:
            return None
        prompt = checkpoint.get("prompt")
        session_metadata = checkpoint.get("session_metadata")
        raw_tool_results = checkpoint.get("tool_results")
        if not isinstance(prompt, str) or not isinstance(session_metadata, dict):
            return None
        if cast(dict[str, object], session_metadata) != stored_metadata:
            return None
        if not isinstance(raw_tool_results, list):
            return None
        checkpoint_tool_results = cast(list[object], raw_tool_results)
        try:
            tool_results = self._tool_results_from_checkpoint(checkpoint_tool_results)
        except (TypeError, ValueError):
            return None
        return _ApprovalResumeCheckpointState(
            prompt=prompt,
            session_metadata=cast(dict[str, object], session_metadata),
            tool_results=tool_results,
        )

    def _load_resume_checkpoint(self, *, session_id: str) -> dict[str, object] | None:
        load_checkpoint = getattr(self._session_store, "load_resume_checkpoint", None)
        if load_checkpoint is None:
            return None
        return cast(
            dict[str, object] | None,
            load_checkpoint(workspace=self._workspace, session_id=session_id),
        )

    @staticmethod
    def _tool_results_from_checkpoint(raw_tool_results: list[object]) -> tuple[ToolResult, ...]:
        parsed: list[ToolResult] = []
        for raw_tool_result in raw_tool_results:
            if not isinstance(raw_tool_result, dict):
                raise ValueError("persisted resume checkpoint tool_results must contain objects")
            payload = cast(dict[str, object], raw_tool_result)
            tool_name = payload.get("tool_name")
            status = payload.get("status")
            data = payload.get("data")
            content = payload.get("content")
            error = payload.get("error")
            if (
                not isinstance(tool_name, str)
                or status not in ("ok", "error")
                or not isinstance(data, dict)
            ):
                raise ValueError("persisted resume checkpoint tool_results are malformed")
            if content is not None and not isinstance(content, str):
                raise ValueError(
                    "persisted resume checkpoint tool result content must be a string or null"
                )
            if error is not None and not isinstance(error, str):
                raise ValueError(
                    "persisted resume checkpoint tool result error must be a string or null"
                )
            tool_status: ToolResultStatus = status
            parsed.append(
                ToolResult(
                    tool_name=tool_name,
                    content=content,
                    status=tool_status,
                    data=cast(dict[str, object], data),
                    error=error,
                )
            )
        return tuple(parsed)

    @staticmethod
    def _replay_response(response: RuntimeResponse) -> Iterator[RuntimeStreamChunk]:
        for event in response.events:
            yield RuntimeStreamChunk(
                kind="event",
                session=VoidCodeRuntime._replayed_chunk_session(
                    response_session=response.session,
                    event=event,
                ),
                event=event,
            )
        if response.output is not None:
            yield RuntimeStreamChunk(
                kind="output",
                session=VoidCodeRuntime._session_with_status(
                    response.session,
                    "completed"
                    if response.session.status == "completed"
                    else response.session.status,
                ),
                output=response.output,
            )

    @staticmethod
    def _session_with_status(session: SessionState, status: SessionStatus) -> SessionState:
        return SessionState(
            session=session.session,
            status=status,
            turn=session.turn,
            metadata=session.metadata,
        )

    @staticmethod
    def _replayed_chunk_session(
        *, response_session: SessionState, event: EventEnvelope
    ) -> SessionState:
        status: SessionStatus = "running"
        if event.event_type in {"runtime.approval_requested", "runtime.question_requested"}:
            status = "waiting"
        elif event.event_type == "runtime.failed":
            status = "failed"
        elif event.event_type == "runtime.acp_disconnected" and response_session.status in {
            "failed",
            "completed",
        }:
            status = response_session.status
        elif response_session.status == "completed" and (
            event.event_type == "graph.response_ready"
            or (event.event_type == "graph.loop_step" and event.payload.get("phase") == "finalize")
        ):
            status = "completed"
        return VoidCodeRuntime._session_with_status(response_session, status)

    def _validated_request(
        self,
        request: RuntimeRequest,
        *,
        allow_internal_metadata: bool = False,
    ) -> RuntimeRequest:
        session_id = request.session_id
        if session_id is not None:
            session_id = validate_session_id(session_id)

        parent_session_id = request.parent_session_id
        if parent_session_id is not None:
            parent_session_id = validate_session_reference_id(
                parent_session_id,
                field_name="parent_session_id",
            )
        if session_id is not None and parent_session_id == session_id:
            raise RuntimeRequestError("parent_session_id must not match session_id")

        metadata = validate_runtime_request_metadata(
            dict(request.metadata),
            allow_internal_fields=allow_internal_metadata,
        )
        existing_session = (
            self._load_existing_session_if_present(session_id=session_id)
            if session_id is not None
            else None
        )
        governance_parent_session_id = parent_session_id
        if governance_parent_session_id is None and existing_session is not None:
            governance_parent_session_id = existing_session.session.session.parent_id
        metadata = self._metadata_with_resolved_subagent_route(
            metadata,
            allow_internal_fields=allow_internal_metadata,
        )
        metadata = self._metadata_with_delegation_governance(
            metadata,
            parent_session_id=governance_parent_session_id,
            existing_session_id=session_id if existing_session is not None else None,
        )
        if parent_session_id is not None:
            parent_session = self._load_existing_session_if_present(session_id=parent_session_id)
            if parent_session is None and not self._is_active_session_id(parent_session_id):
                raise RuntimeRequestError(f"parent session does not exist: {parent_session_id}")

        resolved_parent_session_id = parent_session_id
        if existing_session is not None:
            existing_parent_session_id = existing_session.session.session.parent_id
            if parent_session_id is None:
                resolved_parent_session_id = existing_parent_session_id
            elif existing_parent_session_id != parent_session_id:
                existing_parent_label = (
                    existing_parent_session_id
                    if existing_parent_session_id is not None
                    else "<top-level>"
                )
                raise RuntimeRequestError(
                    f"session {session_id} already belongs to {existing_parent_label} "
                    f"and cannot be rebound to parent session {parent_session_id}"
                )

        return RuntimeRequest(
            prompt=request.prompt,
            session_id=session_id,
            parent_session_id=resolved_parent_session_id,
            metadata=metadata,
            allocate_session_id=request.allocate_session_id,
        )

    def _metadata_with_delegation_governance(
        self,
        metadata: RuntimeRequestMetadataPayload,
        *,
        parent_session_id: str | None,
        existing_session_id: str | None,
    ) -> RuntimeRequestMetadataPayload:
        raw_delegation = metadata.get("delegation")
        if not isinstance(raw_delegation, dict):
            return metadata

        normalized = dict(cast(dict[str, object], metadata))
        delegation = dict(cast(dict[str, object], raw_delegation))
        parent_depth = 0
        remaining_spawn_budget = _DELEGATION_GOVERNANCE.spawn_budget

        if parent_session_id is not None:
            parent_response = self._load_existing_session_if_present(session_id=parent_session_id)
            if parent_response is not None:
                parent_depth = self._delegation_depth_from_metadata(
                    parent_response.session.metadata
                )
                remaining_spawn_budget = self._remaining_spawn_budget_from_metadata(
                    parent_response.session.metadata
                )
            elif (
                active_parent_metadata := self._active_session_metadata(parent_session_id)
            ) is not None:
                parent_depth = self._delegation_depth_from_metadata(active_parent_metadata)
                remaining_spawn_budget = self._remaining_spawn_budget_from_metadata(
                    active_parent_metadata
                )

        request_depth = parent_depth + 1
        if request_depth > _DELEGATION_GOVERNANCE.max_depth:
            raise RuntimeRequestError(
                "delegation depth limit exceeded: "
                f"requested depth {request_depth} exceeds max {_DELEGATION_GOVERNANCE.max_depth}"
            )

        if existing_session_id is None:
            if remaining_spawn_budget < 1:
                raise RuntimeRequestError("delegation spawn budget exhausted for parent session")
            remaining_spawn_budget -= 1

        delegation["depth"] = request_depth
        delegation["remaining_spawn_budget"] = remaining_spawn_budget
        normalized["delegation"] = delegation
        return validate_runtime_request_metadata(
            normalized,
            allow_internal_fields=(
                "background_run" in normalized or "background_task_id" in normalized
            ),
        )

    @staticmethod
    def _delegation_depth_from_metadata(metadata: dict[str, object] | None) -> int:
        if metadata is None:
            return 0
        raw_delegation = metadata.get("delegation")
        if not isinstance(raw_delegation, dict):
            return 0
        delegation = cast(dict[str, object], raw_delegation)
        return max(0, _coerce_int_like(delegation.get("depth"), 0))

    @staticmethod
    def _remaining_spawn_budget_from_metadata(metadata: dict[str, object] | None) -> int:
        if metadata is None:
            return _DELEGATION_GOVERNANCE.spawn_budget
        raw_delegation = metadata.get("delegation")
        if not isinstance(raw_delegation, dict):
            return _DELEGATION_GOVERNANCE.spawn_budget
        delegation = cast(dict[str, object], raw_delegation)
        remaining = _coerce_int_like(
            delegation.get("remaining_spawn_budget"),
            _DELEGATION_GOVERNANCE.spawn_budget,
        )
        return max(0, remaining)

    @staticmethod
    def _resolve_session_id(request: RuntimeRequest) -> str:
        return resolve_runtime_session_routing(request).session_id

    @staticmethod
    def _prompt_from_events(events: tuple[EventEnvelope, ...]) -> str:
        if not events:
            return ""
        prompt = events[0].payload.get("prompt")
        if isinstance(prompt, str):
            return prompt
        return ""

    @staticmethod
    def _provider_attempt_from_metadata(metadata: dict[str, object]) -> int:
        raw_provider_attempt = metadata.get("provider_attempt", 0)
        return raw_provider_attempt if isinstance(raw_provider_attempt, int) else 0

    def _prepare_provider_context_window(
        self,
        *,
        prompt: str,
        tool_results: tuple[ToolResult, ...],
        session_metadata: dict[str, object],
        policy: ContextWindowPolicy | None = None,
    ) -> RuntimeContextWindow:
        return prepare_provider_context(
            prompt=prompt,
            tool_results=tool_results,
            session_metadata=session_metadata,
            policy=policy or self._default_context_window_policy,
        )

    @staticmethod
    def _continuity_state_from_session_metadata(
        session_metadata: dict[str, object],
    ) -> RuntimeContinuityState | None:
        runtime_state = session_metadata.get("runtime_state")
        if not isinstance(runtime_state, dict):
            return None
        runtime_state_payload = cast(dict[str, object], runtime_state)
        continuity = runtime_state_payload.get("continuity")
        if not isinstance(continuity, dict):
            return None
        continuity_payload = cast(dict[str, object], continuity)
        summary_text = continuity_payload.get("summary_text")
        dropped = continuity_payload.get("dropped_tool_result_count")
        retained = continuity_payload.get("retained_tool_result_count")
        source = continuity_payload.get("source")
        if summary_text is not None and not isinstance(summary_text, str):
            return None
        if not isinstance(dropped, int) or isinstance(dropped, bool):
            return None
        if not isinstance(retained, int) or isinstance(retained, bool):
            return None
        if not isinstance(source, str):
            return None
        version = continuity_payload.get("version")
        if version is not None and (not isinstance(version, int) or isinstance(version, bool)):
            return None
        return RuntimeContinuityState(
            summary_text=summary_text,
            dropped_tool_result_count=dropped,
            retained_tool_result_count=retained,
            source=source,
            version=version if version is not None else 1,
        )

    @staticmethod
    def _session_with_context_window_metadata(
        session: SessionState, context_window: RuntimeContextWindow
    ) -> SessionState:
        raw_runtime_state = session.metadata.get("runtime_state")
        runtime_state = (
            dict(cast(dict[str, object], raw_runtime_state))
            if isinstance(raw_runtime_state, dict)
            else {}
        )
        continuity_payload = (
            context_window.continuity_state.metadata_payload()
            if context_window.continuity_state is not None
            else None
        )
        return SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata={
                **session.metadata,
                "context_window": context_window.metadata_payload(),
                "runtime_state": {
                    **runtime_state,
                    **(
                        {"continuity": continuity_payload} if continuity_payload is not None else {}
                    ),
                },
            },
        )

    @staticmethod
    def _renumber_events(
        events: tuple[GraphEvent, ...], *, session_id: str, start_sequence: int
    ) -> tuple[EventEnvelope, ...]:
        return tuple(
            EventEnvelope(
                session_id=session_id,
                sequence=start_sequence + index,
                event_type=event.event_type,
                source=event.source,
                payload=event.payload,
            )
            for index, event in enumerate(events)
        )

    @staticmethod
    def _loaded_skill_names(skill_registry: SkillRegistry) -> list[str]:
        return sorted(skill_registry.skills)

    @staticmethod
    def _validated_runtime_context_from_payload(payload: dict[str, str]) -> SkillRuntimeContext:
        try:
            return runtime_context_from_payload(payload)
        except ValueError as exc:
            source_path = payload.get("source_path", "")
            prefix = (
                f"persisted skill payload {source_path}"
                if source_path
                else "persisted skill payload"
            )
            raise ValueError(f"{prefix}: {exc}") from exc

    def _applied_skill_contexts(
        self,
        skill_registry: SkillRegistry,
        metadata: dict[str, object] | None = None,
        agent: RuntimeAgentConfig | None = None,
    ) -> tuple[SkillRuntimeContext, ...]:
        request_skill_names: tuple[str, ...] | None = None
        if metadata is not None and "skills" in metadata:
            raw_skills = metadata["skills"]
            if not isinstance(raw_skills, list):
                raise ValueError("request metadata 'skills' must be a list of skill names")
            parsed_names: list[str] = []
            for index, raw_name in enumerate(cast(list[object], raw_skills)):
                if not isinstance(raw_name, str) or not raw_name:
                    raise ValueError(
                        f"request metadata 'skills[{index}]' must be a non-empty string"
                    )
                parsed_names.append(raw_name)
            request_skill_names = tuple(parsed_names)

        persisted_selected_skill_names = (
            self._persisted_selected_skill_names(metadata) if metadata is not None else None
        )
        legacy_applied_skill_names = (
            self._legacy_applied_skill_names(metadata) if metadata is not None else None
        )
        if persisted_selected_skill_names is None and legacy_applied_skill_names is not None:
            if not legacy_applied_skill_names:
                return ()
            return self._available_runtime_contexts(skill_registry, legacy_applied_skill_names)
        selected_skill_names = self._selected_skill_names_for_agent(
            agent,
            request_skill_names=request_skill_names,
            persisted_selected_skill_names=persisted_selected_skill_names,
        )
        return build_runtime_contexts(skill_registry, skill_names=selected_skill_names)

    def _build_skill_snapshot(
        self,
        skill_registry: SkillRegistry,
        *,
        metadata: dict[str, object] | None,
        agent: RuntimeAgentConfig | None,
        source: Literal["run", "resume", "replay", "legacy"],
    ) -> SkillExecutionSnapshot:
        binding_snapshot = self._skill_binding_snapshot(metadata)
        if metadata is not None:
            persisted_snapshot = self._skill_snapshot_from_metadata(metadata)
            if persisted_snapshot is not None:
                normalized = persisted_snapshot
                if (
                    binding_snapshot is not None
                    and persisted_snapshot.binding_snapshot != binding_snapshot
                ):
                    normalized = snapshot_from_payload(
                        {
                            **snapshot_payload(normalized),
                            "binding_snapshot": binding_snapshot,
                        }
                    )
                if source != normalized.source:
                    return snapshot_from_payload(
                        {
                            **snapshot_payload(normalized),
                            "source": source,
                        }
                    )
                return normalized
        contexts = self._applied_skill_contexts(skill_registry, metadata, agent)
        return build_skill_execution_snapshot(
            contexts,
            source=source,
            binding_snapshot=binding_snapshot,
        )

    def _skill_binding_snapshot(
        self,
        metadata: dict[str, object] | None,
    ) -> dict[str, object] | None:
        source_runtime_config = None
        if metadata is not None:
            raw_runtime_config = metadata.get("runtime_config")
            if isinstance(raw_runtime_config, dict):
                source_runtime_config = cast(dict[str, object], raw_runtime_config)
        if source_runtime_config is None:
            source_runtime_config = self._runtime_config_metadata(
                self._effective_runtime_config_from_metadata(metadata)
            )
        snapshot: dict[str, object] = {}
        for key in _SKILL_BINDING_SCOPE_KEYS:
            if key in source_runtime_config:
                snapshot[key] = source_runtime_config[key]
        return snapshot

    @staticmethod
    def _skill_binding_mismatch_payload(
        expected: dict[str, object] | None,
        actual: dict[str, object] | None,
    ) -> dict[str, object]:
        expected_payload = expected if isinstance(expected, dict) else {}
        actual_payload = actual if isinstance(actual, dict) else {}
        keys = sorted(set(expected_payload.keys()) | set(actual_payload.keys()))
        mismatches = [key for key in keys if expected_payload.get(key) != actual_payload.get(key)]
        return {
            "mismatch": bool(mismatches),
            "mismatch_keys": mismatches,
            "expected_binding": expected_payload,
            "actual_binding": actual_payload,
        }

    @staticmethod
    def _snapshot_to_session_metadata(snapshot: SkillExecutionSnapshot) -> dict[str, object]:
        return {
            "selected_skill_names": list(snapshot.selected_skill_names),
            "applied_skills": list(snapshot.selected_skill_names),
            "applied_skill_payloads": [
                dict(payload) for payload in snapshot.applied_skill_payloads
            ],
            "skill_snapshot": snapshot_payload(snapshot),
        }

    def _skill_snapshot_from_metadata(
        self,
        metadata: dict[str, object],
    ) -> SkillExecutionSnapshot | None:
        raw_snapshot = metadata.get("skill_snapshot")
        has_payload_keys = "applied_skill_payloads" in metadata
        if isinstance(raw_snapshot, dict) and has_payload_keys:
            return snapshot_from_payload(cast(dict[str, object], raw_snapshot))
        persisted_payloads = self._persisted_applied_skill_payloads(metadata)
        if persisted_payloads is not None:
            contexts = tuple(
                self._validated_runtime_context_from_payload(payload)
                for payload in persisted_payloads
            )
            selected_names = self._persisted_selected_skill_names(metadata)
            return build_skill_execution_snapshot(
                contexts,
                source="legacy",
                selected_skill_names=selected_names if selected_names is not None else None,
            )
        return None

    @staticmethod
    def _selected_skill_names_for_agent(
        agent: RuntimeAgentConfig | None,
        *,
        request_skill_names: tuple[str, ...] | None,
        persisted_selected_skill_names: tuple[str, ...] | None = None,
    ) -> tuple[str, ...] | None:
        manifest_skill_refs: tuple[str, ...] = ()
        persisted_selected_explicit = persisted_selected_skill_names is not None
        if persisted_selected_skill_names is not None:
            manifest_skill_refs = persisted_selected_skill_names
        if agent is not None:
            if not persisted_selected_explicit and not manifest_skill_refs:
                manifest = get_builtin_agent_manifest(agent.preset)
                if manifest is not None:
                    manifest_skill_refs = manifest.skill_refs

        if request_skill_names is None:
            if persisted_selected_explicit:
                return manifest_skill_refs
            return manifest_skill_refs if manifest_skill_refs else None

        selected_names: list[str] = []
        for skill_name in (*manifest_skill_refs, *request_skill_names):
            if skill_name not in selected_names:
                selected_names.append(skill_name)
        return tuple(selected_names)

    @staticmethod
    def _fresh_request_metadata(metadata: RuntimeRequestMetadataPayload) -> dict[str, object]:
        sanitized = dict(metadata)
        sanitized.pop("applied_skills", None)
        sanitized.pop("applied_skill_payloads", None)
        sanitized.pop("selected_skill_names", None)
        sanitized.pop("skill_snapshot", None)
        return sanitized

    @staticmethod
    def _legacy_applied_skill_names(metadata: dict[str, object]) -> tuple[str, ...] | None:
        raw_applied = metadata.get("applied_skills")
        if not isinstance(raw_applied, list):
            return None
        names = [item for item in cast(list[object], raw_applied) if isinstance(item, str)]
        return tuple(names)

    @staticmethod
    def _persisted_selected_skill_names(
        metadata: dict[str, object],
    ) -> tuple[str, ...] | None:
        if "selected_skill_names" not in metadata:
            return None
        raw_skill_names = metadata["selected_skill_names"]
        if not isinstance(raw_skill_names, list):
            raise ValueError("persisted selected skill names must be a list")

        selected_skill_names: list[str] = []
        for index, raw_name in enumerate(cast(list[object], raw_skill_names)):
            if not isinstance(raw_name, str):
                raise ValueError(f"persisted selected skill names[{index}] must be a string")
            selected_skill_names.append(raw_name)
        return tuple(selected_skill_names)

    @staticmethod
    def _persisted_applied_skill_payloads(
        metadata: dict[str, object],
    ) -> tuple[dict[str, str], ...] | None:
        if "applied_skill_payloads" not in metadata:
            return None
        raw_payloads = metadata["applied_skill_payloads"]
        if not isinstance(raw_payloads, list):
            raise ValueError("persisted applied skill payloads must be a list")
        persisted_payloads = cast(list[object], raw_payloads)

        payloads: list[dict[str, str]] = []
        for raw_payload in persisted_payloads:
            if not isinstance(raw_payload, dict):
                raise ValueError("persisted applied skill payloads must contain objects")
            payload = cast(dict[str, object], raw_payload)
            name = payload.get("name")
            description = payload.get("description")
            content = payload.get("content")
            prompt_context = payload.get("prompt_context")
            execution_notes = payload.get("execution_notes")
            source_path = payload.get("source_path")
            if (
                not isinstance(name, str)
                or not isinstance(description, str)
                or not isinstance(content, str)
            ):
                raise ValueError("persisted applied skill payloads must include string fields")
            if not name.strip() or not description.strip() or not content.strip():
                raise ValueError("persisted applied skill payload string fields must not be empty")
            normalized_payload = {"name": name, "description": description, "content": content}
            if prompt_context is not None:
                if not isinstance(prompt_context, str):
                    raise ValueError(
                        "persisted applied skill payload prompt_context must be a string"
                    )
                if not prompt_context.strip():
                    raise ValueError(
                        "persisted applied skill payload prompt_context must not be empty"
                    )
                normalized_payload["prompt_context"] = prompt_context
            if execution_notes is not None:
                if not isinstance(execution_notes, str):
                    raise ValueError(
                        "persisted applied skill payload execution_notes must be a string"
                    )
                if not execution_notes.strip():
                    raise ValueError(
                        "persisted applied skill payload execution_notes must not be empty"
                    )
                normalized_payload["execution_notes"] = execution_notes
            if source_path is not None:
                if not isinstance(source_path, str):
                    raise ValueError("persisted applied skill payload source_path must be a string")
                normalized_payload["source_path"] = source_path
            payloads.append(normalized_payload)
        return tuple(payloads)

    @staticmethod
    def _available_runtime_contexts(
        skill_registry: SkillRegistry,
        skill_names: Iterable[str],
    ) -> tuple[SkillRuntimeContext, ...]:
        contexts: list[SkillRuntimeContext] = []
        for skill_name in skill_names:
            skill = skill_registry.skills.get(skill_name)
            if skill is None:
                continue
            contexts.append(build_runtime_context(skill))
        return tuple(contexts)

    def _runtime_config_metadata(
        self, config: EffectiveRuntimeConfig | None = None
    ) -> dict[str, object]:
        effective_config = config or self._effective_runtime_config_from_metadata(None)
        runtime_config_metadata: dict[str, object] = {
            "approval_mode": effective_config.approval_mode,
            "execution_engine": effective_config.execution_engine,
            "max_steps": effective_config.max_steps,
            "tool_timeout_seconds": effective_config.tool_timeout_seconds,
            "provider_fallback": serialize_provider_fallback_config(
                effective_config.provider_fallback
            ),
            "resolved_provider": resolved_provider_snapshot(effective_config.resolved_provider),
            "plan": serialize_runtime_plan_config(effective_config.plan),
        }
        if effective_config.model is not None:
            runtime_config_metadata["model"] = effective_config.model
        serialized_agent = serialize_runtime_agent_config(effective_config.agent)
        if serialized_agent is not None:
            runtime_config_metadata["agent"] = serialized_agent
        lsp_state = self._lsp_manager.current_state()
        runtime_config_metadata["lsp"] = {
            "mode": lsp_state.mode,
            "configured_enabled": lsp_state.configuration.configured_enabled,
            "servers": list(lsp_state.configuration.servers),
        }
        mcp_state = self._mcp_manager.current_state()
        runtime_config_metadata["mcp"] = {
            "mode": mcp_state.mode,
            "configured_enabled": mcp_state.configuration.configured_enabled,
            "servers": list(mcp_state.configuration.servers),
        }
        return runtime_config_metadata

    def _config_with_request_agent_override(
        self,
        resolved: EffectiveRuntimeConfig,
        raw_agent: object,
        *,
        allow_subagent_presets: bool = False,
    ) -> EffectiveRuntimeConfig:
        agent = parse_runtime_agent_payload(raw_agent, source="request metadata 'agent'")
        if agent is None:
            raise ValueError("request metadata 'agent' must be an object when provided")
        assert agent is not None
        self._validate_runtime_agent_for_execution(
            agent,
            source="request metadata 'agent'",
            allow_subagent_presets=allow_subagent_presets,
        )
        model = agent.model if agent.model is not None else resolved.model
        execution_engine = (
            agent.execution_engine
            if agent.execution_engine is not None
            else resolved.execution_engine
        )
        provider_fallback = (
            agent.provider_fallback
            if agent.provider_fallback is not None
            else resolved.provider_fallback
        )
        merged_agent = RuntimeAgentConfig(
            preset=agent.preset,
            prompt_profile=(
                agent.prompt_profile
                if agent.prompt_profile is not None
                else resolved.agent.prompt_profile
                if resolved.agent is not None
                else None
            ),
            model=model,
            execution_engine=execution_engine,
            tools=(
                agent.tools
                if agent.tools is not None
                else resolved.agent.tools
                if resolved.agent is not None
                else None
            ),
            skills=(
                agent.skills
                if agent.skills is not None
                else resolved.agent.skills
                if resolved.agent is not None
                else None
            ),
            provider_fallback=provider_fallback,
        )
        resolved_provider = resolve_provider_config(
            model,
            provider_fallback,
            registry=self._model_provider_registry,
        )
        return EffectiveRuntimeConfig(
            approval_mode=resolved.approval_mode,
            model=model,
            execution_engine=execution_engine,
            max_steps=resolved.max_steps,
            tool_timeout_seconds=resolved.tool_timeout_seconds,
            provider_fallback=provider_fallback,
            plan=resolved.plan,
            resolved_provider=resolved_provider,
            agent=merged_agent,
        )

    @staticmethod
    def _validate_runtime_agent_for_execution(
        agent: RuntimeAgentConfig,
        *,
        source: str,
        allow_subagent_presets: bool = False,
    ) -> None:
        if agent.preset in _EXECUTABLE_AGENT_PRESETS:
            return
        if allow_subagent_presets and agent.preset in _EXECUTABLE_SUBAGENT_PRESETS:
            return
        valid = ", ".join(sorted(_EXECUTABLE_AGENT_PRESETS))
        if allow_subagent_presets:
            valid = ", ".join(sorted((*_EXECUTABLE_AGENT_PRESETS, *_EXECUTABLE_SUBAGENT_PRESETS)))
        if allow_subagent_presets:
            raise ValueError(
                f"{source}: agent preset '{agent.preset}' is not executable for this "
                f"runtime delegation path; executable agent presets are: {valid}"
            )
        raise ValueError(
            f"{source}: agent preset '{agent.preset}' cannot be executed as the top-level "
            f"active agent in the current runtime; executable agent presets are: {valid}"
        )

    def _metadata_with_resolved_subagent_route(
        self,
        metadata: RuntimeRequestMetadataPayload,
        *,
        allow_internal_fields: bool,
    ) -> RuntimeRequestMetadataPayload:
        resolved_route = runtime_subagent_route_from_metadata(metadata)
        if resolved_route is None:
            return metadata

        normalized_metadata = dict(cast(dict[str, object], metadata))
        raw_delegation_metadata = normalized_metadata["delegation"]
        if not isinstance(raw_delegation_metadata, dict):
            raise RuntimeRequestError(
                "request metadata 'delegation' must be an object when provided"
            )
        delegation_metadata = dict(cast(dict[str, object], raw_delegation_metadata))
        delegation_metadata["selected_preset"] = resolved_route.selected_preset
        delegation_metadata["selected_execution_engine"] = resolved_route.execution_engine

        raw_agent = normalized_metadata.get("agent")
        if raw_agent is None:
            agent = parse_runtime_agent_payload(
                {
                    "preset": resolved_route.selected_preset,
                    "execution_engine": resolved_route.execution_engine,
                },
                source="delegation.selected_preset",
            )
            assert agent is not None
        else:
            agent = parse_runtime_agent_payload(raw_agent, source="request metadata 'agent'")
            if agent is None:
                raise RuntimeRequestError(
                    "request metadata 'agent' must be an object when provided"
                )
            if agent.preset != resolved_route.selected_preset:
                raise RuntimeRequestError(
                    "request metadata 'agent.preset' must match delegated child preset "
                    f"'{resolved_route.selected_preset}'"
                )
            if agent.execution_engine != resolved_route.execution_engine:
                raise RuntimeRequestError(
                    "request metadata 'agent.execution_engine' must match delegated child "
                    f"execution engine '{resolved_route.execution_engine}'"
                )

        self._validate_runtime_agent_for_execution(
            agent,
            source="delegated child agent",
            allow_subagent_presets=True,
        )
        serialized_agent = serialize_runtime_agent_config(agent)
        assert serialized_agent is not None
        normalized_metadata["delegation"] = delegation_metadata
        normalized_metadata["agent"] = serialized_agent
        return validate_runtime_request_metadata(
            normalized_metadata,
            allow_internal_fields=allow_internal_fields,
        )

    def _runtime_state_metadata(self) -> dict[str, object]:
        acp_state = self._acp_adapter.current_state()
        return {
            "acp": {
                "mode": acp_state.mode,
                "configured_enabled": acp_state.configuration.configured_enabled,
                "status": acp_state.status,
                "available": acp_state.available,
                "last_error": acp_state.last_error,
                "last_request_type": acp_state.last_request_type,
                "last_request_id": acp_state.last_request_id,
                "last_event_type": acp_state.last_event_type,
                "last_delegation": (
                    acp_state.last_delegation.as_payload()
                    if acp_state.last_delegation is not None
                    else None
                ),
            }
        }

    @staticmethod
    def _envelopes_for_lsp_events(
        *,
        session_id: str,
        start_sequence: int,
        lsp_events: tuple[object, ...],
    ) -> tuple[EventEnvelope, ...]:
        known_event_types = {
            RUNTIME_LSP_SERVER_STARTED,
            RUNTIME_LSP_SERVER_REUSED,
            RUNTIME_LSP_SERVER_STARTUP_REJECTED,
            RUNTIME_LSP_SERVER_STOPPED,
            RUNTIME_LSP_SERVER_FAILED,
        }
        envelopes: list[EventEnvelope] = []
        sequence = start_sequence
        for raw_event in lsp_events:
            if isinstance(raw_event, dict):
                raw_event_dict = cast(dict[str, object], raw_event)
                event_type = raw_event_dict.get("event_type")
                payload = raw_event_dict.get("payload")
            else:
                event_type = getattr(raw_event, "event_type", None)
                payload = getattr(raw_event, "payload", None)
            if event_type not in known_event_types or not isinstance(payload, dict):
                continue
            envelopes.append(
                EventEnvelope(
                    session_id=session_id,
                    sequence=sequence,
                    event_type=cast(str, event_type),
                    source="runtime",
                    payload=cast(dict[str, object], payload),
                )
            )
            sequence += 1
        return tuple(envelopes)

    @staticmethod
    def _envelopes_for_acp_events(
        *,
        session_id: str,
        start_sequence: int,
        acp_events: tuple[object, ...],
    ) -> tuple[EventEnvelope, ...]:
        known_event_types = {
            RUNTIME_ACP_CONNECTED,
            RUNTIME_ACP_DELEGATED_LIFECYCLE,
            RUNTIME_ACP_DISCONNECTED,
            RUNTIME_ACP_FAILED,
        }
        envelopes: list[EventEnvelope] = []
        sequence = start_sequence
        for raw_event in acp_events:
            acp_session_id: str | None = None
            acp_parent_session_id: str | None = None
            acp_delegation: AcpDelegatedExecution | None = None
            if isinstance(raw_event, dict):
                raw_event_dict = cast(dict[str, object], raw_event)
                event_type = raw_event_dict.get("event_type")
                payload = raw_event_dict.get("payload")
            else:
                event_type = getattr(raw_event, "event_type", None)
                payload = getattr(raw_event, "payload", None)
                acp_session_id = cast(str | None, getattr(raw_event, "session_id", None))
                acp_parent_session_id = cast(
                    str | None, getattr(raw_event, "parent_session_id", None)
                )
                acp_delegation = cast(
                    AcpDelegatedExecution | None,
                    getattr(raw_event, "delegation", None),
                )
            if event_type not in known_event_types or not isinstance(payload, dict):
                continue
            envelopes.append(
                EventEnvelope(
                    session_id=session_id,
                    sequence=sequence,
                    event_type=cast(str, event_type),
                    source="runtime",
                    payload={
                        **cast(dict[str, object], payload),
                        **(
                            {
                                "session_id": acp_session_id,
                                "parent_session_id": acp_parent_session_id,
                                "delegation": acp_delegation.as_payload(),
                            }
                            if acp_delegation is not None
                            else {
                                **(
                                    {"session_id": acp_session_id}
                                    if acp_session_id is not None
                                    else {}
                                ),
                                **(
                                    {"parent_session_id": acp_parent_session_id}
                                    if acp_parent_session_id is not None
                                    else {}
                                ),
                            }
                        ),
                    },
                )
            )
            sequence += 1
        return tuple(envelopes)

    @staticmethod
    def _envelopes_for_mcp_events(
        *,
        session_id: str,
        start_sequence: int,
        mcp_events: tuple[object, ...],
    ) -> tuple[EventEnvelope, ...]:
        known_event_types = {
            RUNTIME_MCP_SERVER_FAILED,
            RUNTIME_MCP_SERVER_STARTED,
            RUNTIME_MCP_SERVER_STOPPED,
        }
        envelopes: list[EventEnvelope] = []
        sequence = start_sequence
        for raw_event in mcp_events:
            if isinstance(raw_event, dict):
                raw_event_dict = cast(dict[str, object], raw_event)
                event_type = raw_event_dict.get("event_type")
                payload = raw_event_dict.get("payload")
            else:
                event_type = getattr(raw_event, "event_type", None)
                payload = getattr(raw_event, "payload", None)
            if event_type not in known_event_types or not isinstance(payload, dict):
                continue
            envelopes.append(
                EventEnvelope(
                    session_id=session_id,
                    sequence=sequence,
                    event_type=cast(str, event_type),
                    source="runtime",
                    payload=cast(dict[str, object], payload),
                )
            )
            sequence += 1
        return tuple(envelopes)

    def _permission_policy_for_session(
        self, metadata: dict[str, object] | None
    ) -> PermissionPolicy:
        approval_mode: PermissionDecision = self._permission_policy.mode
        if metadata is not None:
            persisted_runtime_config = metadata.get("runtime_config")
            if isinstance(persisted_runtime_config, dict):
                runtime_config = cast(dict[str, object], persisted_runtime_config)
                persisted_approval_mode = runtime_config.get("approval_mode")
                if persisted_approval_mode in ("allow", "deny", "ask"):
                    approval_mode = persisted_approval_mode
        return PermissionPolicy(mode=approval_mode)

    @staticmethod
    def _approval_request_id_from_waiting_response(response: RuntimeResponse) -> str | None:
        if response.session.status != "waiting":
            return None
        for event in reversed(response.events):
            if event.event_type in {RUNTIME_APPROVAL_REQUESTED, RUNTIME_QUESTION_REQUESTED}:
                request_id = event.payload.get("request_id")
                return str(request_id) if request_id is not None else None
        return None

    def _load_background_task_child_response(
        self,
        *,
        task: BackgroundTaskState,
    ) -> RuntimeResponse | None:
        child_session_id = task.session_id
        if child_session_id is None:
            return None
        try:
            response = self._session_store.load_session(
                workspace=self._workspace,
                session_id=child_session_id,
            )
        except UnknownSessionError:
            return None
        self._validate_session_workspace(response.session, session_id=child_session_id)
        return response

    def _load_background_task_child_result(
        self,
        *,
        task: BackgroundTaskState,
    ) -> RuntimeSessionResult | None:
        child_session_id = task.session_id
        if child_session_id is None:
            return None
        try:
            result = self._session_store.load_session_result(
                workspace=self._workspace,
                session_id=child_session_id,
            )
        except UnknownSessionError:
            return None
        self._validate_session_workspace(result.session, session_id=child_session_id)
        return result

    def _background_task_result(self, *, task: BackgroundTaskState) -> BackgroundTaskResult:
        child_result = self._load_background_task_child_result(task=task)
        approval_blocked = child_result is not None and child_result.status == "waiting"
        summary_output = child_result.summary if child_result is not None else None
        error = (
            child_result.error if child_result is not None and child_result.error else task.error
        )
        result_available = task.result_available
        if not result_available and task.status != "cancelled" and child_result is not None:
            result_available = True
        return BackgroundTaskResult(
            task_id=task.task.id,
            parent_session_id=task.parent_session_id,
            child_session_id=task.session_id,
            status=task.status,
            requested_child_session_id=task.request.session_id or task.session_id,
            routing=task.routing_identity,
            approval_request_id=task.approval_request_id,
            question_request_id=task.question_request_id,
            approval_blocked=approval_blocked,
            summary_output=summary_output,
            error=error,
            result_available=result_available,
            cancellation_cause=task.cancellation_cause,
        )

    def _emit_background_task_parent_terminal_event(self, *, task: BackgroundTaskState) -> None:
        parent_session_id = task.parent_session_id
        if parent_session_id is None or task.status not in ("completed", "failed", "cancelled"):
            return
        session_event_appender = self._session_store
        if not isinstance(session_event_appender, SessionEventAppender):
            logger.debug(
                "skipping background terminal parent event for session store without append support"
            )
            return
        result = self._background_task_result(task=task)
        event_type_by_status: dict[BackgroundTaskStatus, str] = {
            "completed": RUNTIME_BACKGROUND_TASK_COMPLETED,
            "failed": RUNTIME_BACKGROUND_TASK_FAILED,
            "cancelled": RUNTIME_BACKGROUND_TASK_CANCELLED,
        }
        event_type = event_type_by_status[task.status]
        payload: dict[str, object] = {
            "task_id": task.task.id,
            "parent_session_id": parent_session_id,
            "status": task.status,
            "result_available": result.result_available,
            "delegation": result.delegated_execution.as_payload(),
            "message": result.delegated_message.as_payload(),
        }
        if result.child_session_id is not None:
            payload["child_session_id"] = result.child_session_id
        if task.status == "completed" and result.summary_output is not None:
            payload["summary_output"] = result.summary_output
        if task.status in ("failed", "cancelled") and result.error is not None:
            payload["error"] = result.error
        if task.approval_request_id is not None:
            payload["approval_request_id"] = task.approval_request_id
        if task.question_request_id is not None:
            payload["question_request_id"] = task.question_request_id
        try:
            _ = session_event_appender.append_session_event(
                workspace=self._workspace,
                session_id=parent_session_id,
                event_type=event_type,
                source="runtime",
                payload=payload,
                dedupe_key=f"{event_type}:{task.task.id}",
            )
            self._append_parent_acp_delegated_lifecycle_event(
                task=task,
                lifecycle_status=task.status,
                result_available=result.result_available,
                payload=payload,
            )
            self._publish_delegated_acp_event(
                task=task,
                lifecycle_status=task.status,
                result_available=result.result_available,
                payload=payload,
            )
        except UnknownSessionError:
            logger.debug(
                "skipping background terminal event for unavailable parent session: %s",
                parent_session_id,
            )

    def _backfill_parent_background_task_event(self, *, task: BackgroundTaskState) -> None:
        if task.parent_session_id is None:
            return
        if task.status in ("completed", "failed", "cancelled"):
            self._emit_background_task_parent_terminal_event(task=task)
            return
        if task.status != "running":
            return
        child_response = self._load_background_task_child_response(task=task)
        if child_response is None or child_response.session.status != "waiting":
            return
        self._emit_background_task_waiting_approval(
            task=task,
            child_response=child_response,
        )

    def _reconcile_parent_background_task_events_for_session(
        self,
        *,
        parent_session_id: str,
    ) -> None:
        task_summaries = self._session_store.list_background_tasks_by_parent_session(
            workspace=self._workspace,
            parent_session_id=parent_session_id,
        )
        for task_summary in task_summaries:
            task = self._session_store.load_background_task(
                workspace=self._workspace,
                task_id=task_summary.task.id,
            )
            if task.status == "running" and task.session_id is not None:
                child_response = self._load_background_task_child_response(task=task)
                if child_response is not None and child_response.session.status in (
                    "waiting",
                    "completed",
                    "failed",
                ):
                    self._finalize_background_task_from_session_response(
                        session_response=child_response
                    )
                    continue
            self._backfill_parent_background_task_event(task=task)

    def _emit_background_task_waiting_approval(
        self,
        *,
        task: BackgroundTaskState,
        child_response: RuntimeResponse,
    ) -> None:
        parent_session_id = task.parent_session_id
        child_session_id = task.session_id
        if parent_session_id is None or child_session_id is None:
            return
        approval_request_id = self._approval_request_id_from_waiting_response(child_response)
        dedupe_key = (
            f"background_task_waiting_approval:{task.task.id}:{approval_request_id}"
            if approval_request_id is not None
            else f"background_task_waiting_approval:{task.task.id}:{child_session_id}"
        )
        session_event_appender = self._session_store
        if not isinstance(session_event_appender, SessionEventAppender):
            logger.debug(
                "skipping background waiting event for session store without append support"
            )
            return
        result = self._background_task_result(task=task)
        try:
            _ = session_event_appender.append_session_event(
                workspace=self._workspace,
                session_id=parent_session_id,
                event_type=RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
                source="runtime",
                payload={
                    "task_id": task.task.id,
                    "parent_session_id": parent_session_id,
                    "child_session_id": child_session_id,
                    "status": "running",
                    "approval_blocked": True,
                    "delegation": result.delegated_execution.as_payload(),
                    "message": result.delegated_message.as_payload(),
                    **(
                        {"approval_request_id": approval_request_id}
                        if approval_request_id is not None
                        else {}
                    ),
                },
                dedupe_key=dedupe_key,
            )
            acp_payload: dict[str, object] = {
                "task_id": task.task.id,
                "parent_session_id": parent_session_id,
                "child_session_id": child_session_id,
                "approval_request_id": approval_request_id,
                "status": "running",
                "approval_blocked": True,
            }
            self._append_parent_acp_delegated_lifecycle_event(
                task=task,
                lifecycle_status="waiting_approval",
                approval_blocked=True,
                payload=acp_payload,
            )
            self._publish_delegated_acp_event(
                task=task,
                lifecycle_status="waiting_approval",
                approval_blocked=True,
                payload=acp_payload,
            )
        except UnknownSessionError:
            logger.debug(
                "skipping background waiting event for unavailable parent session: %s",
                parent_session_id,
            )

    def _finalize_background_task_from_session_response(
        self,
        *,
        session_response: RuntimeResponse,
    ) -> None:
        metadata = session_response.session.metadata
        background_task_id = metadata.get("background_task_id")
        background_run = metadata.get("background_run")
        if not isinstance(background_task_id, str) or background_run is not True:
            return
        current_task = self._session_store.load_background_task(
            workspace=self._workspace,
            task_id=background_task_id,
        )
        if current_task.status == "cancelled":
            return
        if session_response.session.status == "waiting":
            if current_task.status in ("completed", "failed"):
                return
            self._emit_background_task_waiting_approval(
                task=current_task,
                child_response=session_response,
            )
            return
        terminal_status: BackgroundTaskStatus = (
            "completed" if session_response.session.status == "completed" else "failed"
        )
        if current_task.status == terminal_status:
            return
        error: str | None = None
        if terminal_status == "failed":
            for event in reversed(session_response.events):
                if event.event_type == RUNTIME_FAILED:
                    event_error = event.payload.get("error")
                    error = str(event_error) if event_error is not None else None
                    break
        terminal_task = self._session_store.mark_background_task_terminal(
            workspace=self._workspace,
            task_id=background_task_id,
            status=terminal_status,
            error=error,
        )
        self._run_background_task_lifecycle_hook(terminal_task)

    def _run_background_task_lifecycle_hook(self, task: BackgroundTaskState) -> None:
        surface_by_status: dict[BackgroundTaskStatus, RuntimeHookSurface] = {
            "completed": "background_task_completed",
            "failed": "background_task_failed",
            "cancelled": "background_task_cancelled",
        }
        surface = surface_by_status.get(task.status)
        if surface is None:
            return
        self._run_background_task_lifecycle_surface(
            task=task,
            surface=surface,
            session_id=task.session_id or task.request.session_id or "runtime",
        )
        self._emit_background_task_parent_terminal_event(task=task)
        if task.status == "completed" and task.parent_session_id is not None:
            self._run_background_task_lifecycle_surface(
                task=task,
                surface="delegated_result_available",
                session_id=task.parent_session_id,
                extra_payload={
                    "delegated_session_id": task.session_id or "",
                    "parent_session_id": task.parent_session_id,
                },
            )

    def _run_background_task_lifecycle_surface(
        self,
        *,
        task: BackgroundTaskState,
        surface: RuntimeHookSurface,
        session_id: str,
        extra_payload: dict[str, object] | None = None,
    ) -> None:
        outcome = run_lifecycle_hooks(
            LifecycleHookExecutionRequest(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session_id=session_id,
                surface=surface,
                recursion_env_var=self._hook_recursion_env_var,
                environment=os.environ,
                sequence_start=0,
                payload={
                    "background_task_id": task.task.id,
                    "background_task_status": task.status,
                    **({"background_task_error": task.error} if task.error is not None else {}),
                    **(extra_payload or {}),
                },
            )
        )
        if outcome.failed_error is not None:
            logger.warning("background task lifecycle hook failed: %s", outcome.failed_error)

    def _reconcile_background_tasks_if_needed(self) -> None:
        if self._background_tasks_reconciled:
            return
        task_summaries = self._session_store.list_background_tasks(workspace=self._workspace)
        for task_summary in task_summaries:
            if task_summary.status != "running" or task_summary.session_id is None:
                continue
            task = self._session_store.load_background_task(
                workspace=self._workspace,
                task_id=task_summary.task.id,
            )
            child_response = self._load_background_task_child_response(task=task)
            if child_response is None:
                continue
            if child_response.session.status in ("waiting", "completed", "failed"):
                self._finalize_background_task_from_session_response(
                    session_response=child_response
                )
        fail_incomplete = getattr(self._session_store, "fail_incomplete_background_tasks", None)
        if callable(fail_incomplete):
            failed_tasks = cast(
                tuple[BackgroundTaskState, ...],
                fail_incomplete(
                    workspace=self._workspace,
                    message="background task interrupted before completion",
                ),
            )
            for failed_task in failed_tasks:
                self._run_background_task_lifecycle_hook(failed_task)
        task_summaries = self._session_store.list_background_tasks(workspace=self._workspace)
        for task_summary in task_summaries:
            task = self._session_store.load_background_task(
                workspace=self._workspace,
                task_id=task_summary.task.id,
            )
            self._backfill_parent_background_task_event(task=task)
        self._background_tasks_reconciled = True

    def _run_background_task_worker(self, task_id: str) -> None:
        try:
            task = self.load_background_task(task_id)
            if task.status == "cancelled":
                return
            request = RuntimeRequest(
                prompt=task.request.prompt,
                session_id=task.request.session_id,
                parent_session_id=task.request.parent_session_id,
                metadata=cast(RuntimeRequestMetadataPayload, task.request.metadata),
                allocate_session_id=task.request.allocate_session_id,
            )
            routing = self._session_routing_for_request(request)
            session_id = routing.session_id
            running_task = self._session_store.mark_background_task_running(
                workspace=self._workspace,
                task_id=task_id,
                session_id=session_id,
            )
            if running_task.status != "running":
                return
            dispatch_task = self.load_background_task(task_id)
            if dispatch_task.status != "running":
                return
            if dispatch_task.cancel_requested_at is not None:
                terminal_task = self._session_store.mark_background_task_terminal(
                    workspace=self._workspace,
                    task_id=task_id,
                    status="cancelled",
                    error="cancelled before dispatch",
                )
                self._run_background_task_lifecycle_hook(terminal_task)
                return
            events: list[EventEnvelope] = []
            output: str | None = None
            final_session: SessionState | None = None
            internal_request = RuntimeRequest(
                prompt=dispatch_task.request.prompt,
                session_id=session_id,
                parent_session_id=dispatch_task.request.parent_session_id,
                metadata=cast(
                    InternalRuntimeRequestMetadata,
                    {
                        **dispatch_task.request.metadata,
                        "background_task_id": task_id,
                        "background_run": True,
                    },
                ),
                allocate_session_id=False,
            )
            for chunk in self._run_with_persistence(
                internal_request,
                allow_internal_metadata=True,
            ):
                final_session = chunk.session
                if chunk.event is not None:
                    events.append(chunk.event)
                if chunk.kind == "output":
                    output = chunk.output
                current_task_state = self._session_store.load_background_task(
                    workspace=self._workspace,
                    task_id=task_id,
                )
                if current_task_state.cancel_requested_at is not None:
                    cancel_metadata = dict(final_session.metadata)
                    cancel_metadata["abort_requested"] = True
                    cancelled_response = RuntimeResponse(
                        session=SessionState(
                            session=final_session.session,
                            status="failed",
                            turn=final_session.turn,
                            metadata=cancel_metadata,
                        ),
                        events=tuple(events)
                        + (
                            EventEnvelope(
                                session_id=session_id,
                                sequence=(events[-1].sequence if events else 0) + 1,
                                event_type=RUNTIME_FAILED,
                                source="runtime",
                                payload={
                                    "error": "cancelled by parent during delegated execution",
                                    "cancelled": True,
                                    "delegated_task_id": task_id,
                                },
                            ),
                        ),
                        output=output,
                    )
                    self._session_store.save_run(
                        workspace=self._workspace,
                        request=internal_request,
                        response=cancelled_response,
                    )
                    terminal_task = self._session_store.mark_background_task_terminal(
                        workspace=self._workspace,
                        task_id=task_id,
                        status="cancelled",
                        error="cancelled by parent during delegated execution",
                    )
                    self._run_background_task_lifecycle_hook(terminal_task)
                    return
            if final_session is None:
                raise ValueError("runtime stream emitted no chunks")
            if final_session.status == "waiting":
                final_session = self._reload_persisted_session(session_id=final_session.session.id)
            response = RuntimeResponse(
                session=final_session,
                events=tuple(events),
                output=output,
            )
            self._finalize_background_task_from_session_response(session_response=response)
        except Exception as exc:
            logger.exception("background task failed: %s", task_id)
            terminal_task = self._session_store.mark_background_task_terminal(
                workspace=self._workspace,
                task_id=task_id,
                status="failed",
                error=str(exc),
            )
            self._run_background_task_lifecycle_hook(terminal_task)
        finally:
            self._background_task_threads.pop(task_id, None)

    def _effective_runtime_config_from_metadata(
        self, metadata: dict[str, object] | None
    ) -> EffectiveRuntimeConfig:
        approval_mode: PermissionDecision = self._config.approval_mode
        model = self._config.model
        execution_engine = self._config.execution_engine
        max_steps = self._config.max_steps
        provider_fallback = self._config.provider_fallback
        plan = self._config.plan
        agent = self._config.agent
        allow_persisted_subagent_presets = False
        if metadata is not None:
            allow_persisted_subagent_presets = (
                runtime_subagent_route_from_metadata(metadata) is not None
            )
        if agent is not None:
            agent = parse_runtime_agent_payload(
                serialize_runtime_agent_config(agent),
                source="runtime config agent",
            )
            assert agent is not None
            self._validate_runtime_agent_for_execution(
                agent,
                source="runtime config agent",
            )
        execution_engine_override = (
            agent.execution_engine
            if agent is not None and agent.execution_engine is not None
            else None
        )
        model_override = agent.model if agent is not None and agent.model is not None else None
        provider_fallback_override = (
            agent.provider_fallback
            if agent is not None and agent.provider_fallback is not None
            else None
        )
        if execution_engine_override is not None:
            execution_engine = execution_engine_override
        if model_override is not None:
            model = model_override
        if provider_fallback_override is not None:
            provider_fallback = provider_fallback_override
        resolved_provider = resolve_provider_config(
            model,
            provider_fallback,
            registry=self._model_provider_registry,
        )
        if metadata is None:
            return EffectiveRuntimeConfig(
                approval_mode=approval_mode,
                model=model,
                execution_engine=execution_engine,
                max_steps=max_steps,
                tool_timeout_seconds=self._config.tool_timeout_seconds,
                provider_fallback=provider_fallback,
                plan=plan,
                resolved_provider=resolved_provider,
                agent=agent,
            )

        persisted_runtime_config = metadata.get("runtime_config")
        if not isinstance(persisted_runtime_config, dict):
            return EffectiveRuntimeConfig(
                approval_mode=approval_mode,
                model=model,
                execution_engine=execution_engine,
                max_steps=max_steps,
                tool_timeout_seconds=self._config.tool_timeout_seconds,
                provider_fallback=provider_fallback,
                plan=plan,
                resolved_provider=resolved_provider,
                agent=agent,
            )

        runtime_config = cast(dict[str, object], persisted_runtime_config)
        persisted_approval_mode = runtime_config.get("approval_mode")
        if persisted_approval_mode in ("allow", "deny", "ask"):
            approval_mode = persisted_approval_mode
        persisted_model = runtime_config.get("model")
        if persisted_model is None or isinstance(persisted_model, str):
            model = persisted_model
        persisted_max_steps = runtime_config.get("max_steps")
        if isinstance(persisted_max_steps, int) and not isinstance(persisted_max_steps, bool):
            if persisted_max_steps < 1:
                raise ValueError("persisted runtime_config max_steps must be at least 1")
            max_steps = persisted_max_steps
        tool_timeout_seconds = self._config.tool_timeout_seconds
        if "tool_timeout_seconds" in runtime_config:
            persisted_tool_timeout = runtime_config.get("tool_timeout_seconds")
            if persisted_tool_timeout is None:
                tool_timeout_seconds = None
            elif isinstance(persisted_tool_timeout, int) and not isinstance(
                persisted_tool_timeout, bool
            ):
                if persisted_tool_timeout < 1:
                    raise ValueError(
                        "persisted runtime_config tool_timeout_seconds must be at least 1"
                    )
                tool_timeout_seconds = persisted_tool_timeout
        provider_fallback = None
        if "provider_fallback" in runtime_config:
            try:
                provider_fallback = parse_provider_fallback_payload(
                    runtime_config.get("provider_fallback"),
                    source="persisted runtime_config.provider_fallback",
                )
            except ValueError as exc:
                raise ValueError(
                    format_invalid_provider_config_error(
                        "persisted runtime_config.provider_fallback",
                        str(exc),
                    )
                ) from exc
        if "plan" in runtime_config:
            try:
                plan = parse_runtime_plan_payload(
                    runtime_config.get("plan"),
                    source="persisted runtime_config.plan",
                )
            except ValueError as exc:
                raise ValueError(
                    format_invalid_provider_config_error(
                        "persisted runtime_config.plan",
                        str(exc),
                    )
                ) from exc
        if "agent" in runtime_config:
            agent = parse_runtime_agent_payload(
                runtime_config.get("agent"),
                source="persisted runtime_config.agent",
            )
            if agent is not None:
                self._validate_runtime_agent_for_execution(
                    agent,
                    source="persisted runtime_config.agent",
                    allow_subagent_presets=allow_persisted_subagent_presets,
                )
        else:
            agent = None
        persisted_execution_engine = runtime_config.get("execution_engine")
        if persisted_execution_engine in ("deterministic", "provider"):
            execution_engine = persisted_execution_engine
        raw_resolved_provider = runtime_config.get("resolved_provider")
        if raw_resolved_provider is not None:
            resolved_provider = parse_resolved_provider_snapshot(
                raw_resolved_provider,
                source="persisted runtime_config.resolved_provider",
                registry=self._model_provider_registry,
            )
            model = resolved_provider.model
            provider_fallback = resolved_provider.provider_fallback
        else:
            resolved_provider = resolve_provider_config(
                model,
                provider_fallback,
                registry=self._model_provider_registry,
            )
        return EffectiveRuntimeConfig(
            approval_mode=approval_mode,
            model=model,
            execution_engine=execution_engine,
            max_steps=max_steps,
            tool_timeout_seconds=tool_timeout_seconds,
            provider_fallback=provider_fallback,
            plan=plan,
            resolved_provider=resolved_provider,
            agent=agent,
        )

    def _provider_chain_for_session_metadata(
        self, metadata: dict[str, object] | None
    ) -> ResolvedProviderChain:
        effective_config = self._effective_runtime_config_from_metadata(metadata)
        return effective_config.resolved_provider.target_chain

    def _graph_for_session_metadata(self, metadata: dict[str, object] | None) -> RuntimeGraph:
        if self._graph_override is not None:
            return self._graph_override

        effective_config = self._effective_runtime_config_from_metadata(metadata)

        # Reuse self._graph if the session's config matches the runtime's config
        if (
            effective_config.execution_engine == self._initial_effective_config.execution_engine
            and effective_config.model == self._initial_effective_config.model
            and effective_config.max_steps == self._initial_effective_config.max_steps
            and effective_config.provider_fallback
            == self._initial_effective_config.provider_fallback
            and effective_config.agent == self._initial_effective_config.agent
        ):
            return self._graph

        # Otherwise use cached graph or build new one
        return self._build_graph_for_engine_from_config(effective_config)

    def _validate_session_workspace(self, session: SessionState, *, session_id: str) -> None:
        session_workspace = session.metadata.get("workspace")
        if session_workspace is None:
            return
        if session_workspace != str(self._workspace):
            raise ValueError(f"session {session_id} does not belong to workspace {self._workspace}")

    def _load_existing_session_if_present(self, *, session_id: str) -> RuntimeResponse | None:
        if not self._session_store.has_session(workspace=self._workspace, session_id=session_id):
            return None
        response = self._session_store.load_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        self._validate_session_workspace(response.session, session_id=session_id)
        return response

    def _is_active_session_id(self, session_id: str) -> bool:
        return _ACTIVE_SESSION_REGISTRY.contains(workspace=self._workspace, session_id=session_id)

    def _register_active_session_id(
        self,
        session_id: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        _ACTIVE_SESSION_REGISTRY.register(workspace=self._workspace, session_id=session_id)
        if metadata is not None:
            _ACTIVE_SESSION_REGISTRY.remember_metadata(
                workspace=self._workspace,
                session_id=session_id,
                metadata=metadata,
            )

    def _unregister_active_session_id(self, session_id: str) -> None:
        _ACTIVE_SESSION_REGISTRY.unregister(workspace=self._workspace, session_id=session_id)

    def _active_session_metadata(self, session_id: str) -> dict[str, object] | None:
        return _ACTIVE_SESSION_REGISTRY.metadata(
            workspace=self._workspace,
            session_id=session_id,
        )

    def _session_belongs_to_workspace(self, session_id: str) -> bool:
        try:
            response = self._load_existing_session_if_present(session_id=session_id)
        except ValueError:
            return False
        if response is None:
            return False
        return True


@dataclass(frozen=True, slots=True)
class EffectiveRuntimeConfig:
    approval_mode: PermissionDecision
    model: str | None
    execution_engine: ExecutionEngineName
    max_steps: int
    tool_timeout_seconds: int | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None
    plan: RuntimePlanConfig | None = None
    resolved_provider: ResolvedProviderConfig = field(default_factory=ResolvedProviderConfig)
    agent: RuntimeAgentConfig | None = None


@dataclass(frozen=True, slots=True)
class _ApprovalResumeCheckpointState:
    prompt: str
    session_metadata: dict[str, object]
    tool_results: tuple[ToolResult, ...]


@dataclass(frozen=True, slots=True)
class _PermissionOutcome:
    chunks: tuple[RuntimeStreamChunk, ...]
    last_sequence: int
    pending_approval: PendingApproval | None = None
    denied: bool = False


@dataclass(frozen=True, slots=True)
class _HookOutcome:
    chunks: tuple[RuntimeStreamChunk, ...]
    last_sequence: int
    failed_error: str | None = None
