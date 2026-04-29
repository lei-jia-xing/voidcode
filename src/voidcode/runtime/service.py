# pyright: reportUnusedFunction=false, reportUnusedImport=false
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast, final

from ..acp import AcpDelegatedExecution, AcpEventEnvelope, AcpRequestEnvelope, AcpResponseEnvelope
from ..agent import get_builtin_agent_manifest, list_builtin_agent_manifests
from ..agent.prompts import render_agent_prompt
from ..command import (
    COMMAND_RESOLVED,
    is_prompt_command,
    load_command_registry,
    resolve_prompt_command,
)
from ..graph.contracts import GraphEvent, GraphRunRequest, RuntimeGraph
from ..hook.config import RuntimeHookSurface
from ..hook.executor import (
    HookExecutionOutcome,
    HookExecutionRequest,
    LifecycleHookExecutionRequest,
    run_lifecycle_hooks,
    run_tool_hooks,
)
from ..provider.auth import (
    ProviderAuthAuthorizeRequest,
    ProviderAuthResolutionError,
    ProviderAuthResolver,
)
from ..provider.errors import format_invalid_provider_config_error, guidance_for_provider_error_kind
from ..provider.model_catalog import (
    ProviderModelCatalog,
    infer_model_metadata,
)
from ..provider.model_catalog import (
    ProviderModelMetadata as CatalogProviderModelMetadata,
)
from ..provider.models import (
    ResolvedProviderChain,
    ResolvedProviderConfig,
    ResolvedProviderModel,
)
from ..provider.protocol import ProviderTokenUsage
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
    Tool,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from ..tools.guidance import definition_with_guidance
from ..tools.question import QuestionTool
from ..tools.runtime_context import current_runtime_tool_context
from ..tools.skill import SkillTool
from ..tools.task import TaskTool
from .acp import AcpAdapter, AcpAdapterState, build_acp_adapter
from .background_tasks import RuntimeBackgroundTaskSupervisor
from .config import (
    ExecutionEngineName,
    RuntimeAgentConfig,
    RuntimeCategoryConfig,
    RuntimeConfig,
    RuntimeContextWindowConfig,
    RuntimeHooksConfig,
    RuntimeProviderFallbackConfig,
    RuntimeSkillsConfig,
    RuntimeWebSettings,
    load_global_web_settings,
    load_runtime_config,
    parse_provider_fallback_payload,
    parse_runtime_agent_payload,
    parse_runtime_agents_payload,
    parse_runtime_categories_payload,
    parse_runtime_context_window_payload,
    save_global_web_settings,
    serialize_provider_fallback_config,
    serialize_runtime_agent_config,
    serialize_runtime_agents_config,
    serialize_runtime_categories_config,
    serialize_runtime_context_window_config,
)
from .context_window import (
    ContextWindowPolicy,
    RuntimeAssembledContext,
    RuntimeContextWindow,
    RuntimeContinuityState,
    assemble_provider_context,
    prepare_provider_context,
)
from .contracts import (
    AgentSummary,
    BackgroundTaskResult,
    CapabilityStatusSnapshot,
    GitStatusSnapshot,
    ProviderInspectResult,
    ProviderModelMetadata,
    ProviderModelsResult,
    ProviderReadinessResult,
    ProviderSummary,
    ProviderValidationResult,
    ReviewFileDiff,
    RuntimeNotification,
    RuntimeRequest,
    RuntimeRequestError,
    RuntimeRequestMetadataPayload,
    RuntimeResponse,
    RuntimeSessionDebugEvent,
    RuntimeSessionDebugFailure,
    RuntimeSessionDebugPendingApproval,
    RuntimeSessionDebugPendingQuestion,
    RuntimeSessionDebugSnapshot,
    RuntimeSessionDebugToolSummary,
    RuntimeSessionResult,
    RuntimeStatusSnapshot,
    RuntimeStreamChunk,
    UnknownSessionError,
    WorkspaceReviewSnapshot,
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
    RUNTIME_CATEGORY_MODEL_DIAGNOSTIC,
    RUNTIME_LSP_SERVER_FAILED,
    RUNTIME_LSP_SERVER_REUSED,
    RUNTIME_LSP_SERVER_STARTED,
    RUNTIME_LSP_SERVER_STARTUP_REJECTED,
    RUNTIME_LSP_SERVER_STOPPED,
    RUNTIME_MCP_SERVER_ACQUIRED,
    RUNTIME_MCP_SERVER_FAILED,
    RUNTIME_MCP_SERVER_IDLE_CLEANED,
    RUNTIME_MCP_SERVER_RELEASED,
    RUNTIME_MCP_SERVER_REUSED,
    RUNTIME_MCP_SERVER_STARTED,
    RUNTIME_MCP_SERVER_STOPPED,
    RUNTIME_QUESTION_ANSWERED,
    RUNTIME_QUESTION_REQUESTED,
    RUNTIME_SKILLS_APPLIED,
    RUNTIME_SKILLS_LOADED,
    EventEnvelope,
)
from .execution_seams import (
    cache_key_for_effective_config,
    fallback_graph_for_provider_error,
    provider_model_required_message,
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
from .provider_protocol import ProviderExecutionError
from .question import PendingQuestion, QuestionResponse
from .resume import RuntimeResumeCoordinator
from .review import WorkspaceReviewService
from .run_loop import RuntimeRunLoopCoordinator
from .session import SessionRef, SessionState, SessionStatus, StoredSessionSummary
from .skills import (
    SkillExecutionSnapshot,
    SkillRuntimeContext,
    build_runtime_context,
    build_runtime_contexts,
    build_skill_execution_snapshot,
    snapshot_from_payload,
    snapshot_payload,
)
from .storage import SessionEventAppender, SessionStore, SqliteSessionStore
from .task import (
    BackgroundTaskState,
    StoredBackgroundTaskSummary,
    supported_subagent_categories,
    validate_background_task_id,
)
from .tool_provider import BuiltinToolProvider

if TYPE_CHECKING:
    from ..tools.lsp import FormatTool
    from .execution_seams import RuntimeGraphSelection, RuntimeSessionRouting

logger = logging.getLogger(__name__)

_EXECUTABLE_AGENT_PRESETS = frozenset({"leader", "product"})
_EXECUTABLE_SUBAGENT_PRESETS = frozenset({"advisor", "explore", "product", "researcher", "worker"})
_PERSISTED_RUNTIME_CONFIG_KEYS = frozenset(
    {
        "approval_mode",
        "execution_engine",
        "max_steps",
        "tool_timeout_seconds",
        "model",
        "provider_fallback",
        "resolved_provider",
        "agent",
        "agents",
        "categories",
        "context_window",
        "lsp",
        "mcp",
    }
)
_ACP_CONNECTIVITY_ERRORS = frozenset(
    {
        "ACP adapter is not connected",
        "ACP transport is not connected",
    }
)
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
    "agent",
    "lsp",
    "mcp",
)


@dataclass(frozen=True, slots=True)
class _ActiveSessionKey:
    workspace: Path
    session_id: str


def _provider_target_label(target: ResolvedProviderModel) -> str:
    provider = target.selection.provider
    model = target.selection.model
    if provider is None and model is None:
        return "unresolved"
    if provider is None:
        return str(model)
    if model is None:
        return provider
    return f"{provider}/{model}"


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
    _graph: RuntimeGraph | None
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
    _background_task_threads: dict[str, threading.Thread]
    _background_tasks_reconciled: bool
    _context_window_config_override: RuntimeContextWindowConfig | None
    _run_loop_coordinator: RuntimeRunLoopCoordinator
    _resume_coordinator: RuntimeResumeCoordinator
    _background_task_supervisor: RuntimeBackgroundTaskSupervisor
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
        self._hydrate_provider_model_catalog_cache()
        initial_agent = self._config.agent
        if initial_agent is None and self._config.execution_engine == "provider":
            initial_agent = RuntimeAgentConfig(preset="leader")
        if initial_agent is not None:
            initial_agent = parse_runtime_agent_payload(
                serialize_runtime_agent_config(initial_agent),
                source="runtime config agent",
                hooks=self._config.hooks,
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
        self._context_window_config_override = self._context_window_config_from_policy(
            context_window_policy
        )
        initial_context_window = self._context_window_config_override or self._config.context_window
        self._initial_effective_config = EffectiveRuntimeConfig(
            approval_mode=self._config.approval_mode,
            model=initial_model,
            execution_engine=initial_execution_engine,
            max_steps=self._config.max_steps,
            tool_timeout_seconds=self._config.tool_timeout_seconds,
            provider_fallback=initial_provider_fallback,
            resolved_provider=self._resolved_provider_config,
            agent=initial_agent,
            context_window=initial_context_window,
        )
        if graph is not None:
            self._graph = graph
        elif self._can_build_graph_for_effective_config(self._initial_effective_config):
            self._graph = self._build_graph_for_engine_from_config(self._initial_effective_config)
        else:
            self._graph = None
        self._permission_policy = permission_policy or PermissionPolicy(
            mode=self._config.approval_mode
        )
        self._session_store = session_store or SqliteSessionStore()
        self._acp_adapter = acp_adapter or build_acp_adapter(self._config.acp)
        self._background_task_threads = {}
        self._background_tasks_reconciled = False
        self._default_context_window_policy = self._context_window_policy_from_config(
            initial_context_window,
            resolved_provider=None,
        )
        self._run_loop_coordinator = RuntimeRunLoopCoordinator(self)
        self._resume_coordinator = RuntimeResumeCoordinator(self)
        self._background_task_supervisor = RuntimeBackgroundTaskSupervisor(self)

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
        if not isinstance(existing_plan_state, dict):
            return None
        plan_state: dict[str, object] = dict(cast(dict[str, object], existing_plan_state))

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
        return self._load_stored_response(session_id=session_id).session

    @staticmethod
    def _resequence_event(event: EventEnvelope, *, sequence: int) -> EventEnvelope:
        # Referenced via extracted collaborators.
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

    @staticmethod
    def _can_build_graph_for_effective_config(config: EffectiveRuntimeConfig) -> bool:
        if config.execution_engine != "provider":
            return True
        return config.resolved_provider.active_target.provider is not None

    @staticmethod
    def _validate_provider_execution_ready(config: EffectiveRuntimeConfig) -> None:
        if config.execution_engine != "provider":
            return
        if config.resolved_provider.active_target.provider is not None:
            return
        raise RuntimeRequestError(provider_model_required_message())

    def _graph_selection_for_effective_config(
        self,
        config: EffectiveRuntimeConfig,
        *,
        provider_attempt: int = 0,
    ) -> RuntimeGraphSelection:
        # Referenced via extracted run-loop/resume collaborators.
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
        # Referenced via extracted run-loop collaborator.
        return fallback_graph_for_provider_error(
            error=error,
            provider_chain=self._provider_chain_for_session_metadata(session_metadata),
            config=self._effective_runtime_config_from_metadata(session_metadata),
            provider_attempt=provider_attempt,
        )

    def _session_routing_for_request(self, request: RuntimeRequest) -> RuntimeSessionRouting:
        # Referenced via extracted background-task collaborator.
        return resolve_runtime_session_routing(request)

    def _runtime_config_for_request(self, request: RuntimeRequest) -> EffectiveRuntimeConfig:
        resolved = self._effective_runtime_config_from_metadata(None)
        request_agent = request.metadata.get("agent")
        if request_agent is not None:
            try:
                resolved = self._config_with_request_agent_override(
                    resolved,
                    request_agent,
                    allow_subagent_presets=request.subagent_routing is not None,
                )
            except ValueError as exc:
                raise RuntimeRequestError(str(exc)) from exc
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
                resolved_provider=resolved.resolved_provider,
                agent=resolved.agent,
                context_window=resolved.context_window,
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

        context = current_runtime_tool_context()
        return tuple(
            McpTool(
                server_name=tool.server_name,
                tool_name=tool.tool_name,
                description=tool.description,
                input_schema=tool.input_schema,
                safety=tool.safety,
                requester=self.request_mcp_tool,
            )
            for tool in self._mcp_manager.list_tools(
                workspace=self._workspace,
                owner_session_id=context.session_id if context is not None else None,
            )
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
            if self._mcp_manager.current_state().mode != "managed":
                return (), session, sequence, None
            merged_tools = dict(self._base_tool_registry.tools)
            for tool in self._build_mcp_tools_for_owner(owner_session_id=session.session.id):
                merged_tools[tool.definition.name] = tool
            self._tool_registry = ToolRegistry(tools=merged_tools)
        except Exception:
            logger.info(
                "continuing session %s after MCP tool refresh failure",
                session.session.id,
                extra={"failure_kind": failure_kind},
                exc_info=True,
            )
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
            return emitted, session, last_sequence, None
        return (), session, sequence, None

    def _build_mcp_tools_for_owner(self, *, owner_session_id: str | None) -> tuple[Tool, ...]:
        if self._mcp_manager.current_state().mode != "managed":
            return ()
        from ..tools.mcp import McpTool

        return tuple(
            McpTool(
                server_name=tool.server_name,
                tool_name=tool.tool_name,
                description=tool.description,
                input_schema=tool.input_schema,
                safety=tool.safety,
                requester=self.request_mcp_tool,
            )
            for tool in self._mcp_manager.list_tools(
                workspace=self._workspace,
                owner_session_id=owner_session_id,
            )
        )

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

    def _delegation_tool_policy_error(
        self,
        *,
        session: SessionState,
        tool_name: str,
    ) -> str | None:
        # Runtime-owned child preset governance: provider-visible schemas are already
        # narrowed, but malicious/raw provider tool calls still need a clear policy
        # denial before normal lookup can obscure the reason as an unknown tool.
        if runtime_subagent_route_from_metadata(session.metadata) is None:
            return None
        effective_config = self._effective_runtime_config_from_metadata(session.metadata)
        agent = effective_config.agent
        if agent is None:
            return None
        manifest = get_builtin_agent_manifest(agent.preset)
        if manifest is None or not manifest.tool_allowlist:
            return None
        if self._tool_name_matches_patterns(tool_name, manifest.tool_allowlist):
            return None
        if tool_name not in self._base_tool_registry.tools:
            return None
        return (
            "delegation policy denied tool "
            f"'{tool_name}' for child preset '{agent.preset}'; this preset may only call "
            "tools allowed by its manifest tool_allowlist"
        )

    @staticmethod
    def _tool_name_matches_patterns(tool_name: str, patterns: Iterable[str]) -> bool:
        return any(fnmatchcase(tool_name, pattern) for pattern in patterns if pattern)

    def current_lsp_state(self) -> LspManagerState:
        return self._lsp_manager.current_state()

    def current_mcp_state(self):
        return self._mcp_manager.current_state()

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
        context = current_runtime_tool_context()
        return self._mcp_manager.call_tool(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            workspace=workspace,
            owner_session_id=context.session_id if context is not None else None,
            parent_session_id=context.parent_session_id if context is not None else None,
        )

    def _release_mcp_session(self, session_id: str) -> tuple[EventEnvelope, ...]:
        release = getattr(self._mcp_manager, "release_session", None)
        if not callable(release):
            return ()
        return self._envelopes_for_mcp_events(
            session_id=session_id,
            start_sequence=1,
            mcp_events=cast(tuple[object, ...], release(session_id=session_id)),
        )

    def cleanup_idle_mcp_sessions(
        self,
        *,
        max_idle_seconds: float = 300.0,
    ) -> tuple[EventEnvelope, ...]:
        cleanup = getattr(self._mcp_manager, "cleanup_idle_session_servers", None)
        if not callable(cleanup):
            return ()
        return self._envelopes_for_mcp_events(
            session_id="runtime",
            start_sequence=1,
            mcp_events=cast(tuple[object, ...], cleanup(max_idle_seconds=max_idle_seconds)),
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
        envelope = AcpRequestEnvelope(
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
        response = self._acp_adapter.request(envelope)
        if response.status != "error" or response.error not in _ACP_CONNECTIVITY_ERRORS:
            return response
        try:
            if response.error == "ACP transport is not connected":
                _ = self.disconnect_acp()
            _ = self.connect_acp()
        except Exception:
            logger.debug("failed to reconnect ACP for delegated request retry", exc_info=True)
            return response
        return self._acp_adapter.request(envelope)

    def _delegated_execution_for_task(
        self,
        *,
        task: BackgroundTaskState,
        lifecycle_status: str,
        approval_blocked: bool | None = None,
        result_available: bool | None = None,
    ) -> AcpDelegatedExecution:
        try:
            routing = task.routing_identity
        except ValueError:
            routing = None
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
        # Referenced via extracted background-task collaborator.
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
        # Referenced via extracted background-task collaborator.
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
        run_id = os.urandom(8).hex()
        self._register_active_session_id(
            session_id,
            metadata={
                "prompt": request.prompt,
                "run_id": run_id,
                **dict(request.metadata),
                "request_metadata": dict(request.metadata),
            },
        )
        try:
            events: list[EventEnvelope] = []
            output: str | None = None
            final_session: SessionState | None = None

            try:
                for chunk in self._stream_chunks(request, session_id=session_id, run_id=run_id):
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

            final_session = self._session_with_loaded_skill_metadata(
                final_session,
                events=tuple(events),
            )

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

        final_session = self._session_with_loaded_skill_metadata(
            final_session,
            events=tuple(events),
        )

        return RuntimeResponse(session=final_session, events=tuple(events), output=output)

    @staticmethod
    def _session_with_loaded_skill_metadata(
        session: SessionState,
        *,
        events: tuple[EventEnvelope, ...],
        force_loaded_skills: tuple[dict[str, object], ...] = (),
    ) -> SessionState:
        loaded_payloads = [
            event.payload for event in events if event.event_type == "runtime.skill_loaded"
        ]
        implicit_force_loaded: tuple[dict[str, object], ...] = ()
        raw_snapshot = session.metadata.get("skill_snapshot")
        if isinstance(raw_snapshot, dict):
            snapshot_payload = cast(dict[str, object], raw_snapshot)
            applied_payloads = snapshot_payload.get("applied_skill_payloads")
            if isinstance(applied_payloads, list):
                normalized: list[dict[str, object]] = []
                for item in cast(list[object], applied_payloads):
                    if not isinstance(item, dict):
                        continue
                    payload = cast(dict[str, object], item)
                    normalized.append(
                        {
                            "name": payload.get("name"),
                            "source": "force_load",
                            "source_path": payload.get("source_path"),
                        }
                    )
                implicit_force_loaded = tuple(normalized)

        merged_payloads = [*loaded_payloads, *force_loaded_skills, *implicit_force_loaded]
        if not merged_payloads:
            return session
        return SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata={
                **session.metadata,
                "loaded_skills": merged_payloads,
            },
        )

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
        run_id: str | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        resolved_session_id = session_id or self._resolve_session_id(request)
        effective_config = self._runtime_config_for_request(request)
        if self._graph_override is None:
            self._validate_provider_execution_ready(effective_config)
        request_metadata = self._fresh_request_metadata(request.metadata)
        session_request_metadata = dict(request_metadata)
        session_request_metadata.pop("background_rate_limit_retry", None)
        session = SessionState(
            session=SessionRef(id=resolved_session_id, parent_id=request.parent_session_id),
            status="running",
            turn=1,
            metadata={
                **session_request_metadata,
                "workspace": str(self._workspace),
                "runtime_config": self._runtime_config_metadata(effective_config),
                "runtime_state": self._runtime_state_metadata(run_id=run_id),
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

        for diagnostic in self._category_model_diagnostics(
            request_metadata=request_metadata,
            effective_config=effective_config,
        ):
            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type=RUNTIME_CATEGORY_MODEL_DIAGNOSTIC,
                    source="runtime",
                    payload=diagnostic,
                ),
            )

        command_metadata = request_metadata.get("command")
        if isinstance(command_metadata, dict):
            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type=COMMAND_RESOLVED,
                    source="runtime",
                    payload={
                        **cast(dict[str, object], command_metadata),
                        "rendered_prompt": request.prompt,
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

        loaded_skill_names = self._loaded_skill_names(skill_registry)

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
        catalog_skill_context = self._catalog_skill_context(
            skill_registry,
            available_skill_names=tuple(loaded_skill_names),
            selected_skill_names=skill_snapshot.selected_skill_names,
        )
        skill_prompt_context = skill_snapshot.skill_prompt_context or catalog_skill_context
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
        sequence += 1
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence,
                event_type=RUNTIME_SKILLS_LOADED,
                source="runtime",
                payload={
                    "skills": loaded_skill_names,
                    "selected_skills": list(skill_snapshot.selected_skill_names),
                    "catalog_context_length": len(catalog_skill_context),
                },
            ),
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

        graph_request = GraphRunRequest(
            session=session,
            prompt=request.prompt,
            available_tools=tool_registry.definitions(),
            context_window=self._prepare_provider_context_window(
                prompt=request.prompt,
                tool_results=(),
                session_metadata=session.metadata,
            ),
            assembled_context=self._assemble_provider_context(
                prompt=request.prompt,
                tool_results=(),
                session_metadata=session.metadata,
                skill_prompt_context=skill_prompt_context,
            ),
            metadata={
                **request_metadata,
                "agent_preset": serialize_runtime_agent_config(
                    self._effective_runtime_config_from_metadata(session.metadata).agent
                ),
                "provider_attempt": 0,
                "provider_stream": _coerce_bool_like(
                    request_metadata.get("provider_stream", False),
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
        release_sequence = end_hook_outcome.last_sequence
        if end_hook_outcome.failed_error is not None:
            logger.warning(
                "session_end hook failed for %s: %s",
                session.session.id,
                end_hook_outcome.failed_error,
            )
        release_session = getattr(self._mcp_manager, "release_session", None)
        release_events: tuple[object, ...] = ()
        if callable(release_session):
            release_events = cast(
                tuple[object, ...],
                release_session(session_id=finalized_session.session.id),
            )
        for event in self._envelopes_for_mcp_events(
            session_id=finalized_session.session.id,
            start_sequence=release_sequence + 1,
            mcp_events=release_events,
        ):
            release_sequence = event.sequence
            yield RuntimeStreamChunk(kind="event", session=finalized_session, event=event)

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
        yield from self._run_loop_coordinator.execute_graph_loop(
            graph=graph,
            tool_registry=tool_registry,
            session=session,
            sequence=sequence,
            graph_request=graph_request,
            tool_results=tool_results,
            approval_resolution=approval_resolution,
            permission_policy=permission_policy,
            preserved_continuity_state=preserved_continuity_state,
        )

    def _run_lifecycle_hooks(
        self,
        *,
        session: SessionState,
        sequence: int,
        surface: RuntimeHookSurface,
        payload: dict[str, object] | None = None,
    ) -> _RuntimeHookOutcome:
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
        return _RuntimeHookOutcome(
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
    ) -> _RuntimeHookOutcome:
        # Referenced via extracted run-loop collaborator.
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
        return _RuntimeHookOutcome(
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
        # Referenced via extracted run-loop collaborator.
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
        pending = replace(pending, request_event_sequence=request_event.sequence)
        return _PermissionOutcome(
            chunks=(
                RuntimeStreamChunk(kind="event", session=waiting_session, event=request_event),
            ),
            last_sequence=sequence,
            pending_approval=pending,
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
        return self._background_task_supervisor.start_background_task(request)

    def load_background_task(self, task_id: str) -> BackgroundTaskState:
        self._reconcile_background_tasks_if_needed()
        validate_background_task_id(task_id)
        return self._session_store.load_background_task(workspace=self._workspace, task_id=task_id)

    def load_background_task_result(self, task_id: str) -> BackgroundTaskResult:
        return self._background_task_supervisor.load_background_task_result(task_id)

    def list_background_tasks(self) -> tuple[StoredBackgroundTaskSummary, ...]:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        return self._session_store.list_background_tasks(workspace=self._workspace)

    def list_background_tasks_by_parent_session(
        self, *, parent_session_id: str
    ) -> tuple[StoredBackgroundTaskSummary, ...]:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        validated_parent_session_id = validate_session_reference_id(
            parent_session_id,
            field_name="parent_session_id",
        )
        return self._session_store.list_background_tasks_by_parent_session(
            workspace=self._workspace,
            parent_session_id=validated_parent_session_id,
        )

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        return self._background_task_supervisor.cancel_background_task(task_id)

    def session_result(self, *, session_id: str) -> RuntimeSessionResult:
        _ = self._load_session_result(session_id=session_id)
        self._background_task_supervisor.reconcile_parent_background_task_events_for_session(
            parent_session_id=session_id
        )
        return self._load_session_result(session_id=session_id)

    def _load_session_result(self, *, session_id: str) -> RuntimeSessionResult:
        validate_session_id(session_id)
        result = self._session_store.load_session_result(
            workspace=self._workspace,
            session_id=session_id,
        )
        self._validate_session_workspace(result.session, session_id=session_id)
        return result

    def session_debug_snapshot(self, *, session_id: str) -> RuntimeSessionDebugSnapshot:
        validate_session_id(session_id)
        active = self._is_active_session_id(session_id)
        active_metadata = self._active_session_metadata(session_id) if active else None
        try:
            result = self._load_session_result(session_id=session_id)
        except UnknownSessionError:
            if not active:
                raise
            return self._active_only_session_debug_snapshot(session_id=session_id)
        if self._should_prefer_active_debug_snapshot(
            result=result,
            active_metadata=active_metadata,
        ):
            return self._active_only_session_debug_snapshot(session_id=session_id)
        persistence_error: str | None = None
        pending_approval: PendingApproval | None = None
        pending_question: PendingQuestion | None = None
        resume_checkpoint: dict[str, object] | None = None
        try:
            pending_approval = self._session_store.load_pending_approval(
                workspace=self._workspace,
                session_id=session_id,
            )
            pending_question = self._session_store.load_pending_question(
                workspace=self._workspace,
                session_id=session_id,
            )
            resume_checkpoint = self._session_store.load_resume_checkpoint(
                workspace=self._workspace,
                session_id=session_id,
            )
        except ValueError as exc:
            persistence_error = str(exc)
        current_status = self._current_debug_status(
            result=result,
            active=active,
            pending_approval=pending_approval,
            pending_question=pending_question,
        )
        terminal = result.session.status in {"completed", "failed"}
        resumable = result.session.status == "waiting"
        replayable = bool(result.transcript) or result.output is not None or terminal
        last_relevant_event = self._debug_event(
            next(
                (
                    event
                    for event in reversed(result.transcript)
                    if event.event_type
                    in {
                        "runtime.approval_requested",
                        "runtime.question_requested",
                        "runtime.approval_resolved",
                        RUNTIME_QUESTION_ANSWERED,
                        "runtime.failed",
                        "runtime.tool_completed",
                        "graph.response_ready",
                    }
                ),
                result.transcript[-1] if result.transcript else None,
            )
        )
        last_failure_event = self._debug_event(
            next(
                (
                    event
                    for event in reversed(result.transcript)
                    if event.event_type == "runtime.failed"
                ),
                None,
            )
        )
        last_tool = self._last_tool_summary(result)
        failure = self._debug_failure(
            result=result,
            last_failure_event=last_failure_event,
            last_tool=last_tool,
            pending_approval=pending_approval,
            pending_question=pending_question,
            resume_checkpoint=resume_checkpoint,
            persistence_error=persistence_error,
        )
        suggested_operator_action, operator_guidance = self._operator_guidance(
            current_status=current_status,
            pending_approval=pending_approval,
            pending_question=pending_question,
            active=active,
            terminal=terminal,
            failure=failure,
        )
        return RuntimeSessionDebugSnapshot(
            session=result.session,
            prompt=result.prompt,
            persisted_status=result.status,
            current_status=current_status,
            active=active,
            resumable=resumable,
            replayable=replayable,
            terminal=terminal,
            resume_checkpoint_kind=(
                cast(str, resume_checkpoint.get("kind"))
                if isinstance(resume_checkpoint, dict)
                and isinstance(resume_checkpoint.get("kind"), str)
                else None
            ),
            pending_approval=(
                RuntimeSessionDebugPendingApproval(
                    request_id=pending_approval.request_id,
                    tool_name=pending_approval.tool_name,
                    target_summary=pending_approval.target_summary,
                    reason=pending_approval.reason,
                    policy_mode=pending_approval.policy_mode,
                    arguments=dict(pending_approval.arguments),
                    owner_session_id=pending_approval.owner_session_id,
                    owner_parent_session_id=pending_approval.owner_parent_session_id,
                    delegated_task_id=pending_approval.delegated_task_id,
                )
                if pending_approval is not None
                else None
            ),
            pending_question=(
                RuntimeSessionDebugPendingQuestion(
                    request_id=pending_question.request_id,
                    tool_name=pending_question.tool_name,
                    question_count=len(pending_question.prompts),
                    headers=tuple(prompt.header for prompt in pending_question.prompts),
                )
                if pending_question is not None
                else None
            ),
            last_event_sequence=result.last_event_sequence,
            last_relevant_event=last_relevant_event,
            last_failure_event=last_failure_event,
            failure=failure,
            last_tool=last_tool,
            suggested_operator_action=suggested_operator_action,
            operator_guidance=operator_guidance,
        )

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
        response = self._load_stored_response(session_id=session_id)
        return self._effective_runtime_config_from_metadata(response.session.metadata)

    def effective_category_model_config(
        self, *, session_id: str | None = None
    ) -> dict[str, object]:
        categories, agents, base_model, _base_provider_fallback = self._display_routing_config(
            session_id=session_id
        )
        payload: dict[str, object] = {}
        for category in supported_subagent_categories():
            route = runtime_subagent_route_from_metadata(
                {"delegation": {"mode": "background", "category": category}}
            )
            assert route is not None
            category_config = categories.get(category)
            model = self._delegated_model_for_route_from_configs(
                category=category,
                selected_preset=route.selected_preset,
                request_agent=None,
                categories=categories,
                agents=agents,
                base_model=base_model,
            )
            payload[category] = {
                "model": category_config.model if category_config is not None else None,
                "effective_model": model,
                "selected_preset": route.selected_preset,
                "selected_execution_engine": route.execution_engine,
            }
        return payload

    def effective_agent_model_config(self, *, session_id: str | None = None) -> dict[str, object]:
        _categories, agents, base_model, base_provider_fallback = self._display_routing_config(
            session_id=session_id
        )
        payload: dict[str, object] = {}
        for manifest in list_builtin_agent_manifests():
            preset_agent = agents.get(manifest.id)
            model = preset_agent.model if preset_agent is not None else manifest.model_preference
            if model is None:
                model = base_model
            provider_fallback = self._provider_fallback_for_agent_selection(
                model=model,
                preset_agent=preset_agent,
                base_provider_fallback=base_provider_fallback,
            )
            execution_engine = (
                preset_agent.execution_engine
                if preset_agent is not None and preset_agent.execution_engine is not None
                else manifest.execution_engine
            )
            fallback_models = (
                list(provider_fallback.fallback_models) if provider_fallback is not None else []
            )
            payload[manifest.id] = {
                "model": preset_agent.model if preset_agent is not None else None,
                "fallback_models": fallback_models,
                "effective_model": model,
                "effective_fallback_models": fallback_models,
                "selected_execution_engine": execution_engine,
            }
        return payload

    def _display_routing_config(
        self,
        *,
        session_id: str | None,
    ) -> tuple[
        Mapping[str, RuntimeCategoryConfig],
        Mapping[str, RuntimeAgentConfig],
        str | None,
        RuntimeProviderFallbackConfig | None,
    ]:
        if session_id is None:
            return (
                self._config.categories or {},
                self._config.agents or {},
                self._config.model,
                self._config.provider_fallback,
            )
        validate_session_id(session_id)
        response = self._load_stored_response(session_id=session_id)
        runtime_config = response.session.metadata.get("runtime_config")
        if not isinstance(runtime_config, dict):
            return {}, {}, None, None
        payload = cast(dict[str, object], runtime_config)
        raw_model = payload.get("model")
        base_model = raw_model if isinstance(raw_model, str) else None
        base_provider_fallback = None
        if "provider_fallback" in payload:
            base_provider_fallback = parse_provider_fallback_payload(
                payload.get("provider_fallback"),
                source="persisted runtime_config.provider_fallback",
            )
        categories = parse_runtime_categories_payload(
            payload.get("categories"),
            source="persisted runtime_config.categories",
        )
        agents = parse_runtime_agents_payload(
            payload.get("agents"),
            source="persisted runtime_config.agents",
            hooks=self._config.hooks,
        )
        return categories or {}, agents or {}, base_model, base_provider_fallback

    def refresh_provider_models(self, provider_name: str) -> tuple[str, ...]:
        if not provider_name or "/" in provider_name:
            raise ValueError("provider_name must be a non-empty provider id without '/'")
        _ = self._model_provider_registry.resolve(provider_name)
        models = self._model_provider_registry.refresh_available_models(provider_name)
        self._persist_provider_model_catalog_cache()
        return models

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
            "model_metadata": {
                model: metadata.payload() for model, metadata in catalog.model_metadata.items()
            },
            "refreshed": catalog.refreshed,
            "source": catalog.source,
            "last_refresh_status": catalog.last_refresh_status,
            "last_error": catalog.last_error,
            "discovery_mode": catalog.discovery_mode,
        }

    def _provider_model_catalog_cache_path(self) -> Path:
        return self._workspace / ".voidcode" / "provider-model-catalog.json"

    def _hydrate_provider_model_catalog_cache(self) -> None:
        catalog = self._model_provider_registry.model_catalog
        if catalog is None or catalog:
            return
        cache_path = self._provider_model_catalog_cache_path()
        try:
            raw_payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(raw_payload, dict):
            return
        payload = cast(dict[str, object], raw_payload)
        raw_providers = payload.get("providers")
        if not isinstance(raw_providers, dict):
            return

        hydrated: dict[str, ProviderModelCatalog] = {}
        for provider_name, raw_catalog in cast(dict[object, object], raw_providers).items():
            if not isinstance(provider_name, str) or not provider_name or "/" in provider_name:
                continue
            if not isinstance(raw_catalog, dict):
                continue
            catalog_payload = cast(dict[str, object], raw_catalog)
            raw_models = catalog_payload.get("models", [])
            if not isinstance(raw_models, list):
                continue
            models = tuple(
                raw_model
                for raw_model in cast(list[object], raw_models)
                if isinstance(raw_model, str) and raw_model
            )
            raw_metadata = catalog_payload.get("model_metadata", {})
            metadata_payloads: dict[object, object] = (
                cast(dict[object, object], raw_metadata) if isinstance(raw_metadata, dict) else {}
            )
            model_metadata = {
                model: VoidCodeRuntime._catalog_metadata_from_payload(payload)
                for model, raw_payload in metadata_payloads.items()
                if isinstance(model, str) and isinstance(raw_payload, dict)
                for payload in (cast(dict[str, object], raw_payload),)
            }
            raw_source = catalog_payload.get("source")
            source = raw_source if isinstance(raw_source, str) else "remote"
            raw_status = catalog_payload.get("last_refresh_status")
            last_refresh_status = raw_status if isinstance(raw_status, str) else "ok"
            raw_discovery_mode = catalog_payload.get("discovery_mode")
            discovery_mode = (
                cast(
                    Literal[
                        "configured_endpoint",
                        "configured_base_url",
                        "disabled",
                        "unavailable",
                    ],
                    raw_discovery_mode,
                )
                if raw_discovery_mode
                in {"configured_endpoint", "configured_base_url", "disabled", "unavailable"}
                else "unavailable"
            )
            hydrated[provider_name] = ProviderModelCatalog(
                provider=provider_name,
                models=models,
                refreshed=bool(catalog_payload.get("refreshed", False)),
                model_metadata=model_metadata,
                source=source,
                last_refresh_status=last_refresh_status,
                last_error=(
                    cast(str, catalog_payload["last_error"])
                    if isinstance(catalog_payload.get("last_error"), str)
                    else None
                ),
                discovery_mode=discovery_mode,
            )
        catalog.update(hydrated)

    def _persist_provider_model_catalog_cache(self) -> None:
        catalog = self._model_provider_registry.model_catalog
        if catalog is None:
            return
        cache_path = self._provider_model_catalog_cache_path()
        payload = {
            "version": 1,
            "providers": {
                provider_name: {
                    "provider": entry.provider,
                    "models": list(entry.models),
                    "model_metadata": {
                        model: metadata.payload()
                        for model, metadata in entry.model_metadata.items()
                    },
                    "refreshed": entry.refreshed,
                    "source": entry.source,
                    "last_refresh_status": entry.last_refresh_status,
                    "last_error": entry.last_error,
                    "discovery_mode": entry.discovery_mode,
                }
                for provider_name, entry in sorted(catalog.items())
            },
        }
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        except OSError:
            logger.debug("failed to persist provider model catalog cache", exc_info=True)

    def _metadata_for_provider_model(
        self, provider_name: str, model_name: str
    ) -> ProviderModelMetadata | None:
        catalog = self.provider_model_catalog(provider_name)
        raw_metadata = None if catalog is None else catalog.get("model_metadata")
        if isinstance(raw_metadata, dict):
            metadata_payloads = cast(dict[str, object], raw_metadata)
            raw_payload = metadata_payloads.get(model_name)
            if isinstance(raw_payload, dict):
                payload = cast(dict[str, object], raw_payload)
                return VoidCodeRuntime._contract_metadata_from_payload(payload)
        inferred = infer_model_metadata(provider_name, model_name)
        if inferred is None:
            return None
        return ProviderModelMetadata(
            context_window=inferred.context_window,
            max_input_tokens=inferred.max_input_tokens,
            max_output_tokens=inferred.max_output_tokens,
            supports_tools=inferred.supports_tools,
            supports_vision=inferred.supports_vision,
            supports_streaming=inferred.supports_streaming,
            supports_reasoning=inferred.supports_reasoning,
            supports_json_mode=inferred.supports_json_mode,
            cost_per_input_token=inferred.cost_per_input_token,
            cost_per_output_token=inferred.cost_per_output_token,
            cost_per_cache_read_token=inferred.cost_per_cache_read_token,
            cost_per_cache_write_token=inferred.cost_per_cache_write_token,
            supports_reasoning_effort=inferred.supports_reasoning_effort,
            default_reasoning_effort=inferred.default_reasoning_effort,
            supports_interleaved_reasoning=inferred.supports_interleaved_reasoning,
            modalities_input=inferred.modalities_input,
            modalities_output=inferred.modalities_output,
            model_status=inferred.model_status,
        )

    def _context_window_policy_for_provider_attempt(
        self,
        policy: ContextWindowPolicy,
        *,
        resolved_provider: ResolvedProviderConfig | None,
        provider_attempt: int,
    ) -> ContextWindowPolicy:
        if policy.model_context_window_tokens is not None:
            return policy
        if resolved_provider is None:
            return policy
        provider_target = resolved_provider.target_chain.target_at(provider_attempt)
        if provider_target is None:
            provider_target = resolved_provider.active_target
        provider_name = provider_target.selection.provider
        model_name = provider_target.selection.model
        if provider_name is None or model_name is None:
            return policy
        metadata = self._metadata_for_provider_model(provider_name, model_name)
        if metadata is None or metadata.context_window is None:
            return policy
        return replace(policy, model_context_window_tokens=metadata.context_window)

    def list_provider_summaries(self) -> tuple[ProviderSummary, ...]:
        current_provider = self._current_provider_name()
        providers: list[ProviderSummary] = []
        for provider_name in self._model_provider_registry.providers:
            providers.append(
                ProviderSummary(
                    name=provider_name,
                    label=self._provider_label(provider_name),
                    configured=self._provider_is_configured(provider_name),
                    current=provider_name == current_provider,
                )
            )
        providers.sort(key=lambda item: item.name)
        return tuple(providers)

    def provider_models_result(self, provider_name: str) -> ProviderModelsResult:
        configured = self._provider_is_configured(provider_name)
        catalog = self.provider_model_catalog(provider_name)
        if configured and catalog is None:
            _ = self.refresh_provider_models(provider_name)
            catalog = self.provider_model_catalog(provider_name)
        if catalog is None:
            return ProviderModelsResult(
                provider=provider_name,
                configured=configured,
                models=(),
            )
        return ProviderModelsResult(
            provider=provider_name,
            configured=configured,
            models=tuple(cast(list[str], catalog["models"])),
            model_metadata={
                model: VoidCodeRuntime._contract_metadata_from_payload(payload)
                for model, raw_payload in cast(
                    dict[str, object], catalog.get("model_metadata", {})
                ).items()
                if isinstance(raw_payload, dict)
                for payload in (cast(dict[str, object], raw_payload),)
            },
            source=cast(str | None, catalog["source"]),
            last_refresh_status=cast(str | None, catalog["last_refresh_status"]),
            last_error=cast(str | None, catalog["last_error"]),
            discovery_mode=cast(str | None, catalog["discovery_mode"]),
        )

    def provider_readiness(self, *, session_id: str | None = None) -> ProviderReadinessResult:
        effective_config = self.effective_runtime_config(session_id=session_id)
        return self._provider_readiness_for_effective_config(effective_config)

    def _provider_readiness_for_effective_config(
        self, effective_config: EffectiveRuntimeConfig
    ) -> ProviderReadinessResult:
        active_target = effective_config.resolved_provider.active_target
        provider_name = active_target.selection.provider
        model_name = active_target.selection.model
        fallback_chain = tuple(
            _provider_target_label(target)
            for target in effective_config.resolved_provider.target_chain.all_targets
        )
        streaming_configured = None
        streaming_supported = None
        context_window = None
        max_output_tokens = None
        if provider_name is not None and model_name is not None:
            metadata = self._metadata_for_provider_model(provider_name, model_name)
            if metadata is not None:
                streaming_supported = metadata.supports_streaming
                context_window = metadata.context_window
                max_output_tokens = metadata.max_output_tokens
        if effective_config.context_window is not None:
            if effective_config.context_window.model_context_window_tokens is not None:
                context_window = effective_config.context_window.model_context_window_tokens
            if effective_config.context_window.reserved_output_tokens is not None:
                max_output_tokens = effective_config.context_window.reserved_output_tokens
        configured = provider_name is not None and self._provider_is_configured(provider_name)
        auth_present, auth_failure_kind, auth_message = self._provider_auth_presence(provider_name)
        validation_status = "ready"
        ok = configured and auth_present is not False
        guidance = "Provider/model configuration is ready enough to run."
        if provider_name is None or model_name is None:
            validation_status = "missing_model"
            ok = False
            guidance = "Configure a provider/model, for example model: 'openai/gpt-4o'."
        elif auth_present is False and auth_failure_kind == "invalid_model":
            validation_status = auth_failure_kind
            ok = False
            guidance = auth_message or guidance_for_provider_error_kind("invalid_model")
        elif not configured:
            validation_status = "unconfigured"
            ok = False
            guidance = "Add provider credentials in environment variables or .voidcode.json."
        elif auth_present is False:
            validation_status = auth_failure_kind or "missing_auth"
            ok = False
            guidance = auth_message or guidance_for_provider_error_kind("missing_auth")
        elif streaming_supported is False:
            validation_status = "streaming_unsupported"
            ok = False
            guidance = guidance_for_provider_error_kind("unsupported_feature")
        return ProviderReadinessResult(
            provider=provider_name,
            model=model_name,
            configured=configured,
            ok=ok,
            status=validation_status,
            guidance=guidance,
            auth_present=auth_present,
            streaming_configured=streaming_configured,
            streaming_supported=streaming_supported,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            fallback_chain=fallback_chain,
        )

    def inspect_provider(self, provider_name: str) -> ProviderInspectResult:
        if not provider_name or "/" in provider_name:
            raise ValueError("provider_name must be a non-empty provider id without '/'")
        summary = next(
            (
                provider
                for provider in self.list_provider_summaries()
                if provider.name == provider_name
            ),
            ProviderSummary(
                name=provider_name,
                label=self._provider_label(provider_name),
                configured=self._provider_is_configured(provider_name),
                current=provider_name == self._current_provider_name(),
            ),
        )
        validation = self.validate_provider_credentials(provider_name)
        models = self.provider_models_result(provider_name)
        current_model = (
            self._provider_model.selection.model
            if self._provider_model.selection.provider == provider_name
            else None
        )
        current_metadata = (
            self._metadata_for_provider_model(provider_name, current_model)
            if current_model is not None
            else None
        )
        return ProviderInspectResult(
            summary=summary,
            models=models,
            validation=validation,
            current_model=current_model,
            current_model_metadata=current_metadata,
            readiness=self.provider_readiness() if summary.current else None,
        )

    def validate_provider_credentials(self, provider_name: str) -> ProviderValidationResult:
        if not provider_name or "/" in provider_name:
            raise ValueError("provider_name must be a non-empty provider id without '/'")
        if not self._provider_is_configured(provider_name):
            result = self.provider_models_result(provider_name)
            return ProviderValidationResult(
                provider=provider_name,
                configured=False,
                ok=False,
                status="unconfigured",
                message="Provider is not configured.",
                source=result.source,
                last_error=result.last_error,
                discovery_mode=result.discovery_mode,
                failure_kind="missing_auth",
                guidance="Add provider credentials in environment variables or .voidcode.json.",
            )
        auth_present, auth_failure_kind, auth_message = self._provider_auth_presence(provider_name)
        if auth_present is False:
            return ProviderValidationResult(
                provider=provider_name,
                configured=True,
                ok=False,
                status=auth_failure_kind or "missing_auth",
                message=auth_message or "Provider authentication is missing.",
                failure_kind=auth_failure_kind or "missing_auth",
                guidance=auth_message or guidance_for_provider_error_kind("missing_auth"),
            )
        _ = self.refresh_provider_models(provider_name)
        result = self.provider_models_result(provider_name)
        if result.last_refresh_status == "failed":
            return ProviderValidationResult(
                provider=provider_name,
                configured=True,
                ok=False,
                status="failed",
                message=result.last_error or "Provider credential validation failed.",
                source=result.source,
                last_error=result.last_error,
                discovery_mode=result.discovery_mode,
                failure_kind="transient_failure",
                guidance=guidance_for_provider_error_kind("transient_failure"),
            )
        status = result.last_refresh_status or "ok"
        ok = status == "ok"
        message = (
            "Remote provider validation succeeded."
            if ok
            else "Provider credentials are configured; remote validation is unavailable."
        )
        return ProviderValidationResult(
            provider=provider_name,
            configured=True,
            ok=ok,
            status=status,
            message=message,
            source=result.source,
            last_error=result.last_error,
            discovery_mode=result.discovery_mode,
            guidance=(
                "Provider model discovery succeeded."
                if ok
                else "Credentials are present, but remote validation could not confirm readiness."
            ),
        )

    def _provider_auth_presence(
        self, provider_name: str | None
    ) -> tuple[bool | None, str | None, str | None]:
        if provider_name is None:
            return None, None, None
        oauth_presence = self._oauth_provider_auth_presence(provider_name)
        if oauth_presence is not None:
            return oauth_presence
        try:
            result = self._provider_auth_resolver.authorize(
                ProviderAuthAuthorizeRequest(provider=provider_name)
            )
        except ProviderAuthResolutionError as exc:
            if exc.code == "missing_credentials":
                return False, "missing_auth", str(exc)
            return False, exc.provider_error_kind, str(exc)
        return result.status == "authorized", None, None

    def _oauth_provider_auth_presence(
        self, provider_name: str
    ) -> tuple[bool | None, str | None, str | None] | None:
        providers = self._config.providers
        if providers is None:
            return None
        if provider_name == "google":
            config = providers.google
            auth = None if config is None else config.auth
            if auth is None or auth.method != "oauth":
                return None
            if auth.access_token:
                return True, None, None
            return (
                False,
                "missing_auth",
                "provider auth field 'google.access_token' must be provided for google oauth auth",
            )
        if provider_name == "copilot":
            config = providers.copilot
            auth = None if config is None else config.auth
            if auth is None or auth.method != "oauth":
                return None
            if auth.token or (auth.token_env_var and os.environ.get(auth.token_env_var)):
                return True, None, None
            return (
                False,
                "missing_auth",
                "provider auth field 'copilot.token' must be provided for copilot oauth auth",
            )
        return None

    @staticmethod
    def _optional_positive_int(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if value > 0 else None

    @staticmethod
    def _optional_bool(value: object) -> bool | None:
        return value if isinstance(value, bool) else None

    @staticmethod
    def _optional_positive_float(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        normalized = float(value)
        return normalized if normalized > 0 else None

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _optional_string_tuple(value: object) -> tuple[str, ...] | None:
        if not isinstance(value, list | tuple):
            return None
        raw_items = cast(Iterable[object], value)
        items = tuple(item for item in raw_items if isinstance(item, str) and item)
        return items or None

    @staticmethod
    def _catalog_metadata_from_payload(
        payload: dict[str, object],
    ) -> CatalogProviderModelMetadata:
        return CatalogProviderModelMetadata(
            context_window=VoidCodeRuntime._optional_positive_int(payload.get("context_window")),
            max_input_tokens=VoidCodeRuntime._optional_positive_int(
                payload.get("max_input_tokens")
            ),
            max_output_tokens=VoidCodeRuntime._optional_positive_int(
                payload.get("max_output_tokens")
            ),
            supports_tools=VoidCodeRuntime._optional_bool(payload.get("supports_tools")),
            supports_vision=VoidCodeRuntime._optional_bool(payload.get("supports_vision")),
            supports_streaming=VoidCodeRuntime._optional_bool(payload.get("supports_streaming")),
            supports_reasoning=VoidCodeRuntime._optional_bool(payload.get("supports_reasoning")),
            supports_json_mode=VoidCodeRuntime._optional_bool(payload.get("supports_json_mode")),
            cost_per_input_token=VoidCodeRuntime._optional_positive_float(
                payload.get("cost_per_input_token")
            ),
            cost_per_output_token=VoidCodeRuntime._optional_positive_float(
                payload.get("cost_per_output_token")
            ),
            cost_per_cache_read_token=VoidCodeRuntime._optional_positive_float(
                payload.get("cost_per_cache_read_token")
            ),
            cost_per_cache_write_token=VoidCodeRuntime._optional_positive_float(
                payload.get("cost_per_cache_write_token")
            ),
            supports_reasoning_effort=VoidCodeRuntime._optional_bool(
                payload.get("supports_reasoning_effort")
            ),
            default_reasoning_effort=VoidCodeRuntime._optional_string(
                payload.get("default_reasoning_effort")
            ),
            supports_interleaved_reasoning=VoidCodeRuntime._optional_bool(
                payload.get("supports_interleaved_reasoning")
            ),
            modalities_input=VoidCodeRuntime._optional_string_tuple(
                payload.get("modalities_input")
            ),
            modalities_output=VoidCodeRuntime._optional_string_tuple(
                payload.get("modalities_output")
            ),
            model_status=VoidCodeRuntime._optional_string(payload.get("model_status")),
        )

    @staticmethod
    def _contract_metadata_from_payload(payload: dict[str, object]) -> ProviderModelMetadata:
        catalog_metadata = VoidCodeRuntime._catalog_metadata_from_payload(payload)
        return ProviderModelMetadata(
            context_window=catalog_metadata.context_window,
            max_input_tokens=catalog_metadata.max_input_tokens,
            max_output_tokens=catalog_metadata.max_output_tokens,
            supports_tools=catalog_metadata.supports_tools,
            supports_vision=catalog_metadata.supports_vision,
            supports_streaming=catalog_metadata.supports_streaming,
            supports_reasoning=catalog_metadata.supports_reasoning,
            supports_json_mode=catalog_metadata.supports_json_mode,
            cost_per_input_token=catalog_metadata.cost_per_input_token,
            cost_per_output_token=catalog_metadata.cost_per_output_token,
            cost_per_cache_read_token=catalog_metadata.cost_per_cache_read_token,
            cost_per_cache_write_token=catalog_metadata.cost_per_cache_write_token,
            supports_reasoning_effort=catalog_metadata.supports_reasoning_effort,
            default_reasoning_effort=catalog_metadata.default_reasoning_effort,
            supports_interleaved_reasoning=catalog_metadata.supports_interleaved_reasoning,
            modalities_input=catalog_metadata.modalities_input,
            modalities_output=catalog_metadata.modalities_output,
            model_status=catalog_metadata.model_status,
        )

    def list_agent_summaries(self) -> tuple[AgentSummary, ...]:
        summaries: list[AgentSummary] = []
        configured_agent = self._config.agent
        for manifest in list_builtin_agent_manifests():
            if manifest.mode != "primary":
                continue

            agent_config = (
                configured_agent
                if configured_agent is not None and configured_agent.preset == manifest.id
                else None
            )
            execution_engine = (
                agent_config.execution_engine
                if agent_config is not None and agent_config.execution_engine is not None
                else manifest.execution_engine
                if manifest.execution_engine is not None
                else self._config.execution_engine
            )
            agent_model = agent_config.model if agent_config is not None else None
            model = (
                agent_model
                if agent_model is not None
                else manifest.model_preference
                if agent_config is not None and manifest.model_preference is not None
                else self._config.model
            )
            provider_fallback = (
                agent_config.provider_fallback
                if agent_config is not None and agent_config.provider_fallback is not None
                else self._config.provider_fallback
            )
            resolved_provider = resolve_provider_config(
                model,
                self._provider_fallback_for_agent_selection(
                    model=model,
                    preset_agent=agent_config,
                    base_provider_fallback=self._config.provider_fallback,
                ),
                registry=self._model_provider_registry,
            )
            resolved_model = resolved_provider.model or model
            active_selection = resolved_provider.active_target.selection
            model_source = (
                "configured"
                if agent_model is not None
                else "builtin"
                if agent_config is not None and manifest.model_preference is not None
                else "configured"
                if self._config.model is not None or provider_fallback is not None
                else None
            )
            configured = (
                agent_config is not None
                or self._config.model is not None
                or provider_fallback is not None
            )
            summaries.append(
                AgentSummary(
                    id=manifest.id,
                    label=manifest.name,
                    description=manifest.description,
                    mode=manifest.mode,
                    selectable=manifest.id in _EXECUTABLE_AGENT_PRESETS,
                    configured=configured,
                    execution_engine=execution_engine,
                    model=resolved_model,
                    model_label=active_selection.model,
                    model_source=model_source,
                    provider=active_selection.provider,
                    fallback_chain=tuple(
                        _provider_target_label(target)
                        for target in resolved_provider.target_chain.all_targets
                    ),
                )
            )
        return tuple(summaries)

    def current_status(self) -> RuntimeStatusSnapshot:
        git = self._git_status_snapshot()
        lsp_state = self.current_lsp_state()
        mcp_state = self.current_mcp_state()
        acp_state = self.current_acp_state()
        lsp_servers = tuple(lsp_state.servers.values())
        lsp_status = (
            "unconfigured"
            if lsp_state.mode != "managed" or not lsp_state.configuration.configured_enabled
            else "failed"
            if any(server.status == "failed" for server in lsp_servers)
            else "running"
            if any(server.status == "running" for server in lsp_servers)
            else "stopped"
        )
        lsp_error = next(
            (server.last_error for server in lsp_servers if server.last_error),
            None,
        )
        mcp_servers = tuple(mcp_state.servers.values())
        mcp_status = (
            "unconfigured"
            if mcp_state.mode != "managed" or not mcp_state.configuration.configured_enabled
            else "failed"
            if any(server.status == "failed" for server in mcp_servers)
            else "running"
            if any(server.status == "running" for server in mcp_servers)
            else "stopped"
        )
        mcp_error = next((server.error for server in mcp_servers if server.error), None)
        return RuntimeStatusSnapshot(
            git=git,
            lsp=CapabilityStatusSnapshot(state=lsp_status, error=lsp_error),
            mcp=CapabilityStatusSnapshot(
                state=mcp_status,
                error=mcp_error,
                details={
                    "configured_server_count": len(mcp_servers),
                    "running_server_count": sum(
                        1 for server in mcp_servers if server.status == "running"
                    ),
                    "failed_server_count": sum(
                        1 for server in mcp_servers if server.status == "failed"
                    ),
                    "retry_available": any(server.retry_available for server in mcp_servers),
                    "servers": [
                        {
                            "server": server.server_name,
                            "status": server.status,
                            "workspace_root": server.workspace_root,
                            "stage": server.stage,
                            "error": server.error,
                            "command": list(server.command),
                            "retry_available": server.retry_available,
                        }
                        for server in mcp_servers
                    ],
                },
            ),
            acp=self._acp_status_snapshot(acp_state),
        )

    @staticmethod
    def _acp_status_snapshot(acp_state: AcpAdapterState) -> CapabilityStatusSnapshot:
        acp_status = (
            "unconfigured"
            if acp_state.mode != "managed" or not acp_state.configuration.configured_enabled
            else "failed"
            if acp_state.status == "failed"
            else "running"
            if acp_state.available and acp_state.status == "connected"
            else "stopped"
        )
        details: dict[str, object] = {
            "mode": acp_state.mode,
            "configured": acp_state.configured,
            "configured_enabled": acp_state.configuration.configured_enabled,
            "available": acp_state.available,
            "status": acp_state.status,
        }
        if acp_state.last_request_type is not None:
            details["last_request_type"] = acp_state.last_request_type
        if acp_state.last_request_id is not None:
            details["last_request_id"] = acp_state.last_request_id
        if acp_state.last_event_type is not None:
            details["last_event_type"] = acp_state.last_event_type
        if acp_state.last_delegation is not None:
            details["last_delegation"] = acp_state.last_delegation.as_payload()
        return CapabilityStatusSnapshot(
            state=acp_status,
            error=acp_state.last_error,
            details=details,
        )

    def retry_mcp_connections(self) -> RuntimeStatusSnapshot:
        self._mcp_manager.retry_connections(workspace=self._workspace)
        try:
            self._refresh_mcp_tools()
        except Exception:
            logger.debug("failed to refresh MCP tools after retry", exc_info=True)
        return self.current_status()

    def review_snapshot(self) -> WorkspaceReviewSnapshot:
        return WorkspaceReviewService(workspace=self._workspace).snapshot(
            git=self._git_status_snapshot()
        )

    def review_diff(self, path: str) -> ReviewFileDiff:
        return WorkspaceReviewService(workspace=self._workspace).diff(
            path=path,
            git=self._git_status_snapshot(),
        )

    def _git_status_snapshot(self) -> GitStatusSnapshot:
        result = subprocess.run(
            ["git", "-C", str(self._workspace), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return GitStatusSnapshot(
                state="git_ready", root=result.stdout.strip() or str(self._workspace)
            )
        stderr = result.stderr.strip()
        if "not a git repository" in stderr.lower():
            return GitStatusSnapshot(state="not_git_repo", root=None, error=stderr or None)
        return GitStatusSnapshot(
            state="git_error", root=None, error=stderr or result.stdout.strip() or None
        )

    def _current_provider_name(self) -> str | None:
        active_target = self._resolved_provider_config.active_target
        selection = active_target.selection
        return selection.provider

    @staticmethod
    def _provider_label(provider_name: str) -> str:
        return {
            "opencode": "OpenCode",
            "opencode-go": "OpenCode Go",
            "openai": "OpenAI",
            "anthropic": "Anthropic",
            "google": "Google",
            "copilot": "Copilot",
            "litellm": "LiteLLM",
            "deepseek": "DeepSeek",
            "glm": "GLM",
            "grok": "Grok",
            "minimax": "MiniMax",
            "kimi": "Kimi",
            "qwen": "Qwen",
        }.get(provider_name, provider_name)

    def _provider_is_configured(self, provider_name: str) -> bool:
        providers = self._config.providers
        if providers is None:
            return False
        if provider_name == "openai":
            return providers.openai is not None
        if provider_name == "anthropic":
            return providers.anthropic is not None
        if provider_name == "google":
            return providers.google is not None
        if provider_name == "copilot":
            return providers.copilot is not None
        if provider_name == "litellm":
            return providers.litellm is not None
        if provider_name == "deepseek":
            return providers.deepseek is not None
        if provider_name == "glm":
            return providers.glm is not None
        if provider_name == "grok":
            return providers.grok is not None
        if provider_name == "minimax":
            return providers.minimax is not None
        if provider_name == "kimi":
            return providers.kimi is not None
        if provider_name == "opencode-go":
            return providers.opencode_go is not None
        if provider_name == "qwen":
            return providers.qwen is not None
        return provider_name in providers.custom

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
        if initial_agent is None and self._config.execution_engine == "provider":
            initial_agent = RuntimeAgentConfig(preset="leader")
        if initial_agent is not None:
            initial_agent = parse_runtime_agent_payload(
                serialize_runtime_agent_config(initial_agent),
                source="runtime config agent",
                hooks=self._config.hooks,
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
            resolved_provider=self._resolved_provider_config,
            agent=initial_agent,
            context_window=self._config.context_window,
        )
        self._graph_cache = {}
        if self._graph_override is not None:
            self._graph = self._graph_override
        elif self._can_build_graph_for_effective_config(self._initial_effective_config):
            self._graph = self._build_graph_for_engine_from_config(self._initial_effective_config)
        else:
            self._graph = None

    @staticmethod
    def _debug_event(event: EventEnvelope | None) -> RuntimeSessionDebugEvent | None:
        if event is None:
            return None
        return RuntimeSessionDebugEvent(
            sequence=event.sequence,
            event_type=event.event_type,
            source=event.source,
            payload=dict(event.payload),
        )

    @staticmethod
    def _current_debug_status(
        *,
        result: RuntimeSessionResult,
        active: bool,
        pending_approval: PendingApproval | None,
        pending_question: PendingQuestion | None,
    ) -> str:
        if active and result.session.status == "running":
            return "running"
        if pending_approval is not None:
            return "waiting_for_approval"
        if pending_question is not None:
            return "waiting_for_question"
        if active and result.session.status == "waiting":
            return "waiting_active"
        return result.session.status

    @staticmethod
    def _debug_failure(
        *,
        result: RuntimeSessionResult,
        last_failure_event: RuntimeSessionDebugEvent | None,
        last_tool: RuntimeSessionDebugToolSummary | None,
        pending_approval: PendingApproval | None,
        pending_question: PendingQuestion | None,
        resume_checkpoint: dict[str, object] | None,
        persistence_error: str | None,
    ) -> RuntimeSessionDebugFailure | None:
        if persistence_error is not None:
            return RuntimeSessionDebugFailure(
                classification="session_state_inconsistency",
                message=persistence_error,
            )
        if (
            inconsistency_message := VoidCodeRuntime._debug_session_state_inconsistency(
                result=result,
                pending_approval=pending_approval,
                pending_question=pending_question,
                resume_checkpoint=resume_checkpoint,
            )
        ) is not None:
            return RuntimeSessionDebugFailure(
                classification="session_state_inconsistency",
                message=inconsistency_message,
            )
        message = None
        classification = "runtime_internal_failure"
        if last_failure_event is not None:
            provider_error_kind = last_failure_event.payload.get("provider_error_kind")
            if isinstance(provider_error_kind, str) and provider_error_kind:
                classification = "provider_failure"
            raw_error = last_failure_event.payload.get("error")
            if raw_error is not None:
                message = str(raw_error)
        elif result.error is not None:
            message = result.error
        if message is None:
            if last_tool is not None and last_tool.status == "error":
                return RuntimeSessionDebugFailure(
                    classification="tool_execution_failure",
                    message=last_tool.summary,
                )
            return None
        lowered = message.lower()
        if "permission denied" in lowered:
            classification = "approval_denied"
        elif pending_approval is not None or "approval" in lowered or "question" in lowered:
            classification = "approval_interruption"
        elif "cancel" in lowered:
            classification = "cancelled"
        elif last_tool is not None and last_tool.status == "error":
            classification = "tool_execution_failure"
        elif "tool" in lowered:
            classification = "tool_execution_failure"
        return RuntimeSessionDebugFailure(classification=classification, message=message)

    @staticmethod
    def _debug_session_state_inconsistency(
        *,
        result: RuntimeSessionResult,
        pending_approval: PendingApproval | None,
        pending_question: PendingQuestion | None,
        resume_checkpoint: dict[str, object] | None,
    ) -> str | None:
        checkpoint_kind = (
            cast(str, resume_checkpoint.get("kind"))
            if isinstance(resume_checkpoint, dict)
            and isinstance(resume_checkpoint.get("kind"), str)
            else None
        )
        if result.session.status == "waiting":
            if pending_approval is None and pending_question is None:
                return "waiting session is missing pending approval/question state"
            if pending_approval is not None and checkpoint_kind != "approval_wait":
                return "pending approval does not match the persisted resume checkpoint"
            if pending_question is not None and checkpoint_kind != "question_wait":
                return "pending question does not match the persisted resume checkpoint"
        if result.session.status in {"completed", "failed"}:
            if checkpoint_kind not in {None, "terminal"}:
                return "terminal session resume checkpoint does not match persisted terminal state"
            if pending_approval is not None or pending_question is not None:
                return "terminal session still has pending approval/question state"
        if result.session.status == "running" and checkpoint_kind == "terminal":
            return "running session should not carry a terminal resume checkpoint"
        return None

    @staticmethod
    def _last_tool_summary(result: RuntimeSessionResult) -> RuntimeSessionDebugToolSummary | None:
        for event in reversed(result.transcript):
            if event.event_type != "runtime.tool_completed":
                continue
            payload = event.payload
            tool_name = payload.get("tool")
            if not isinstance(tool_name, str) or not tool_name:
                continue
            raw_status = payload.get("status")
            status = raw_status if isinstance(raw_status, str) and raw_status else "ok"
            if status not in {"ok", "error"}:
                status = "error" if payload.get("error") is not None else "ok"
            summary_source = payload.get("error") if status == "error" else payload.get("content")
            summary = str(summary_source).strip() if summary_source is not None else tool_name
            if len(summary) > 160:
                summary = summary[:157] + "..."
            arguments = payload.get("arguments")
            return RuntimeSessionDebugToolSummary(
                tool_name=tool_name,
                status=status,
                summary=summary,
                arguments=(
                    dict(cast(dict[str, object], arguments)) if isinstance(arguments, dict) else {}
                ),
                sequence=event.sequence,
            )
        return None

    @staticmethod
    def _operator_guidance(
        *,
        current_status: str,
        pending_approval: PendingApproval | None,
        pending_question: PendingQuestion | None,
        active: bool,
        terminal: bool,
        failure: RuntimeSessionDebugFailure | None,
    ) -> tuple[str, str]:
        if pending_approval is not None:
            return (
                "resolve_approval",
                "Resolve approval request "
                f"{pending_approval.request_id} for {pending_approval.tool_name}.",
            )
        if pending_question is not None:
            return (
                "answer_question",
                f"Answer pending question request {pending_question.request_id} before resuming.",
            )
        if failure is not None and failure.classification == "session_state_inconsistency":
            return (
                "inspect_failure",
                "Inspect persisted session state before attempting resume or replay.",
            )
        if active:
            return ("wait", "Session is currently active in the runtime.")
        if terminal and failure is not None:
            return ("inspect_failure", f"Inspect {failure.classification} and rerun if needed.")
        if terminal:
            return ("replay", "Session is terminal; replay or inspect transcript if needed.")
        if current_status == "waiting_active":
            return (
                "inspect_wait",
                "Session is waiting but still marked active; inspect runtime ownership.",
            )
        return ("inspect_session", "Inspect the persisted session state.")

    def _active_only_session_debug_snapshot(
        self,
        *,
        session_id: str,
    ) -> RuntimeSessionDebugSnapshot:
        active_metadata = self._active_session_metadata(session_id) or {}
        request_metadata = active_metadata.get("request_metadata")
        session_metadata = {
            **(
                dict(cast(dict[str, object], request_metadata))
                if isinstance(request_metadata, dict)
                else {}
            ),
            "workspace": str(self._workspace),
        }
        prompt = (
            cast(str, active_metadata["prompt"])
            if isinstance(active_metadata.get("prompt"), str)
            else ""
        )
        session = SessionState(
            session=SessionRef(id=session_id),
            status="running",
            turn=1,
            metadata=session_metadata,
        )
        return RuntimeSessionDebugSnapshot(
            session=session,
            prompt=prompt,
            persisted_status="running",
            current_status="running",
            active=True,
            resumable=False,
            replayable=False,
            terminal=False,
            suggested_operator_action="wait",
            operator_guidance="Session is currently active in the runtime.",
        )

    @staticmethod
    def _should_prefer_active_debug_snapshot(
        *,
        result: RuntimeSessionResult,
        active_metadata: dict[str, object] | None,
    ) -> bool:
        if active_metadata is None:
            return False
        active_run_id = active_metadata.get("run_id")
        persisted_run_id = VoidCodeRuntime._run_id_from_session_metadata(result.session.metadata)
        if isinstance(active_run_id, str) and active_run_id != persisted_run_id:
            return True
        request_metadata = active_metadata.get("request_metadata")
        if not isinstance(request_metadata, dict):
            return False
        active_request_metadata = VoidCodeRuntime._fresh_request_metadata(
            cast(RuntimeRequestMetadataPayload, request_metadata)
        )
        persisted_request_metadata = VoidCodeRuntime._request_metadata_from_session_metadata(
            result.session.metadata
        )
        if active_request_metadata != persisted_request_metadata:
            return True
        active_prompt = active_metadata.get("prompt")
        return isinstance(active_prompt, str) and active_prompt != result.prompt

    @staticmethod
    def _request_metadata_from_session_metadata(metadata: dict[str, object]) -> dict[str, object]:
        request_metadata_keys = {
            "abort_requested",
            "agent",
            "delegation",
            "max_steps",
            "provider_stream",
            "skills",
            "background_run",
            "background_task_id",
        }
        request_metadata = {
            key: value for key, value in metadata.items() if key in request_metadata_keys
        }
        return VoidCodeRuntime._fresh_request_metadata(
            cast(RuntimeRequestMetadataPayload, request_metadata)
        )

    @staticmethod
    def _run_id_from_session_metadata(metadata: dict[str, object]) -> str | None:
        runtime_state = metadata.get("runtime_state")
        if not isinstance(runtime_state, dict):
            return None
        runtime_state_dict = cast(dict[str, object], runtime_state)
        run_id = runtime_state_dict.get("run_id")
        return run_id if isinstance(run_id, str) and run_id else None

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> RuntimeResponse:
        validate_session_id(session_id)
        if approval_request_id is None and approval_decision is None:
            response = self._load_stored_response(session_id=session_id)
            self._background_task_supervisor.reconcile_parent_background_task_events_for_session(
                parent_session_id=session_id
            )
            response = self._load_stored_response(session_id=session_id)
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
        self._background_task_supervisor.finalize_background_task_from_session_response(
            session_response=response
        )
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
            response = self._load_stored_response(session_id=session_id)
            self._background_task_supervisor.reconcile_parent_background_task_events_for_session(
                parent_session_id=session_id
            )
            response = self._load_stored_response(session_id=session_id)
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
        self._validate_waiting_request_target_ownership(
            session_id=session_id,
            request_id=approval_request_id,
            request_kind="approval",
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

    def _validate_question_targets_owned_request(
        self,
        *,
        session_id: str,
        question_request_id: str,
    ) -> None:
        self._validate_waiting_request_target_ownership(
            session_id=session_id,
            request_id=question_request_id,
            request_kind="question",
        )

    def _validate_waiting_request_target_ownership(
        self,
        *,
        session_id: str,
        request_id: str,
        request_kind: Literal["approval", "question"],
    ) -> None:
        wrong_target_error = (
            "approval resume must target the child session that owns the approval request"
            if request_kind == "approval"
            else "question answer must target the child session that owns the question request"
        )
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
                    self._waiting_request_id_from_response(
                        child_response,
                        request_kind=request_kind,
                    )
                    if child_response is not None
                    else (
                        task.approval_request_id
                        if request_kind == "approval"
                        else task.question_request_id
                    )
                )
                if owned_request_id == request_id:
                    raise ValueError(wrong_target_error)

    def answer_question(
        self,
        session_id: str,
        *,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> RuntimeResponse:
        validate_session_id(session_id)
        self._validate_question_targets_owned_request(
            session_id=session_id,
            question_request_id=question_request_id,
        )
        _, response = self._answer_pending_question_response(
            session_id=session_id,
            question_request_id=question_request_id,
            responses=responses,
        )
        self._background_task_supervisor.finalize_background_task_from_session_response(
            session_response=response
        )
        return response

    def answer_question_stream(
        self,
        session_id: str,
        *,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> Iterator[RuntimeStreamChunk]:
        validate_session_id(session_id)
        self._validate_question_targets_owned_request(
            session_id=session_id,
            question_request_id=question_request_id,
        )
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
            request_event_sequence=approval_event.sequence,
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

    @staticmethod
    def _request_event_and_resolution_state(
        events: tuple[EventEnvelope, ...],
        *,
        request_kind: Literal["approval", "question"],
        request_id: str,
    ) -> tuple[EventEnvelope | None, bool]:
        request_event_type = (
            RUNTIME_APPROVAL_REQUESTED if request_kind == "approval" else RUNTIME_QUESTION_REQUESTED
        )
        resolution_event_type = (
            "runtime.approval_resolved" if request_kind == "approval" else RUNTIME_QUESTION_ANSWERED
        )
        request_event: EventEnvelope | None = None
        resolved = False
        for event in events:
            event_request_id = event.payload.get("request_id")
            if event_request_id != request_id:
                continue
            if event.event_type == request_event_type:
                request_event = event
            elif event.event_type == resolution_event_type:
                resolved = True
        return request_event, resolved

    def _validate_pending_approval_matches_recorded_request(
        self,
        *,
        stored: RuntimeResponse,
        pending: PendingApproval,
        checkpoint: dict[str, object] | None,
    ) -> None:
        # Referenced via extracted resume collaborator.
        request_event, resolved = self._request_event_and_resolution_state(
            stored.events,
            request_kind="approval",
            request_id=pending.request_id,
        )
        if resolved:
            raise ValueError(
                "approval request was already resolved; stale approval replay is not allowed"
            )
        if request_event is None:
            if checkpoint is None:
                raise ValueError(
                    "persisted pending approval has no matching approval request event"
                )
            if checkpoint.get("pending_approval_request_id") != pending.request_id:
                raise ValueError(
                    "persisted approval resume checkpoint request id "
                    "does not match pending approval"
                )
            if (
                checkpoint.get("pending_approval_tool_name") != pending.tool_name
                or checkpoint.get("pending_approval_arguments") != pending.arguments
            ):
                raise ValueError(
                    "persisted pending approval no longer matches "
                    "the recorded approval request payload"
                )
            if checkpoint.get("pending_approval_owner_session_id") != pending.owner_session_id:
                raise ValueError(
                    "persisted pending approval owner_session_id "
                    "does not match the recorded approval request"
                )
            if (
                checkpoint.get("pending_approval_owner_parent_session_id")
                != pending.owner_parent_session_id
            ):
                raise ValueError(
                    "persisted pending approval owner_parent_session_id "
                    "does not match the recorded approval request"
                )
            if checkpoint.get("pending_approval_delegated_task_id") != pending.delegated_task_id:
                raise ValueError(
                    "persisted pending approval delegated_task_id "
                    "does not match the recorded approval request"
                )
            checkpoint_sequence = checkpoint.get("pending_approval_request_event_sequence")
            if (
                pending.request_event_sequence is not None
                and checkpoint_sequence is not None
                and checkpoint_sequence != pending.request_event_sequence
            ):
                raise ValueError(
                    "persisted pending approval sequence "
                    "does not match the recorded approval request"
                )
            return
        if (
            pending.request_event_sequence is not None
            and request_event.sequence != pending.request_event_sequence
        ):
            raise ValueError(
                "persisted pending approval sequence does not match the recorded approval request"
            )
        payload = request_event.payload
        if (
            payload.get("tool") != pending.tool_name
            or payload.get("arguments") != pending.arguments
        ):
            raise ValueError(
                "persisted pending approval no longer matches the recorded approval request payload"
            )
        if payload.get("owner_session_id") != pending.owner_session_id:
            raise ValueError(
                "persisted pending approval owner_session_id "
                "does not match the recorded approval request"
            )
        if payload.get("owner_parent_session_id") != pending.owner_parent_session_id:
            raise ValueError(
                "persisted pending approval owner_parent_session_id "
                "does not match the recorded approval request"
            )
        if payload.get("delegated_task_id") != pending.delegated_task_id:
            raise ValueError(
                "persisted pending approval delegated_task_id "
                "does not match the recorded approval request"
            )

    def _validate_pending_question_matches_recorded_request(
        self,
        *,
        stored: RuntimeResponse,
        pending: PendingQuestion,
        checkpoint: dict[str, object] | None,
    ) -> None:
        # Referenced via extracted resume collaborator.
        request_event, resolved = self._request_event_and_resolution_state(
            stored.events,
            request_kind="question",
            request_id=pending.request_id,
        )
        if resolved:
            raise ValueError(
                "question request was already answered; stale question replay is not allowed"
            )
        expected_questions = [
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
            for prompt in pending.prompts
        ]
        if request_event is None:
            if checkpoint is None:
                raise ValueError(
                    "persisted pending question has no matching question request event"
                )
            if checkpoint.get("pending_question_request_id") != pending.request_id:
                raise ValueError(
                    "persisted question resume checkpoint request id "
                    "does not match pending question"
                )
            if checkpoint.get("pending_question_tool_name") != pending.tool_name:
                raise ValueError(
                    "persisted pending question tool does not match the recorded question request"
                )
            if checkpoint.get("pending_question_prompts") != expected_questions:
                raise ValueError(
                    "persisted pending question no longer matches "
                    "the recorded question request payload"
                )
            return
        payload = request_event.payload
        if payload.get("tool") != pending.tool_name:
            raise ValueError(
                "persisted pending question tool does not match the recorded question request"
            )
        if payload.get("questions") != expected_questions:
            raise ValueError(
                "persisted pending question no longer matches the recorded question request payload"
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
        yield from self._resume_coordinator.resume_pending_approval_stream(
            session_id=session_id,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
            finalize_background_task=finalize_background_task,
        )

    def _resume_pending_approval_response(
        self,
        *,
        session_id: str,
        approval_request_id: str,
        approval_decision: PermissionResolution,
    ) -> tuple[tuple[EventEnvelope, ...], RuntimeResponse]:
        return self._resume_coordinator.resume_pending_approval_response(
            session_id=session_id,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
        )

    def _answer_pending_question_stream(
        self,
        *,
        session_id: str,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
        finalize_background_task: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        yield from self._resume_coordinator.answer_pending_question_stream(
            session_id=session_id,
            question_request_id=question_request_id,
            responses=responses,
            finalize_background_task=finalize_background_task,
        )

    def _answer_pending_question_response(
        self,
        *,
        session_id: str,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> tuple[tuple[EventEnvelope, ...], RuntimeResponse]:
        return self._resume_coordinator.answer_pending_question_response(
            session_id=session_id,
            question_request_id=question_request_id,
            responses=responses,
        )

    def _answer_pending_question_impl(
        self,
        *,
        stored: RuntimeResponse,
        pending: PendingQuestion,
        responses: tuple[QuestionResponse, ...],
        checkpoint: dict[str, object] | None,
    ) -> Iterator[RuntimeStreamChunk]:
        yield from self._resume_coordinator.answer_pending_question_impl(
            stored=stored,
            pending=pending,
            responses=responses,
            checkpoint=checkpoint,
        )

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
        return self._resume_coordinator.response_from_resumed_chunks(
            stored_response=stored_response,
            streamed_events=streamed_events,
            output=output,
            final_session=final_session,
        )

    def _resume_pending_approval_impl(
        self,
        *,
        stored: RuntimeResponse,
        pending: PendingApproval,
        approval_decision: PermissionResolution,
        checkpoint: dict[str, object] | None,
    ) -> Iterator[RuntimeStreamChunk]:
        yield from self._resume_coordinator.resume_pending_approval_impl(
            stored=stored,
            pending=pending,
            approval_decision=approval_decision,
            checkpoint=checkpoint,
        )

    def _approval_resume_state_from_checkpoint(
        self,
        *,
        checkpoint: dict[str, object] | None,
        pending: PendingApproval,
        stored_metadata: dict[str, object],
    ) -> _ApprovalResumeCheckpointState | None:
        state = self._resume_coordinator.approval_resume_state_from_checkpoint(
            checkpoint=checkpoint,
            pending=pending,
            stored_metadata=stored_metadata,
        )
        if state is None:
            return None
        return _ApprovalResumeCheckpointState(
            prompt=state.prompt,
            session_metadata=state.session_metadata,
            tool_results=state.tool_results,
        )

    def _question_resume_state_from_checkpoint(
        self,
        *,
        checkpoint: dict[str, object] | None,
        pending: PendingQuestion,
        stored_metadata: dict[str, object],
    ) -> _ApprovalResumeCheckpointState | None:
        state = self._resume_coordinator.question_resume_state_from_checkpoint(
            checkpoint=checkpoint,
            pending=pending,
            stored_metadata=stored_metadata,
        )
        if state is None:
            return None
        return _ApprovalResumeCheckpointState(
            prompt=state.prompt,
            session_metadata=state.session_metadata,
            tool_results=state.tool_results,
        )

    @staticmethod
    def _validated_resume_checkpoint_envelope(
        *, checkpoint: dict[str, object] | None, expected_kind: str
    ) -> _PersistedResumeCheckpointEnvelope | None:
        envelope = RuntimeResumeCoordinator.validated_resume_checkpoint_envelope(
            checkpoint=checkpoint,
            expected_kind=expected_kind,
        )
        if envelope is None:
            return None
        return _PersistedResumeCheckpointEnvelope(
            kind=envelope.kind,
            version=envelope.version,
            payload=envelope.payload,
        )

    def _load_resume_checkpoint(self, *, session_id: str) -> dict[str, object] | None:
        return self._resume_coordinator.load_resume_checkpoint(session_id=session_id)

    @staticmethod
    def _tool_results_from_checkpoint(raw_tool_results: list[object]) -> tuple[ToolResult, ...]:
        return RuntimeResumeCoordinator.tool_results_from_checkpoint(raw_tool_results)

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

        prompt, metadata = self._resolve_prompt_command_for_request(
            prompt=request.prompt,
            metadata=metadata,
        )

        return RuntimeRequest(
            prompt=prompt,
            session_id=session_id,
            parent_session_id=resolved_parent_session_id,
            metadata=metadata,
            allocate_session_id=request.allocate_session_id,
        )

    def _resolve_prompt_command_for_request(
        self,
        *,
        prompt: str,
        metadata: RuntimeRequestMetadataPayload,
    ) -> tuple[str, RuntimeRequestMetadataPayload]:
        if "command" in metadata or not is_prompt_command(prompt):
            return prompt, metadata
        try:
            resolution = resolve_prompt_command(
                prompt,
                load_command_registry(workspace=self._workspace),
            )
        except ValueError as exc:
            raise RuntimeRequestError(str(exc)) from exc
        if resolution is None:
            return prompt, metadata

        normalized = dict(cast(dict[str, object], metadata))
        normalized["command"] = {
            "name": resolution.invocation.name,
            "source": resolution.invocation.source,
            "arguments": list(resolution.invocation.arguments),
            "raw_arguments": resolution.invocation.raw_arguments,
            "original_prompt": resolution.invocation.original_prompt,
        }
        return resolution.invocation.rendered_prompt, cast(
            RuntimeRequestMetadataPayload, normalized
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
                "background_run" in normalized
                or "background_rate_limit_retry" in normalized
                or "background_task_id" in normalized
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
        # Referenced via extracted collaborators.
        if not events:
            return ""
        prompt = events[0].payload.get("prompt")
        if isinstance(prompt, str):
            return prompt
        return ""

    @staticmethod
    def _provider_attempt_from_metadata(metadata: dict[str, object]) -> int:
        # Referenced via extracted collaborators.
        raw_provider_attempt = metadata.get("provider_attempt", 0)
        return raw_provider_attempt if isinstance(raw_provider_attempt, int) else 0

    @staticmethod
    def _context_window_config_from_policy(
        policy: ContextWindowPolicy | None,
    ) -> RuntimeContextWindowConfig | None:
        if policy is None:
            return None
        return RuntimeContextWindowConfig(
            auto_compaction=policy.auto_compaction,
            max_tool_results=policy.max_tool_results,
            max_tool_result_tokens=policy.max_tool_result_tokens,
            max_context_ratio=policy.max_context_ratio,
            model_context_window_tokens=policy.model_context_window_tokens,
            reserved_output_tokens=policy.reserved_output_tokens,
            minimum_retained_tool_results=policy.minimum_retained_tool_results,
            recent_tool_result_count=policy.recent_tool_result_count,
            recent_tool_result_tokens=policy.recent_tool_result_tokens,
            default_tool_result_tokens=policy.default_tool_result_tokens,
            per_tool_result_tokens=dict(policy.per_tool_result_tokens),
            tokenizer_model=policy.tokenizer_model,
            continuity_preview_items=policy.continuity_preview_items,
            continuity_preview_chars=policy.continuity_preview_chars,
        )

    @staticmethod
    def _context_window_policy_from_config(
        config: RuntimeContextWindowConfig | None,
        *,
        resolved_provider: ResolvedProviderConfig | None,
        provider_attempt: int = 0,
    ) -> ContextWindowPolicy:
        if config is None:
            return ContextWindowPolicy()
        model_context_window_tokens = config.model_context_window_tokens
        if model_context_window_tokens is None and resolved_provider is not None:
            provider_target = resolved_provider.target_chain.target_at(provider_attempt)
            if provider_target is None:
                provider_target = resolved_provider.active_target
            provider = provider_target.selection.provider
            model = provider_target.selection.model
            if provider is not None and model is not None:
                metadata = infer_model_metadata(provider, model)
                if metadata is not None:
                    model_context_window_tokens = metadata.context_window
        return ContextWindowPolicy(
            auto_compaction=config.auto_compaction,
            max_tool_results=config.max_tool_results,
            max_tool_result_tokens=config.max_tool_result_tokens,
            max_context_ratio=config.max_context_ratio,
            model_context_window_tokens=model_context_window_tokens,
            reserved_output_tokens=config.reserved_output_tokens,
            minimum_retained_tool_results=config.minimum_retained_tool_results,
            recent_tool_result_count=config.recent_tool_result_count,
            recent_tool_result_tokens=config.recent_tool_result_tokens,
            default_tool_result_tokens=config.default_tool_result_tokens,
            per_tool_result_tokens=dict(config.per_tool_result_tokens),
            tokenizer_model=config.tokenizer_model,
            continuity_preview_items=config.continuity_preview_items,
            continuity_preview_chars=config.continuity_preview_chars,
        )

    def _prepare_provider_context_window(
        self,
        *,
        prompt: str,
        tool_results: tuple[ToolResult, ...],
        session_metadata: dict[str, object],
        policy: ContextWindowPolicy | None = None,
    ) -> RuntimeContextWindow:
        effective_config = self._effective_runtime_config_from_metadata(session_metadata)
        provider_attempt = self._provider_attempt_from_metadata(session_metadata)
        if policy is None:
            policy = self._context_window_policy_from_config(
                effective_config.context_window,
                resolved_provider=None,
                provider_attempt=provider_attempt,
            )
        policy = self._context_window_policy_for_provider_attempt(
            policy,
            resolved_provider=effective_config.resolved_provider,
            provider_attempt=provider_attempt,
        )
        return prepare_provider_context(
            prompt=prompt,
            tool_results=tool_results,
            session_metadata=session_metadata,
            policy=policy or self._default_context_window_policy,
        )

    def _assemble_provider_context(
        self,
        *,
        prompt: str,
        tool_results: tuple[ToolResult, ...],
        session_metadata: dict[str, object],
        skill_prompt_context: str = "",
        preserved_system_segments: tuple[str, ...] = (),
    ) -> RuntimeAssembledContext:
        effective_config = self._effective_runtime_config_from_metadata(session_metadata)
        provider_attempt = self._provider_attempt_from_metadata(session_metadata)
        policy = self._context_window_policy_from_config(
            effective_config.context_window,
            resolved_provider=None,
            provider_attempt=provider_attempt,
        )
        policy = self._context_window_policy_for_provider_attempt(
            policy,
            resolved_provider=effective_config.resolved_provider,
            provider_attempt=provider_attempt,
        )
        raw_loaded = session_metadata.get("loaded_skills", [])
        loaded_skills: tuple[dict[str, object], ...] = ()
        if isinstance(raw_loaded, list):
            typed: list[dict[str, object]] = []
            for item in cast(list[object], raw_loaded):
                if isinstance(item, dict):
                    entry: dict[str, object] = {}
                    for k, v in cast(dict[object, object], item).items():
                        if isinstance(k, str):
                            entry[k] = v
                    typed.append(entry)
            loaded_skills = tuple(typed)
        raw_agent_preset = session_metadata.get("agent_preset")
        agent_preset = (
            cast(dict[str, object], raw_agent_preset)
            if isinstance(raw_agent_preset, dict)
            else None
        )
        model_family = effective_config.resolved_provider.active_target.selection.provider
        agent_prompt_context = render_agent_prompt(agent_preset, model_family=model_family) or ""
        return assemble_provider_context(
            prompt=prompt,
            tool_results=tool_results,
            session_metadata=session_metadata,
            policy=policy or self._default_context_window_policy,
            agent_prompt_context=agent_prompt_context,
            skill_prompt_context=skill_prompt_context,
            preserved_system_segments=preserved_system_segments,
            loaded_skills=loaded_skills,
            preserved_continuity_state=self._continuity_state_from_session_metadata(
                session_metadata
            ),
        )

    @staticmethod
    def _continuity_state_from_session_metadata(
        session_metadata: dict[str, object],
    ) -> RuntimeContinuityState | None:
        # Referenced via extracted collaborators.
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
        original_tokens = continuity_payload.get("original_tool_result_tokens")
        retained_tokens = continuity_payload.get("retained_tool_result_tokens")
        dropped_tokens = continuity_payload.get("dropped_tool_result_tokens")
        token_budget = continuity_payload.get("token_budget")
        token_estimate_source = continuity_payload.get("token_estimate_source")
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

        def _optional_int(value: object) -> int | None:
            if value is None:
                return None
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            raise ValueError

        try:
            original_token_count = _optional_int(original_tokens)
            retained_token_count = _optional_int(retained_tokens)
            dropped_token_count = _optional_int(dropped_tokens)
            resolved_token_budget = _optional_int(token_budget)
        except ValueError:
            return None
        if token_estimate_source is not None and not isinstance(token_estimate_source, str):
            return None
        return RuntimeContinuityState(
            summary_text=summary_text,
            dropped_tool_result_count=dropped,
            retained_tool_result_count=retained,
            source=source,
            original_tool_result_tokens=original_token_count,
            retained_tool_result_tokens=retained_token_count,
            dropped_tool_result_tokens=dropped_token_count,
            token_budget=resolved_token_budget,
            token_estimate_source=token_estimate_source,
            version=version if version is not None else 1,
        )

    @staticmethod
    def _session_with_context_window_metadata(
        session: SessionState, context_window: RuntimeContextWindow
    ) -> SessionState:
        # Referenced via extracted run-loop collaborator.
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
        continuity_summary_payload = (
            {
                "anchor": context_window.summary_anchor,
                "source": context_window.summary_source,
            }
            if context_window.summary_anchor is not None
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
                    **(
                        {"continuity_summary": continuity_summary_payload}
                        if continuity_summary_payload is not None
                        else {}
                    ),
                },
            },
        )

    @staticmethod
    def _session_with_provider_usage_metadata(
        session: SessionState, usage: ProviderTokenUsage | None
    ) -> SessionState:
        if usage is None:
            return session
        usage_payload = usage.metadata_payload()
        raw_provider_usage = session.metadata.get("provider_usage")
        provider_usage = (
            dict(cast(dict[str, object], raw_provider_usage))
            if isinstance(raw_provider_usage, dict)
            else {}
        )
        raw_cumulative = provider_usage.get("cumulative")
        cumulative = (
            dict(cast(dict[str, object], raw_cumulative))
            if isinstance(raw_cumulative, dict)
            else {}
        )

        def _int_value(key: str) -> int:
            raw_value = cumulative.get(key, 0)
            if isinstance(raw_value, int) and not isinstance(raw_value, bool):
                return raw_value
            return 0

        cumulative_payload = {key: _int_value(key) + value for key, value in usage_payload.items()}
        raw_turn_count = provider_usage.get("turn_count", 0)
        turn_count = 0
        if isinstance(raw_turn_count, int) and not isinstance(raw_turn_count, bool):
            turn_count = raw_turn_count
        return SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata={
                **session.metadata,
                "provider_usage": {
                    "latest": usage_payload,
                    "cumulative": cumulative_payload,
                    "turn_count": turn_count + 1,
                },
            },
        )

    @staticmethod
    def _renumber_events(
        events: tuple[GraphEvent, ...], *, session_id: str, start_sequence: int
    ) -> tuple[EventEnvelope, ...]:
        # Referenced via extracted run-loop collaborator.
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

    def _applied_skill_contexts(
        self,
        skill_registry: SkillRegistry,
        metadata: dict[str, object] | None = None,
        agent: RuntimeAgentConfig | None = None,
    ) -> tuple[SkillRuntimeContext, ...]:
        _ = agent
        request_force_load_skill_names = self._request_skill_names_from_metadata(
            metadata,
            key="force_load_skills",
        )

        force_load_skill_names = request_force_load_skill_names
        if force_load_skill_names is None:
            return ()
        deduped_force_load_skill_names: list[str] = []
        for skill_name in force_load_skill_names:
            if skill_name not in deduped_force_load_skill_names:
                deduped_force_load_skill_names.append(skill_name)
        return build_runtime_contexts(
            skill_registry,
            skill_names=tuple(deduped_force_load_skill_names),
        )

    @staticmethod
    def _request_skill_names_from_metadata(
        metadata: dict[str, object] | None,
        *,
        key: str,
    ) -> tuple[str, ...] | None:
        if metadata is None or key not in metadata:
            return None
        raw_skills = metadata[key]
        if not isinstance(raw_skills, list):
            raise ValueError(f"request metadata '{key}' must be a list of skill names")
        parsed_names: list[str] = []
        for index, raw_name in enumerate(cast(list[object], raw_skills)):
            if not isinstance(raw_name, str) or not raw_name:
                raise ValueError(f"request metadata '{key}[{index}]' must be a non-empty string")
            parsed_names.append(raw_name)
        return tuple(parsed_names)

    def _build_skill_snapshot(
        self,
        skill_registry: SkillRegistry,
        *,
        metadata: dict[str, object] | None,
        agent: RuntimeAgentConfig | None,
        source: Literal["run", "resume", "replay"],
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

        selected_skill_names = self._selected_skill_names_for_agent(
            agent,
            request_skill_names=self._request_skill_names_from_metadata(metadata, key="skills"),
            persisted_selected_skill_names=(
                self._persisted_selected_skill_names(metadata) if metadata is not None else None
            ),
        )
        force_load_skill_names = self._request_skill_names_from_metadata(
            metadata,
            key="force_load_skills",
        )
        contexts = self._applied_skill_contexts(skill_registry, metadata, agent)
        effective_selected_skill_names = self._effective_selected_skill_names(
            selected_skill_names,
            force_load_skill_names,
        )
        return build_skill_execution_snapshot(
            contexts,
            source=source,
            selected_skill_names=effective_selected_skill_names,
            binding_snapshot=binding_snapshot,
        )

    @staticmethod
    def _effective_selected_skill_names(
        selected_skill_names: tuple[str, ...] | None,
        force_load_skill_names: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if force_load_skill_names is None:
            return selected_skill_names

        merged_names: list[str] = []
        for skill_name in (*(selected_skill_names or ()), *force_load_skill_names):
            if skill_name not in merged_names:
                merged_names.append(skill_name)
        return tuple(merged_names)

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
        # Referenced via extracted resume collaborator.
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
            "applied_skills": [payload["name"] for payload in snapshot.applied_skill_payloads],
            "skill_snapshot": snapshot_payload(snapshot),
        }

    @staticmethod
    def _force_loaded_skill_payloads(
        snapshot: SkillExecutionSnapshot,
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "name": payload.get("name"),
                "source": "force_load",
                "source_path": payload.get("source_path"),
            }
            for payload in snapshot.applied_skill_payloads
        )

    def _skill_snapshot_from_metadata(
        self,
        metadata: dict[str, object],
    ) -> SkillExecutionSnapshot | None:
        raw_snapshot = metadata.get("skill_snapshot")
        if isinstance(raw_snapshot, dict):
            return snapshot_from_payload(cast(dict[str, object], raw_snapshot))
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

    @staticmethod
    def _catalog_skill_context(
        skill_registry: SkillRegistry,
        *,
        available_skill_names: tuple[str, ...],
        selected_skill_names: tuple[str, ...],
    ) -> str:
        names = selected_skill_names or available_skill_names
        if not names:
            return ""
        lines = [
            "Runtime skills catalog (recommended/visible).",
            "Load full instructions with tool: skill(name=...).",
            "",
            "<available_skills>",
        ]
        for skill_name in names:
            skill = skill_registry.skills.get(skill_name)
            if skill is None:
                continue
            lines.extend(
                (
                    "  <skill>",
                    f"    <name>{skill.name}</name>",
                    f"    <description>{skill.description}</description>",
                    f"    <location>{skill.entry_path.as_uri()}</location>",
                    "  </skill>",
                )
            )
        lines.append("</available_skills>")
        return "\n".join(lines)

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
        }
        serialized_context_window = serialize_runtime_context_window_config(
            effective_config.context_window
        )
        if serialized_context_window is not None:
            runtime_config_metadata["context_window"] = serialized_context_window
        if effective_config.model is not None:
            runtime_config_metadata["model"] = effective_config.model
        serialized_agent = serialize_runtime_agent_config(effective_config.agent)
        if serialized_agent is not None:
            runtime_config_metadata["agent"] = serialized_agent
        serialized_agents = serialize_runtime_agents_config(self._config.agents)
        if serialized_agents is not None:
            runtime_config_metadata["agents"] = serialized_agents
        serialized_categories = serialize_runtime_categories_config(self._config.categories)
        if serialized_categories is not None:
            runtime_config_metadata["categories"] = serialized_categories
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
        agent = parse_runtime_agent_payload(
            raw_agent,
            source="request metadata 'agent'",
            hooks=self._config.hooks,
        )
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
            prompt_ref=(
                agent.prompt_ref
                if agent.prompt_ref is not None
                else resolved.agent.prompt_ref
                if resolved.agent is not None
                else None
            ),
            prompt_source=(
                agent.prompt_source
                if agent.prompt_source is not None
                else resolved.agent.prompt_source
                if resolved.agent is not None
                else None
            ),
            hook_refs=(
                agent.hook_refs
                if agent.hook_refs
                else resolved.agent.hook_refs
                if resolved.agent is not None
                else ()
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
            resolved_provider=resolved_provider,
            agent=merged_agent,
            context_window=resolved.context_window,
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
            delegated_model = self._delegated_model_for_route(
                category=resolved_route.requested.category,
                selected_preset=resolved_route.selected_preset,
                request_agent=None,
            )
            delegated_provider_fallback = self._delegated_provider_fallback_for_route(
                category=resolved_route.requested.category,
                selected_preset=resolved_route.selected_preset,
                request_agent=None,
                model=delegated_model,
            )
            agent = parse_runtime_agent_payload(
                {
                    "preset": resolved_route.selected_preset,
                    "execution_engine": resolved_route.execution_engine,
                    **({"model": delegated_model} if delegated_model is not None else {}),
                    **(
                        {
                            "provider_fallback": serialize_provider_fallback_config(
                                delegated_provider_fallback
                            )
                        }
                        if delegated_provider_fallback is not None
                        else {}
                    ),
                },
                source="delegation.selected_preset",
                hooks=self._config.hooks,
            )
            assert agent is not None
        else:
            agent = parse_runtime_agent_payload(
                raw_agent,
                source="request metadata 'agent'",
                hooks=self._config.hooks,
            )
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
            if agent.model is None:
                delegated_model = self._delegated_model_for_route(
                    category=resolved_route.requested.category,
                    selected_preset=resolved_route.selected_preset,
                    request_agent=agent,
                )
                if delegated_model is not None:
                    agent = replace(agent, model=delegated_model)
            if agent.provider_fallback is None:
                delegated_provider_fallback = self._delegated_provider_fallback_for_route(
                    category=resolved_route.requested.category,
                    selected_preset=resolved_route.selected_preset,
                    request_agent=agent,
                    model=agent.model,
                )
                if delegated_provider_fallback is not None:
                    agent = replace(agent, provider_fallback=delegated_provider_fallback)

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

    def _delegated_model_for_route(
        self,
        *,
        category: str | None,
        selected_preset: str,
        request_agent: RuntimeAgentConfig | None,
    ) -> str | None:
        if request_agent is not None and request_agent.model is not None:
            return request_agent.model
        return self._delegated_model_for_route_from_configs(
            category=category,
            selected_preset=selected_preset,
            request_agent=request_agent,
            categories=self._config.categories or {},
            agents=self._config.agents or {},
            base_model=self._config.model,
        )

    def _delegated_model_for_route_from_configs(
        self,
        *,
        category: str | None,
        selected_preset: str,
        request_agent: RuntimeAgentConfig | None,
        categories: Mapping[str, RuntimeCategoryConfig],
        agents: Mapping[str, RuntimeAgentConfig],
        base_model: str | None,
    ) -> str | None:
        if request_agent is not None and request_agent.model is not None:
            return request_agent.model
        category_config = categories.get(category) if category is not None else None
        if category_config is not None and category_config.model is not None:
            return category_config.model
        preset_agent = agents.get(selected_preset)
        if preset_agent is not None and preset_agent.model is not None:
            return preset_agent.model
        return base_model

    def _delegated_provider_fallback_for_route(
        self,
        *,
        category: str | None,
        selected_preset: str,
        request_agent: RuntimeAgentConfig | None,
        model: str | None,
    ) -> RuntimeProviderFallbackConfig | None:
        if request_agent is not None and request_agent.provider_fallback is not None:
            return request_agent.provider_fallback
        preset_agent = self._preset_agent_config(selected_preset)
        provider_fallback = self._provider_fallback_for_agent_selection(
            model=model,
            preset_agent=preset_agent,
            base_provider_fallback=self._config.provider_fallback,
        )
        if category is not None and provider_fallback is not None and model is not None:
            return self._provider_fallback_with_preferred_model(provider_fallback, model)
        return provider_fallback

    def _provider_fallback_for_agent_selection(
        self,
        *,
        model: str | None,
        preset_agent: RuntimeAgentConfig | None,
        base_provider_fallback: RuntimeProviderFallbackConfig | None,
    ) -> RuntimeProviderFallbackConfig | None:
        if preset_agent is not None and preset_agent.provider_fallback is not None:
            if model is None or model == preset_agent.provider_fallback.preferred_model:
                return preset_agent.provider_fallback
            return self._provider_fallback_with_preferred_model(
                preset_agent.provider_fallback,
                model,
            )
        if base_provider_fallback is None:
            return None
        if model is None or model == base_provider_fallback.preferred_model:
            return base_provider_fallback
        return self._provider_fallback_with_preferred_model(base_provider_fallback, model)

    @staticmethod
    def _provider_fallback_with_preferred_model(
        provider_fallback: RuntimeProviderFallbackConfig,
        preferred_model: str,
    ) -> RuntimeProviderFallbackConfig:
        return RuntimeProviderFallbackConfig(
            preferred_model=preferred_model,
            fallback_models=tuple(
                fallback_model
                for fallback_model in provider_fallback.fallback_models
                if fallback_model != preferred_model
            ),
        )

    def _category_config(self, category: str | None) -> RuntimeCategoryConfig | None:
        if category is None or self._config.categories is None:
            return None
        return self._config.categories.get(category)

    def _preset_agent_config(self, preset: str) -> RuntimeAgentConfig | None:
        if self._config.agents is None:
            return None
        return self._config.agents.get(preset)

    def _category_model_diagnostics(
        self,
        *,
        request_metadata: dict[str, object],
        effective_config: EffectiveRuntimeConfig,
    ) -> tuple[dict[str, object], ...]:
        raw_delegation = request_metadata.get("delegation")
        if not isinstance(raw_delegation, dict):
            return ()
        delegation = cast(dict[str, object], raw_delegation)
        if delegation.get("category") != "ultrabrain":
            return ()
        active_target = effective_config.resolved_provider.active_target.selection
        provider_name = active_target.provider
        model_name = active_target.model
        if provider_name is None or model_name is None:
            return ()
        metadata = self._metadata_for_provider_model(provider_name, model_name)
        if metadata is None or metadata.supports_reasoning is not False:
            return ()
        return (
            {
                "severity": "warning",
                "category": "model_capability_mismatch",
                "capability": "reasoning",
                "requested_category": "ultrabrain",
                "provider": provider_name,
                "model": model_name,
                "message": (
                    "task category 'ultrabrain' resolved to a model whose provider metadata "
                    "does not support reasoning"
                ),
            },
        )

    def _runtime_state_metadata(self, *, run_id: str | None = None) -> dict[str, object]:
        acp_state = self._acp_adapter.current_state()
        return {
            **({"run_id": run_id} if run_id is not None else {}),
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
            },
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
            RUNTIME_MCP_SERVER_ACQUIRED,
            RUNTIME_MCP_SERVER_IDLE_CLEANED,
            RUNTIME_MCP_SERVER_RELEASED,
            RUNTIME_MCP_SERVER_REUSED,
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
        # Referenced via extracted resume collaborator.
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
        # Referenced via extracted background-task collaborator.
        return VoidCodeRuntime._waiting_request_id_from_response(response, request_kind="approval")

    @staticmethod
    def _waiting_request_id_from_response(
        response: RuntimeResponse,
        *,
        request_kind: Literal["approval", "question"],
    ) -> str | None:
        if response.session.status != "waiting":
            return None
        target_event_type = (
            RUNTIME_APPROVAL_REQUESTED if request_kind == "approval" else RUNTIME_QUESTION_REQUESTED
        )
        for event in reversed(response.events):
            if event.event_type == target_event_type:
                request_id = event.payload.get("request_id")
                return str(request_id) if request_id is not None else None
        return None

    def _load_background_task_child_response(
        self,
        *,
        task: BackgroundTaskState,
    ) -> RuntimeResponse | None:
        # Compatibility wrapper for tests/callers.
        return self._background_task_supervisor.load_background_task_child_response(task=task)

    def _load_background_task_child_result(
        self,
        *,
        task: BackgroundTaskState,
    ) -> RuntimeSessionResult | None:
        # Compatibility wrapper for tests/callers.
        return self._background_task_supervisor.load_background_task_child_result(task=task)

    def _background_task_result(self, *, task: BackgroundTaskState) -> BackgroundTaskResult:
        # Compatibility wrapper for tests/callers.
        return self._background_task_supervisor.background_task_result(task=task)

    def _emit_background_task_parent_terminal_event(self, *, task: BackgroundTaskState) -> None:
        # Compatibility wrapper for tests/callers.
        self._background_task_supervisor.emit_background_task_parent_terminal_event(task=task)

    def _backfill_parent_background_task_event(self, *, task: BackgroundTaskState) -> None:
        # Compatibility wrapper for tests/callers.
        self._background_task_supervisor.backfill_parent_background_task_event(task=task)

    def _reconcile_parent_background_task_events_for_session(
        self,
        *,
        parent_session_id: str,
    ) -> None:
        # Compatibility wrapper for tests/callers.
        self._background_task_supervisor.reconcile_parent_background_task_events_for_session(
            parent_session_id=parent_session_id
        )

    def _emit_background_task_waiting_approval(
        self,
        *,
        task: BackgroundTaskState,
        child_response: RuntimeResponse,
    ) -> None:
        # Compatibility wrapper for tests/callers.
        self._background_task_supervisor.emit_background_task_waiting_approval(
            task=task,
            child_response=child_response,
        )

    def _finalize_background_task_from_session_response(
        self,
        *,
        session_response: RuntimeResponse,
    ) -> None:
        # Compatibility wrapper for tests/callers.
        self._background_task_supervisor.finalize_background_task_from_session_response(
            session_response=session_response
        )

    def _run_background_task_lifecycle_hook(self, task: BackgroundTaskState) -> None:
        # Compatibility wrapper for tests/callers.
        self._background_task_supervisor.run_background_task_lifecycle_hook(task)

    def _run_background_task_lifecycle_surface(
        self,
        *,
        task: BackgroundTaskState,
        surface: RuntimeHookSurface,
        session_id: str,
        extra_payload: dict[str, object] | None = None,
    ) -> None:
        # Compatibility wrapper for tests/callers.
        self._background_task_supervisor.run_background_task_lifecycle_surface(
            task=task,
            surface=surface,
            session_id=session_id,
            extra_payload=extra_payload,
        )

    def _reconcile_background_tasks_if_needed(self) -> None:
        # Compatibility wrapper for tests/callers.
        self._background_task_supervisor.reconcile_background_tasks_if_needed()

    def _run_background_task_worker(self, task_id: str) -> None:
        # Compatibility wrapper for tests/callers.
        self._background_task_supervisor.run_background_task_worker(task_id)

    def _effective_runtime_config_from_metadata(
        self, metadata: dict[str, object] | None
    ) -> EffectiveRuntimeConfig:
        approval_mode: PermissionDecision = self._config.approval_mode
        model = self._config.model
        execution_engine = self._config.execution_engine
        max_steps = self._config.max_steps
        provider_fallback = self._config.provider_fallback
        agent = self._config.agent
        if agent is None and execution_engine == "provider":
            agent = RuntimeAgentConfig(preset="leader")
        context_window = self._context_window_config_override or self._config.context_window
        allow_persisted_subagent_presets = False
        if metadata is not None:
            allow_persisted_subagent_presets = (
                runtime_subagent_route_from_metadata(metadata) is not None
            )
        if agent is not None:
            agent = parse_runtime_agent_payload(
                serialize_runtime_agent_config(agent),
                source="runtime config agent",
                hooks=self._config.hooks,
            )
            assert agent is not None
            self._validate_runtime_agent_for_execution(
                agent,
                source="runtime config agent",
            )
        elif execution_engine == "provider":
            agent = parse_runtime_agent_payload(
                serialize_runtime_agent_config(RuntimeAgentConfig(preset="leader")),
                source="runtime config agent",
                hooks=self._config.hooks,
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
                resolved_provider=resolved_provider,
                agent=agent,
                context_window=context_window,
            )

        persisted_runtime_config = metadata.get("runtime_config")
        if not isinstance(persisted_runtime_config, dict):
            raise ValueError("persisted session metadata must include runtime_config")

        runtime_config = cast(dict[str, object], persisted_runtime_config)
        unknown_runtime_config_keys = sorted(
            key for key in runtime_config if key not in _PERSISTED_RUNTIME_CONFIG_KEYS
        )
        if unknown_runtime_config_keys:
            raise ValueError(
                "persisted runtime_config field "
                f"'{unknown_runtime_config_keys[0]}' is not supported"
            )
        persisted_approval_mode = runtime_config.get("approval_mode")
        if persisted_approval_mode in ("allow", "deny", "ask"):
            approval_mode = persisted_approval_mode
        persisted_model = runtime_config.get("model")
        if persisted_model is None or isinstance(persisted_model, str):
            model = persisted_model
        if "max_steps" in runtime_config:
            persisted_max_steps = runtime_config.get("max_steps")
            if persisted_max_steps is None:
                max_steps = None
            elif isinstance(persisted_max_steps, int) and not isinstance(persisted_max_steps, bool):
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
        if "agent" in runtime_config:
            agent = parse_runtime_agent_payload(
                runtime_config.get("agent"),
                source="persisted runtime_config.agent",
                hooks=self._config.hooks,
            )
            if agent is not None:
                self._validate_runtime_agent_for_execution(
                    agent,
                    source="persisted runtime_config.agent",
                    allow_subagent_presets=allow_persisted_subagent_presets,
                )
        else:
            agent = None
        if "context_window" in runtime_config:
            try:
                context_window = parse_runtime_context_window_payload(
                    runtime_config.get("context_window"),
                    source="persisted runtime_config.context_window",
                )
            except ValueError as exc:
                raise ValueError(
                    format_invalid_provider_config_error(
                        "persisted runtime_config.context_window",
                        str(exc),
                    )
                ) from exc
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
            resolved_provider=resolved_provider,
            agent=agent,
            context_window=context_window,
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
            and effective_config.context_window == self._initial_effective_config.context_window
        ):
            if self._graph is not None:
                return self._graph
            self._graph = self._build_graph_for_engine_from_config(effective_config)
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
        return self._load_stored_response(session_id=session_id)

    def _load_stored_response(self, *, session_id: str) -> RuntimeResponse:
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
        _ = self._release_mcp_session(session_id)

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
    max_steps: int | None
    tool_timeout_seconds: int | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None
    resolved_provider: ResolvedProviderConfig = field(default_factory=ResolvedProviderConfig)
    agent: RuntimeAgentConfig | None = None
    context_window: RuntimeContextWindowConfig | None = None


@dataclass(frozen=True, slots=True)
class _ApprovalResumeCheckpointState:
    prompt: str
    session_metadata: dict[str, object]
    tool_results: tuple[ToolResult, ...]


@dataclass(frozen=True, slots=True)
class _PersistedResumeCheckpointEnvelope:
    kind: str
    version: int
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class _PermissionOutcome:
    chunks: tuple[RuntimeStreamChunk, ...]
    last_sequence: int
    pending_approval: PendingApproval | None = None
    denied: bool = False


@dataclass(frozen=True, slots=True)
class _RuntimeHookOutcome:
    chunks: tuple[RuntimeStreamChunk, ...]
    last_sequence: int
    failed_error: str | None = None
