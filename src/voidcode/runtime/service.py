from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast, final
from uuid import uuid4

from ..acp import AcpDelegatedExecution, AcpEventEnvelope, AcpRequestEnvelope, AcpResponseEnvelope
from ..agent import AgentManifestRegistry, get_builtin_agent_manifest, load_agent_manifest_registry
from ..agent.prompts import render_agent_prompt
from ..command import (
    COMMAND_RESOLVED,
    is_prompt_command,
    load_command_registry,
    resolve_prompt_command,
)
from ..command.models import CommandDefinition
from ..graph.contracts import GraphEvent, GraphRunRequest, RuntimeGraph
from ..hook.config import RuntimeHookSurface
from ..hook.executor import (
    HookExecutionOutcome,
    HookExecutionPolicy,
    HookExecutionRequest,
    LifecycleHookExecutionRequest,
    run_lifecycle_hooks,
    run_tool_hooks,
)
from ..hook.presets import (
    ResolvedHookPresetSnapshot,
    hook_preset_snapshot_from_payload,
    resolve_hook_preset_refs,
)
from ..mcp.redaction import redact_mcp_command
from ..provider.auth import (
    ProviderAuthResolver,
)
from ..provider.config import (
    DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG,
    ProviderTransientRetryConfig,
)
from ..provider.errors import guidance_for_provider_error_kind
from ..provider.model_catalog import ToolFeedbackMode
from ..provider.models import (
    ResolvedProviderChain,
    ResolvedProviderConfig,
    ResolvedProviderModel,
)
from ..provider.protocol import (
    ProviderAbortSignal,
    ProviderErrorKind,
    ProviderTokenUsage,
)
from ..provider.reasoning_effort import provider_supports_reasoning_effort
from ..provider.registry import ModelProviderRegistry
from ..provider.resolution import resolve_provider_config
from ..provider.snapshot import (
    parse_resolved_provider_snapshot,
    resolved_provider_snapshot,
)
from ..skills import SkillRegistry, skill_registry_with_builtins
from ..tools.background_cancel import BackgroundCancelTool
from ..tools.background_output import BackgroundOutputTool
from ..tools.background_process_logs import BackgroundProcessLogsTool
from ..tools.background_process_send import BackgroundProcessSendTool
from ..tools.background_process_start import BackgroundProcessManager, BackgroundProcessStartTool
from ..tools.background_process_stop import BackgroundProcessStopTool
from ..tools.contracts import (
    Tool,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from ..tools.output import (
    read_tool_output_artifact,
    search_tool_output_artifact,
)
from ..tools.output import (
    resolve_tool_output_artifact as resolve_tool_output_artifact_metadata,
)
from ..tools.question import QuestionTool
from ..tools.runtime_context import current_runtime_tool_context
from ..tools.skill import SkillTool
from ..tools.task import TaskTool
from .acp import AcpAdapter, AcpAdapterState, build_acp_adapter
from .active_session import (
    ACTIVE_SESSION_REGISTRY,
    ActiveRunInterruptResult,
    ActiveSessionRegistry,
)
from .active_session import (
    _ActiveRunAbortSignal as _ActiveRunAbortSignal,
)
from .active_session import (
    _ActiveRunHandle as _ActiveRunHandle,
)
from .active_session import (
    _ActiveSessionKey as _ActiveSessionKey,
)
from .agent_capability import (
    AGENT_CAPABILITY_SNAPSHOT_VERSION,
    agent_capability_agent_snapshot,
    agent_capability_delegation_snapshot,
    agent_capability_prompt_snapshot,
    agent_capability_tool_snapshot,
    agent_mcp_binding_payload,
    validate_agent_capability_snapshot,
)
from .background_tasks import RuntimeBackgroundTaskSupervisor
from .bundle import (
    SessionBundle,
    SessionBundleImportResult,
    SessionBundleOptions,
    apply_session_bundle,
    build_session_bundle,
    read_session_bundle,
    write_session_bundle,
)
from .command_effects import apply_runtime_command_effects, session_with_command_artifacts
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
    parse_runtime_agent_payload,
    parse_runtime_agents_payload,
    parse_runtime_categories_payload,
    parse_runtime_tools_payload,
    save_global_web_settings,
    serialize_runtime_agent_config,
    serialize_runtime_agents_config,
    serialize_runtime_categories_config,
)
from .config_materializer import (
    EffectiveRuntimeConfig,
    apply_request_runtime_config_overrides,
    parse_persisted_runtime_config,
    serialize_runtime_config_core,
)
from .context_transforms import (
    RuntimeContextTransformRegistry,
    build_provider_context_transform_result,
    default_runtime_context_transform_registry,
    validate_runtime_context_transform_refs,
)
from .context_window import (
    ContextProjection,
    ContextWindowPolicy,
    RuntimeAssembledContext,
    RuntimeContextSegment,
    RuntimeContextWindow,
    assemble_provider_context,
    continuity_state_from_metadata_payload,
    prepare_provider_context,
)
from .context_window_policy import (
    context_window_config_from_policy,
    context_window_policy_from_config,
)
from .contracts import (
    AgentSummary,
    BackgroundTaskResult,
    CapabilityStatusSnapshot,
    CommandSummary,
    GitStatusSnapshot,
    ProviderInspectResult,
    ProviderModelMetadata,
    ProviderModelsResult,
    ProviderReadinessResult,
    ProviderSummary,
    ProviderValidationResult,
    ReviewFileDiff,
    RuntimeBackgroundTaskStatusSnapshot,
    RuntimeHookPresetSnapshot,
    RuntimeMemoryStatusSnapshot,
    RuntimeNotification,
    RuntimeProviderContextPolicyDecision,
    RuntimeProviderContextSnapshot,
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
    RuntimeSessionRevertMarker,
    RuntimeStatusSnapshot,
    RuntimeStreamChunk,
    SkillSummary,
    UnknownSessionError,
    WorkspaceReviewSnapshot,
    runtime_mode_from_metadata,
    runtime_read_only_from_metadata,
    runtime_subagent_route_from_metadata,
    validate_runtime_request_metadata,
    validate_session_id,
    validate_session_reference_id,
)
from .delegation_routing import (
    delegated_model_for_route_from_configs,
    provider_fallback_for_agent_selection,
    provider_fallback_with_preferred_model,
)
from .edit_schema_policy import EditSchema, EditSchemaResolver, select_edit_schema
from .effectiveness import ToolEffectivenessReport
from .event_envelopes import (
    ReasoningCaptureState as _ReasoningCaptureState,
)
from .event_envelopes import (
    envelopes_for_acp_events,
    envelopes_for_lsp_events,
    envelopes_for_mcp_events,
    renumber_events,
    resequence_event,
)
from .events import (
    RUNTIME_ACP_DELEGATED_LIFECYCLE,
    RUNTIME_CATEGORY_MODEL_DIAGNOSTIC,
    RUNTIME_HOOK_PRESETS_LOADED,
    RUNTIME_MEMORY_ADDED,
    RUNTIME_MEMORY_DELETED,
    RUNTIME_MEMORY_SEARCHED,
    RUNTIME_MEMORY_STATUS_CHECKED,
    RUNTIME_QUESTION_ANSWERED,
    RUNTIME_REASONING_DIAGNOSTIC,
    RUNTIME_SKILLS_APPLIED,
    RUNTIME_SKILLS_LOADED,
    EventEnvelope,
    EventSource,
    runtime_policy_observability_payload,
)
from .execution_seams import (
    cache_key_for_effective_config,
    fallback_graph_for_provider_error,
    provider_model_required_message,
    resolve_runtime_session_routing,
    select_graph_for_effective_config,
)
from .hook_preset_metadata import (
    debug_hook_preset_snapshot,
    hook_preset_event_payload_from_session_metadata,
    hook_preset_refs_for_agent,
    hook_preset_refs_for_mode_and_agent,
    resolved_hook_preset_snapshot_from_session_metadata,
)
from .interaction_queue import drain_runtime_messages, enqueue_runtime_message
from .lsp import LspManager, LspManagerState, LspRequest, LspRequestResult, build_lsp_manager
from .mcp import McpManager, build_mcp_manager
from .memory import MemoryConfig, MemoryKind, MemoryRecord, MemorySearchResult, build_memory_manager
from .paths import provider_catalog_cache_path
from .permission import (
    DelegationGovernance,
    PendingApproval,
    PermissionDecision,
    PermissionPolicy,
    PermissionResolution,
    resolve_permission,
)
from .permission_context import RuntimePermissionContextResolver
from .permission_engine import PermissionEngine
from .permission_path_helpers import extract_paths_from_patch
from .permission_policy import (
    approval_request_id_from_waiting_response,
    pending_approval_from_response,
    pending_question_from_response,
    permission_policy_for_session,
    request_event_and_resolution_state,
    waiting_request_id_from_response,
)
from .policy import (
    materialize_runtime_policy_snapshot,
)
from .provider_catalog_cache import RuntimeProviderCatalogCache
from .provider_catalog_query import RuntimeProviderCatalogQuery
from .provider_context import inspect_provider_context
from .provider_execution_metadata import (
    provider_attempt_from_metadata,
    provider_retry_attempt_from_metadata,
    run_id_from_session_metadata,
    session_with_provider_usage_metadata,
)
from .provider_inspection import (
    ProviderReadinessFacts,
    ProviderSummaryProjector,
    ProviderValidationFacts,
    RuntimeProviderAuthInspector,
    RuntimeProviderReadinessProjector,
    RuntimeProviderValidationProjector,
)
from .provider_metadata import (
    contract_metadata_from_payload,
    optional_bool,
    optional_positive_float,
    optional_positive_int,
    optional_string,
    optional_string_tuple,
    tool_feedback_mode,
)
from .provider_protocol import ProviderExecutionError
from .question import PendingQuestion, QuestionResponse
from .resume import RuntimeResumeCoordinator
from .review import WorkspaceReviewService
from .run_loop import RuntimeRunLoopCoordinator
from .runtime_debug import (
    artifact_debug_metadata,
    current_debug_status,
    debug_event,
    debug_failure,
    debug_session_state_inconsistency,
    last_tool_summary,
    operator_guidance,
    payload_with_artifact_status,
    prompt_and_tool_results_from_debug_events,
    prompt_from_events,
    provider_visible_tool_result_data,
)
from .session import (
    SessionRef,
    SessionState,
    SessionStatus,
    StoredSessionSummary,
    is_session_status_terminal,
    session_metadata_for_replay,
)
from .session_metadata_helpers import (
    plan_state_from_metadata,
    session_with_context_window_payload_metadata,
    session_with_todo_state,
)
from .skill_metadata import (
    available_runtime_contexts,
    catalog_skill_context,
    effective_selected_skill_names,
    force_loaded_skill_payloads,
    fresh_request_metadata,
    loaded_skill_names,
    persisted_selected_skill_names,
    request_skill_names_from_metadata,
    selected_skill_names_for_agent,
    skill_binding_snapshot_from_agent_capability_snapshot,
    skill_snapshot_from_metadata,
    snapshot_to_session_metadata,
)
from .skills import (
    SkillExecutionSnapshot,
    SkillRuntimeContext,
    build_runtime_contexts,
    build_skill_execution_snapshot,
)
from .storage import SessionEventAppender, SessionSealedError, SessionStore, SqliteSessionStore
from .task import (
    BackgroundTaskState,
    ContinuationLoopRef,
    ContinuationLoopState,
    ContinuationLoopStatus,
    ContinuationLoopStrategy,
    StoredBackgroundTaskSummary,
    StoredContinuationLoopSummary,
    supported_subagent_categories,
    validate_background_task_id,
    validate_continuation_loop_id,
)
from .tool_execution import RuntimeToolExecutor
from .tool_materializer import RuntimeToolMaterialization, RuntimeToolMaterializer
from .tool_provider import (
    LocalCustomToolProvider,
)
from .tool_registry import (
    ToolPolicyDecision,
    ToolRegistry,
    agent_required_tool_patterns,
)
from .tool_replay import ToolExecutionIntent, recovery_action
from .tool_scope import RuntimeToolScopeResolver
from .workflow import (
    WorkflowModeResolution,
    get_builtin_workflow_mode,
    resolve_workflow_mode,
)
from .workflow_snapshot import (
    workflow_snapshot_from_metadata,
)

if TYPE_CHECKING:
    from .execution_seams import RuntimeGraphSelection, RuntimeSessionRouting

logger = logging.getLogger(__name__)

_ACTIVE_SESSION_TYPES = (
    _ActiveRunAbortSignal,
    _ActiveRunHandle,
    _ActiveSessionKey,
    ActiveSessionRegistry,
)

_EXECUTABLE_AGENT_PRESETS = frozenset({"leader"})
_EXECUTABLE_SUBAGENT_PRESETS = frozenset({"advisor", "explore", "researcher", "worker", "product"})


def _agent_effective_execution_engine(
    base_engine: ExecutionEngineName,
    agent: RuntimeAgentConfig | None,
) -> ExecutionEngineName:
    if base_engine == "deterministic":
        return "deterministic"
    if agent is not None and agent.execution_engine is not None:
        return agent.execution_engine
    return base_engine


_ACP_CONNECTIVITY_ERRORS = frozenset(
    {
        "ACP adapter is not connected",
        "ACP transport is not connected",
    }
)
_SKILL_BINDING_SCOPE_KEYS = (
    "approval_mode",
    "execution_engine",
    "max_steps",
    "tool_timeout_seconds",
    "reasoning_effort",
    "model",
    "fallback_models",
    "providers",
    "resolved_provider",
    "agent",
    "workflow",
    "lsp",
    "mcp",
)


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


def _render_workspace_memory_context(
    memories: tuple[MemoryRecord, ...],
    *,
    max_chars: int,
) -> str:
    if not memories:
        return ""
    header = "Workspace Memory:\nMemories may be stale; prefer current repository files when conflicts exist."
    if len(header) > max_chars:
        return header[:max_chars]
    lines = [header]
    rendered = header
    for memory in memories:
        tags = f" tags={','.join(memory.tags)}" if memory.tags else ""
        candidate_line = f"- {memory.id} [{memory.kind}]{tags} {memory.content.strip()}"
        candidate = "\n".join((*lines, candidate_line)).strip()
        if len(candidate) > max_chars:
            continue
        lines.append(candidate_line)
        rendered = candidate
    return rendered


@final
class _RuntimeToolCatalogFacade:
    """Adapter exposing the runtime's live registry to tools via the context.

    Tools read on-demand documentation (``voidcode://tool/<name>``) through
    this facade; it always resolves against the current materialization so MCP
    refreshes are reflected immediately.
    """

    def __init__(self, runtime: VoidCodeRuntime) -> None:
        self._runtime = runtime

    def lookup(self, tool_name: str) -> ToolDefinition | None:
        return self._runtime._tool_catalog_lookup(tool_name)


@final
class _RuntimeArtifactReadFacade:
    """Adapter exposing runtime-owned, session-guarded artifact reads to tools.

    ``voidcode://artifact/<id>`` resolves against the *caller's* session id
    from the active runtime tool context. It reuses the service's own
    ``read_tool_output_artifact`` path, which applies the session/workspace
    guards (``_validate_session_workspace``) and the artifact-path containment
    checks, so the URI can never reach a foreign session's artifact or an
    arbitrary external file.
    """

    def __init__(self, runtime: VoidCodeRuntime) -> None:
        self._runtime = runtime

    def read_artifact(
        self,
        *,
        artifact_id: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> dict[str, object] | None:
        context = current_runtime_tool_context()
        if context is None or not context.session_id:
            raise RuntimeError("voidcode://artifact/<id> requires an active runtime tool invocation context")
        result = self._runtime.read_tool_output_artifact(
            session_id=context.session_id,
            artifact_id=artifact_id,
            offset=max(0, offset or 0),
            limit=max(1, limit or 2000),
        )
        if result.get("status") == "artifact_not_found":
            return None
        return result


@final
class VoidCodeRuntime:
    """Headless runtime entrypoint for one local deterministic request."""

    _workspace: Path
    _base_tool_registry: ToolRegistry
    _tool_registry: ToolRegistry
    _tool_materializer: RuntimeToolMaterializer
    _tool_materialization: RuntimeToolMaterialization
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
    _mcp_manager_is_injected: bool
    _acp_adapter: AcpAdapter
    _graph_cache: dict[tuple[ExecutionEngineName, str], RuntimeGraph]
    _context_window_config_override: RuntimeContextWindowConfig | None
    _agent_registry: AgentManifestRegistry
    _run_loop_coordinator: RuntimeRunLoopCoordinator
    _resume_coordinator: RuntimeResumeCoordinator
    _background_task_supervisor: RuntimeBackgroundTaskSupervisor
    _background_process_manager: BackgroundProcessManager
    _context_transform_registry: RuntimeContextTransformRegistry
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
        context_transform_registry: RuntimeContextTransformRegistry | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._permission_context_resolver = RuntimePermissionContextResolver(workspace=self._workspace)
        self._agent_registry = self._runtime_agent_registry()
        self._config = config or load_runtime_config(self._workspace)
        self._bind_tool_scope_resolver()
        self._permission_engine = PermissionEngine(
            _context_resolver=self._permission_context_resolver,
            _permission_config=self._config.permission,
            _patch_path_extractor=extract_paths_from_patch,
        )
        self._model_provider_registry = model_provider_registry or ModelProviderRegistry.with_defaults(provider_configs=self._config.providers)
        self._bind_provider_catalog_collaborators()
        self._provider_summary_projector = ProviderSummaryProjector()
        self._config_workflow_mode_resolution = self._workflow_mode_resolution_for_request_metadata({})
        self._hydrate_provider_model_catalog_cache()
        initial_agent = self._config.agent
        if initial_agent is None and self._config.execution_engine == "provider":
            initial_agent = RuntimeAgentConfig(preset="leader")
        if initial_agent is not None:
            initial_agent = parse_runtime_agent_payload(
                serialize_runtime_agent_config(initial_agent),
                source="runtime config agent",
                hooks=self._config.hooks,
                agent_registry=self._agent_registry,
            )
            assert initial_agent is not None
            self._validate_runtime_agent_for_execution(
                initial_agent,
                source="runtime config agent",
            )
        initial_model = initial_agent.model if initial_agent is not None and initial_agent.model is not None else self._config.model
        initial_execution_engine = _agent_effective_execution_engine(
            self._config.execution_engine,
            initial_agent,
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
        self._bind_provider_auth_inspector()
        self._lsp_manager = lsp_manager or build_lsp_manager(self._config.lsp)
        self._mcp_manager_is_injected = mcp_manager is not None
        self._mcp_manager = mcp_manager or build_mcp_manager(self._config.mcp)
        self._skill_registry_is_injected = skill_registry is not None
        self._skill_registry = skill_registry or self._build_skill_registry(self._config.skills)
        self._base_tool_registry = tool_registry or self._build_base_tool_registry()
        self._tool_materializer = RuntimeToolMaterializer(self._base_tool_registry)
        self._tool_materialization = self._tool_materializer.base()
        self._tool_registry = self._tool_materialization.registry
        self._graph_override = graph
        self._graph_cache = {}
        self._context_window_config_override = self._context_window_config_from_policy(context_window_policy)
        initial_context_window = self._context_window_config_override or self._config.context_window
        self._initial_effective_config = EffectiveRuntimeConfig(
            approval_mode=self._config.approval_mode,
            permission=self._config.permission,
            model=initial_model,
            execution_engine=initial_execution_engine,
            max_steps=self._config.max_steps,
            tool_timeout_seconds=self._config.tool_timeout_seconds,
            provider_fallback=initial_provider_fallback,
            providers=self._config.providers,
            resolved_provider=self._resolved_provider_config,
            agent=initial_agent,
            context_window=initial_context_window,
            tools=self._config.tools,
            policy=self._config.policy,
        )
        if graph is not None:
            self._graph = graph
        elif self._can_build_graph_for_effective_config(self._initial_effective_config):
            self._graph = self._build_graph_for_engine_from_config(self._initial_effective_config)
        else:
            self._graph = None
        self._permission_policy = permission_policy or PermissionPolicy(mode=self._config.approval_mode)
        self._session_store = session_store or SqliteSessionStore()
        self._acp_adapter = acp_adapter or build_acp_adapter(self._config.acp)
        self._context_transform_registry = context_transform_registry or default_runtime_context_transform_registry()
        self._default_context_window_policy = self._context_window_policy_from_config(
            initial_context_window,
            resolved_provider=None,
        )
        self._run_loop_coordinator = RuntimeRunLoopCoordinator(
            self,
            tool_executor=RuntimeToolExecutor(
                workspace=self._workspace,
                memory=self,
                lsp=self,
                lsp_diagnostics_on_write=bool(self._config.lsp is not None and self._config.lsp.diagnostics_on_write),
                tool_catalog=_RuntimeToolCatalogFacade(self),
                artifact=_RuntimeArtifactReadFacade(self),
            ),
        )
        self._resume_coordinator = RuntimeResumeCoordinator(self)
        self._background_task_supervisor = RuntimeBackgroundTaskSupervisor(self)
        self._background_process_manager = BackgroundProcessManager()

    def _bind_provider_catalog_collaborators(self) -> None:
        self._provider_catalog_cache = RuntimeProviderCatalogCache(
            registry=self._model_provider_registry,
            path=provider_catalog_cache_path(),
        )
        self._provider_catalog_query = RuntimeProviderCatalogQuery(
            registry=self._model_provider_registry,
        )

    def _bind_provider_auth_inspector(self) -> None:
        self._provider_auth_inspector = RuntimeProviderAuthInspector(
            providers=self._config.providers,
            resolver=self._provider_auth_resolver,
            env=os.environ,
        )

    def _bind_tool_scope_resolver(self) -> None:
        self._tool_scope_resolver = RuntimeToolScopeResolver(
            memory_enabled=self._config.memory.enabled,
        )

    def __enter__(self) -> VoidCodeRuntime:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = exc_type, exc, tb
        # Shutdown drain order (must be preserved):
        #   1. Drain background-task workers FIRST — ``shutdown_background_tasks``
        #      joins every live worker so each child/background-task result is
        #      durably finalized (task row terminal state, parent-session
        #      notification events, lifecycle hooks) before anything is torn
        #      down, and terminalizes anything that could not finish.
        #   2. Stop spawned background processes.
        #   3. Tear down ACP/MCP/LSP adapters LAST — their per-session release
        #      events were already drained/persisted by the run loop at run end,
        #      so adapter shutdown is purely a client-surface close and must not
        #      race the durable session writes above.
        self.shutdown_background_tasks()
        self._background_process_manager.stop_all()
        _ = self.disconnect_acp()
        _ = self.shutdown_mcp()
        _ = self.shutdown_lsp()

    def shutdown_background_tasks(self, *, timeout_seconds: float = 2.0) -> None:
        """Drain background-task workers before runtime teardown.

        Enforced ordering inside ``RuntimeBackgroundTaskSupervisor.shutdown``:
        set the shutdown flag, join every live worker (each worker finalizes
        its task durably before exiting), then terminalize (mark ``failed`` in
        storage) any worker that could not finish within the timeout. After
        this returns, every task row is terminal and all child/background-task
        results are durable — no pending worker writes can be lost to teardown.
        """
        self._background_task_supervisor.shutdown(timeout_seconds=timeout_seconds)

    def _tool_catalog_lookup(self, tool_name: str) -> ToolDefinition | None:
        """Read-only registry lookup for on-demand tool documentation.

        Resolves against the current materialization (base + MCP + local tools)
        so doc reads stay consistent with the live registry even after MCP
        refreshes.
        """
        tool = self._tool_materialization.registry.tools.get(tool_name)
        return None if tool is None else tool.definition

    def _build_base_tool_registry(self) -> ToolRegistry:
        return ToolRegistry.with_defaults(
            lsp_tool=self._build_lsp_tool(),
            hooks_config=self._config.hooks or RuntimeHooksConfig(),
            edit_schema_resolver=self._edit_schema_resolver(),
            skill_tool=SkillTool(
                list_skills=self._skill_registry.all,
                resolve_skill=self._skill_registry.resolve,
            ),
            task_tool=TaskTool(runtime=self),
            question_tool=QuestionTool(),
            background_output_tool=BackgroundOutputTool(runtime=self),
            background_cancel_tool=BackgroundCancelTool(runtime=self),
            background_process_start_tool=BackgroundProcessStartTool(runtime=self),
            background_process_logs_tool=BackgroundProcessLogsTool(runtime=self),
            background_process_stop_tool=BackgroundProcessStopTool(runtime=self),
            background_process_send_tool=BackgroundProcessSendTool(runtime=self),
        )

    def _edit_schema_resolver(self) -> EditSchemaResolver:
        """Resolve the per-model edit schema from observed edit effectiveness.

        The persisted effectiveness report is cached briefly so per-edit
        resolution does not rescan the event log on every call. Any lookup
        failure degrades to the flexible profile: policy selection must never
        break an edit.
        """
        cache: dict[str, object] = {"report": None, "expires_at": 0.0}
        ttl_seconds = 5.0

        def resolve(model: str | None) -> EditSchema:
            if model is None:
                return EditSchema.FLEXIBLE
            now = time.monotonic()
            cached_report = cast(ToolEffectivenessReport | None, cache["report"])
            expires_at = cast(float, cache["expires_at"])
            if cached_report is None or now >= expires_at:
                try:
                    cached_report = self._session_store.tool_effectiveness_report(workspace=self._workspace)
                except Exception:
                    return EditSchema.FLEXIBLE
                cache["report"] = cached_report
                cache["expires_at"] = now + ttl_seconds
            return select_edit_schema(model, cached_report)

        return resolve

    @property
    def background_process_manager(self) -> BackgroundProcessManager:
        return self._background_process_manager

    def _tool_materialization_with_effective_local_tools(
        self,
        effective_config: EffectiveRuntimeConfig,
    ) -> RuntimeToolMaterialization:
        local_config = effective_config.tools.local if effective_config.tools is not None else None
        local_tools = LocalCustomToolProvider(
            workspace=self._workspace,
            config=local_config,
        ).provide_tools()
        return self._tool_materializer.materialize_local_tools(
            self._tool_materialization,
            local_tools,
        )

    def _tool_registry_with_effective_local_tools(
        self,
        effective_config: EffectiveRuntimeConfig,
    ) -> ToolRegistry:
        return self._tool_materialization_with_effective_local_tools(effective_config).registry

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
            "last_delegation": (acp_state.last_delegation.as_payload() if acp_state.last_delegation is not None else None),
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
        return plan_state_from_metadata(
            metadata,
            status=status,
            approval_request_id=approval_request_id,
            blocked_tool=blocked_tool,
            error=error,
        )

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
            if status is not None and status.startswith("waiting_"):
                plan_state = {"status": status}
                if approval_request_id is not None:
                    plan_state["approval_request_id"] = approval_request_id
                if blocked_tool is not None:
                    plan_state["blocked_tool"] = blocked_tool
                if error is not None:
                    plan_state["last_error"] = error
            else:
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
        return resequence_event(event, sequence=sequence)

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
            emitted.append(RuntimeStreamChunk(kind="event", session=current_session, event=acp_event))
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

    def _build_graph_for_engine_from_config(
        self,
        config: EffectiveRuntimeConfig,
        *,
        use_cache: bool = True,
    ) -> RuntimeGraph:
        cache_key = cache_key_for_effective_config(config)
        if use_cache and cache_key in self._graph_cache:
            return self._graph_cache[cache_key]

        graph = select_graph_for_effective_config(config=config).graph
        if use_cache:
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
        _ = self._workflow_mode_resolution_for_request_metadata(request.metadata)
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
        request_context_transform_refs = request.metadata.get("context_transform_refs")
        context_transform_refs: tuple[str, ...] | None = None
        if request_context_transform_refs is not None:
            assert isinstance(request_context_transform_refs, list)
            context_transform_refs = tuple(cast(list[str], request_context_transform_refs))
            validate_runtime_context_transform_refs(
                context_transform_refs,
                field_path="request metadata 'context_transform_refs'",
                registry=self._context_transform_registry_for_agent(resolved.agent),
            )
        resolved = apply_request_runtime_config_overrides(
            resolved,
            max_steps=request.metadata.get("max_steps"),
            reasoning_effort=request.metadata.get("reasoning_effort"),
            context_transform_refs=context_transform_refs,
        )
        try:
            self._validate_reasoning_effort_capability(resolved)
        except ValueError as exc:
            raise RuntimeRequestError(str(exc)) from exc
        return resolved

    def _workflow_mode_resolution_for_request_metadata(
        self,
        metadata: Mapping[str, object],
    ) -> WorkflowModeResolution:
        command_workflow_mode: str | None = None
        command_metadata = metadata.get("command")
        if isinstance(command_metadata, dict):
            command_payload = cast(dict[str, object], command_metadata)
            raw_command_workflow_mode = command_payload.get("workflow_mode")
            if isinstance(raw_command_workflow_mode, str):
                command_workflow_mode = raw_command_workflow_mode
            elif isinstance(command_payload.get("name"), str):
                command_definition = load_command_registry(workspace=self._workspace).get(cast(str, command_payload["name"]))
                if command_definition is not None:
                    command_workflow_mode = command_definition.workflow_mode
        raw_workflow_mode = metadata.get("workflow_mode")
        metadata_workflow_mode = raw_workflow_mode if isinstance(raw_workflow_mode, str) else None
        if metadata_workflow_mode is None:
            raw_top_workflow_mode = metadata.get("workflow_mode")
            if isinstance(raw_top_workflow_mode, str):
                metadata_workflow_mode = raw_top_workflow_mode
        inherited_workflow_mode: str | None = None
        raw_workflow = metadata.get("workflow")
        if isinstance(raw_workflow, dict):
            normalized_workflow = self._workflow_snapshot_from_metadata({"workflow": cast(dict[str, object], raw_workflow)})
            if normalized_workflow is not None:
                raw_effective = normalized_workflow.get("effective")
                if isinstance(raw_effective, dict):
                    raw_mode = cast(dict[str, object], raw_effective).get("mode")
                    if isinstance(raw_mode, str):
                        inherited_workflow_mode = raw_mode
                if inherited_workflow_mode is None:
                    raw_requested = normalized_workflow.get("requested")
                    if isinstance(raw_requested, dict):
                        raw_mode = cast(dict[str, object], raw_requested).get("workflow_mode")
                        if isinstance(raw_mode, str):
                            inherited_workflow_mode = raw_mode
        if metadata_workflow_mode is None:
            metadata_workflow_mode = inherited_workflow_mode
        try:
            return resolve_workflow_mode(
                command_workflow_mode=command_workflow_mode,
                metadata_workflow_mode=metadata_workflow_mode,
            )
        except ValueError as exc:
            raise RuntimeRequestError(str(exc)) from exc

    @staticmethod
    def _workflow_mode_prompt_context(resolution: WorkflowModeResolution | None) -> str:
        if resolution is None:
            return ""
        mode = resolution.mode
        return f"Workflow mode: {mode.id}. {mode.description} Guidance only; does not expand tool permissions or agent scope."

    @staticmethod
    def _metadata_without_workflow_mode(metadata: Mapping[str, object]) -> dict[str, object]:
        sanitized = dict(metadata)
        sanitized.pop("workflow_mode", None)
        raw_command = sanitized.get("command")
        if isinstance(raw_command, dict):
            command = dict(cast(dict[str, object], raw_command))
            command.pop("workflow_mode", None)
            sanitized["command"] = command
        return sanitized

    @staticmethod
    def _validate_command_workflow_metadata(metadata: Mapping[str, object]) -> None:
        raw_command = metadata.get("command")
        if not isinstance(raw_command, dict):
            return
        from .contracts import validate_runtime_command_metadata

        _ = validate_runtime_command_metadata(raw_command)

    @staticmethod
    def _validate_explicit_workflow_mode_metadata(metadata: Mapping[str, object]) -> None:
        raw_workflow_mode = metadata.get("workflow_mode")
        if raw_workflow_mode is None:
            return
        if not isinstance(raw_workflow_mode, str) or not raw_workflow_mode:
            raise RuntimeRequestError("request metadata 'workflow_mode' must be a non-empty string")

    @staticmethod
    def _restore_explicit_workflow_mode(
        metadata: RuntimeRequestMetadataPayload,
        raw_metadata: Mapping[str, object],
    ) -> RuntimeRequestMetadataPayload:
        raw_command = raw_metadata.get("command")
        if not isinstance(raw_command, dict):
            raw_workflow_mode = raw_metadata.get("workflow_mode")
            if not isinstance(raw_workflow_mode, str):
                return metadata
            return cast(
                RuntimeRequestMetadataPayload,
                {**cast(dict[str, object], metadata), "workflow_mode": raw_workflow_mode},
            )
        raw_command_payload = cast(dict[str, object], raw_command)
        raw_command_workflow_mode = raw_command_payload.get("workflow_mode")
        if isinstance(raw_command_workflow_mode, str):
            return cast(
                RuntimeRequestMetadataPayload,
                {
                    **cast(dict[str, object], metadata),
                    "workflow_mode": raw_command_workflow_mode,
                },
            )
        raw_workflow_mode = raw_metadata.get("workflow_mode")
        if not isinstance(raw_workflow_mode, str):
            return metadata
        return cast(
            RuntimeRequestMetadataPayload,
            {**cast(dict[str, object], metadata), "workflow_mode": raw_workflow_mode},
        )

    @staticmethod
    def _workflow_snapshot_with_effective_mode(
        workflow_snapshot: Mapping[str, object],
        workflow_mode: str,
    ) -> dict[str, object]:
        payload = dict(workflow_snapshot)
        raw_requested = payload.get("requested")
        requested = dict(cast(dict[str, object], raw_requested)) if isinstance(raw_requested, dict) else {}
        requested["workflow_mode"] = workflow_mode
        payload["requested"] = requested
        raw_effective = payload.get("effective")
        effective = dict(cast(dict[str, object], raw_effective)) if isinstance(raw_effective, dict) else {}
        effective["mode"] = workflow_mode
        payload["effective"] = effective
        payload["mode"] = workflow_mode
        return payload

    def _workflow_snapshot_for_resolution(
        self,
        resolution: WorkflowModeResolution,
    ) -> dict[str, object]:
        effective: dict[str, object] = {
            "mode": resolution.workflow_mode,
            "source": resolution.source,
        }
        return {
            "snapshot_version": 2,
            "requested": {
                "workflow_mode": resolution.workflow_mode,
            },
            "effective": effective,
            "mode": resolution.workflow_mode,
            "source": resolution.source,
        }

    def _request_metadata_with_workflow_defaults(
        self,
        metadata: dict[str, object],
    ) -> dict[str, object]:
        return metadata

    @staticmethod
    def _workflow_snapshot_from_metadata(
        metadata: dict[str, object] | None,
    ) -> dict[str, object] | None:
        return workflow_snapshot_from_metadata(metadata)

    def _workflow_metadata_for_delegated_child(
        self,
        *,
        metadata: dict[str, object],
        selected_child_preset: str,
        parent_session_id: str | None,
    ) -> dict[str, object]:
        inherited_snapshot = self._workflow_snapshot_from_metadata(metadata)
        if inherited_snapshot is None and parent_session_id is not None:
            parent_response = self._load_existing_session_if_present(session_id=parent_session_id)
            parent_metadata = parent_response.session.metadata if parent_response is not None else self._active_session_metadata(parent_session_id)
            inherited_snapshot = self._workflow_snapshot_from_metadata(parent_metadata)
        _ = selected_child_preset
        return inherited_snapshot or {}

    def _validate_reasoning_effort_capability(self, config: EffectiveRuntimeConfig) -> None:
        if config.reasoning_effort is None:
            return
        if config.execution_engine != "provider":
            return
        active_target = config.resolved_provider.active_target.selection
        provider_name = active_target.provider
        model_name = active_target.model
        if provider_name is None or model_name is None:
            return
        if provider_supports_reasoning_effort(provider_name, model_name) is False:
            raise ValueError(
                "reasoning_effort is configured but model "
                f"'{provider_name}/{model_name}' does not support reasoning effort; "
                "remove the reasoning_effort hint or pick a reasoning-effort capable model"
            )

    def _build_skill_registry(self, skills_config: RuntimeSkillsConfig | None) -> SkillRegistry:
        if skills_config is None or skills_config.enabled is not True:
            return skill_registry_with_builtins(())
        if skills_config.paths:
            discovered = SkillRegistry.discover(
                workspace=self._workspace,
                search_paths=skills_config.paths,
            )
        else:
            discovered = SkillRegistry.discover(workspace=self._workspace)
        return skill_registry_with_builtins(discovered.all())

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
        return self._build_skill_registry(self._skills_config_for_effective_config(effective_config))

    def _build_lsp_tool(self) -> Tool | None:
        if self._lsp_manager.current_state().mode != "managed":
            return None
        from ..tools.lsp import LspTool

        return LspTool(requester=self.request_lsp)

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
            if tool.enabled
        )

    def _refresh_mcp_tools(self) -> None:
        if self._mcp_manager.current_state().mode != "managed":
            return
        self._tool_materialization = self._tool_materializer.materialize_mcp_tools(self._build_mcp_tools())
        self._tool_registry = self._tool_materialization.registry

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
            self._tool_materialization = self._tool_materializer.materialize_mcp_tools(
                self._build_mcp_tools_for_owner(owner_session_id=session.session.id)
            )
            self._tool_registry = self._tool_materialization.registry
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
            emitted = tuple(RuntimeStreamChunk(kind="event", session=session, event=event) for event in emitted_events)
            last_sequence = emitted_events[-1].sequence if emitted_events else sequence
            return emitted, session, last_sequence, None
        return (), session, sequence, None

    @staticmethod
    def _is_background_child_mcp_deferred(
        *,
        request_metadata: Mapping[str, object],
        effective_config: EffectiveRuntimeConfig,
        workflow_snapshot: dict[str, object] | None,
    ) -> bool:
        if request_metadata.get("background_run") is not True:
            return False
        agent_binding = effective_config.agent.mcp_binding if effective_config.agent is not None else None
        if agent_binding is not None:
            return False
        if workflow_snapshot is not None:
            raw_intents = workflow_snapshot.get("mcp_binding_intents")
            if isinstance(raw_intents, list):
                for raw_intent in raw_intents:
                    if not isinstance(raw_intent, dict):
                        continue
                    if cast(dict[str, object], raw_intent).get("required") is True:
                        return False
        return True

    def _should_skip_mcp_startup_for_request(
        self,
        *,
        request_metadata: Mapping[str, object],
        effective_config: EffectiveRuntimeConfig,
    ) -> bool:
        _ = request_metadata
        if self._mcp_manager_is_injected:
            return False
        configured_servers = set(self._mcp_manager.current_state().configuration.servers)
        builtin_servers = {"context7", "websearch", "grep_app"}
        if not configured_servers <= builtin_servers:
            return False
        # An explicitly supplied graph is already the execution boundary.  Eagerly
        # discovering the default remote MCP catalog here adds network latency even
        # though that graph cannot depend on runtime-selected MCP tools.  Keep
        # discovery enabled when a caller injected an MCP manager so MCP integration
        # tests and custom managers retain their explicit behavior.
        return effective_config.execution_engine == "deterministic" or self._graph_override is not None

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
            if tool.enabled
        )

    def _tool_registry_for_effective_config(
        self,
        effective_config: EffectiveRuntimeConfig,
        metadata: dict[str, object] | None = None,
    ) -> ToolRegistry:
        return self._tool_materialization_for_effective_config(
            effective_config,
            metadata,
        ).registry

    def _provider_tool_definitions(
        self,
        tool_registry: ToolRegistry,
        effective_config: EffectiveRuntimeConfig,
    ) -> tuple[ToolDefinition, ...]:
        """Provider-visible tool definitions for a request.

        With ``tools.essential_only`` enabled, only the essential tool set plus
        agent-allowlist-required tools are exposed top-level; discoverable
        tools stay registered and reachable via ``invoke_tool`` dispatch.
        Disabled (default) keeps the historical all-tools-top-level behavior.
        """
        if effective_config.tools is None or effective_config.tools.essential_only is not True:
            return tool_registry.definitions()
        return tool_registry.provider_definitions(
            allowlist_patterns=agent_required_tool_patterns(effective_config.agent),
        )

    def _skill_prompt_context_for_assembly(
        self,
        *,
        skill_registry: SkillRegistry,
        applied_context: str,
        selected_skill_names: tuple[str, ...],
    ) -> str:
        """System-prompt skill metadata: applied skill bodies plus the catalog.

        The catalog (name + description per skill) is always included when
        skills exist so the model can discover and lazily load skill bodies via
        the skill tool, independent of which skills are currently applied.
        """
        catalog = self._catalog_skill_context(
            skill_registry,
            available_skill_names=tuple(self._loaded_skill_names(skill_registry)),
            selected_skill_names=selected_skill_names,
        )
        return "\n\n".join(part for part in (applied_context, catalog) if part)

    def _tool_materialization_for_effective_config(
        self,
        effective_config: EffectiveRuntimeConfig,
        metadata: dict[str, object] | None = None,
    ) -> RuntimeToolMaterialization:
        materialization = self._tool_materialization_with_effective_local_tools(effective_config)
        registry = self._tool_scope_resolver.scope(
            materialization.registry,
            agent=effective_config.agent,
            metadata=metadata,
        )
        return materialization.scoped(registry)

    def _tool_registry_with_workflow_policy(
        self,
        registry: ToolRegistry,
        metadata: dict[str, object] | None,
    ) -> ToolRegistry:
        return self._tool_scope_resolver.apply_policy(registry, metadata=metadata)

    def _tool_policy_decisions(
        self,
        registry: ToolRegistry,
        metadata: dict[str, object] | None,
    ) -> tuple[ToolPolicyDecision, ...]:
        return self._tool_scope_resolver.decisions(registry, metadata=metadata)

    def _tool_policy_decision(
        self,
        *,
        tool_name: str,
        registry: ToolRegistry,
        metadata: dict[str, object] | None,
    ) -> ToolPolicyDecision:
        return self._tool_scope_resolver.decision(
            tool_name=tool_name,
            registry=registry,
            metadata=metadata,
        )

    @staticmethod
    def _runtime_mode_for_policy_metadata(metadata: dict[str, object] | None) -> str:
        return RuntimeToolScopeResolver.runtime_mode(metadata)

    @staticmethod
    def _runtime_read_only_for_policy_metadata(metadata: dict[str, object] | None) -> bool:
        return RuntimeToolScopeResolver.runtime_read_only(metadata)

    def _effective_runtime_read_only_for_policy_metadata(
        self,
        metadata: dict[str, object] | None,
    ) -> bool:
        return self._tool_scope_resolver.effective_read_only(metadata)

    def _tool_policy_denial(
        self,
        *,
        session: SessionState,
        tool_name: str,
    ) -> ToolPolicyDecision | None:
        effective_config = self._effective_runtime_config_from_metadata(session.metadata)
        registry = self._tool_registry_with_effective_local_tools(effective_config)
        return self._tool_scope_resolver.denial(
            registry,
            agent=effective_config.agent,
            metadata=session.metadata,
            tool_name=tool_name,
        )

    @staticmethod
    def _tool_policy_error(decision: ToolPolicyDecision) -> str:
        reason = decision.reason or "runtime tool policy denied the tool"
        return f"{reason}: '{decision.tool_name}'"

    def _delegation_tool_policy_error(
        self,
        *,
        session: SessionState,
        tool_name: str,
    ) -> str | None:
        # Runtime-owned child preset governance: provider-visible schemas are already
        # narrowed, but malicious/raw provider tool calls still need a clear policy
        # denial before normal lookup can obscure the reason as an unknown tool.
        route = runtime_subagent_route_from_metadata(
            session.metadata,
            callable_subagent_presets=self._agent_registry.executable_subagent_ids(),
        )
        if route is None:
            return None
        effective_config = self._effective_runtime_config_from_metadata(session.metadata)
        agent = effective_config.agent
        return self._tool_scope_resolver.delegation_policy_error(
            delegated_child=True,
            agent=agent,
            base_registry=self._base_tool_registry,
            tool_name=tool_name,
        )

    def _workflow_tool_policy_error(
        self,
        *,
        session: SessionState,
        tool_name: str,
    ) -> str | None:
        denial = self._tool_policy_denial(session=session, tool_name=tool_name)
        return None if denial is None else str(denial.reason)

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

    def request_diagnostics(
        self,
        *,
        file_path: str,
        workspace: str,
    ) -> dict[str, object]:
        _ = workspace
        result = self.request_lsp(
            server_name=None,
            method="textDocument/diagnostic",
            params={
                "textDocument": {
                    "uri": (self._workspace / file_path).resolve().as_uri(),
                }
            },
            workspace=self._workspace,
        )
        return {"lsp_response": result.response}

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
        return self._release_mcp_session_events(session_id=session_id, start_sequence=1)

    def _release_mcp_session_events(
        self,
        *,
        session_id: str,
        start_sequence: int,
    ) -> tuple[EventEnvelope, ...]:
        release_session = getattr(self._mcp_manager, "release_session", None)
        if release_session is None:
            return ()
        return self._envelopes_for_mcp_events(
            session_id=session_id,
            start_sequence=start_sequence,
            mcp_events=cast(
                tuple[object, ...],
                release_session(session_id=session_id),
            ),
        )

    def cleanup_idle_mcp_sessions(
        self,
        *,
        max_idle_seconds: float = 300.0,
    ) -> tuple[EventEnvelope, ...]:
        return self._envelopes_for_mcp_events(
            session_id="runtime",
            start_sequence=1,
            mcp_events=cast(
                tuple[object, ...],
                self._mcp_manager.cleanup_idle_session_servers(max_idle_seconds=max_idle_seconds),
            ),
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
        return self._acp_adapter.request(AcpRequestEnvelope(request_type=request_type, payload=payload))

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
                lifecycle_status=("waiting_approval" if task.status == "running" and task.approval_request_id else task.status),
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

    def _runtime_agent_registry(self) -> AgentManifestRegistry:
        registry = load_agent_manifest_registry(self._workspace)
        builtin = dict(registry.builtin)
        for agent_id in tuple(builtin):
            manifest = get_builtin_agent_manifest(agent_id)
            if manifest is not None:
                builtin[agent_id] = manifest
        return AgentManifestRegistry(builtin=builtin, custom=registry.custom)

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
        delegation_dict = cast(dict[str, object], delegation_metadata) if isinstance(delegation_metadata, dict) else {}
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
            selected_preset=(cast(str, delegation_dict["selected_preset"]) if isinstance(delegation_dict.get("selected_preset"), str) else None),
            selected_execution_engine=(
                cast(str, delegation_dict["selected_execution_engine"]) if isinstance(delegation_dict.get("selected_execution_engine"), str) else None
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
            approval_blocked=(approval_blocked if approval_blocked is not None else task.status == "running"),
            result_available=(result_available if result_available is not None else task.result_available),
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
        correlation_id = task.approval_request_id or task.question_request_id or task.session_id or "none"
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
                dedupe_key=(f"{RUNTIME_ACP_DELEGATED_LIFECYCLE}:{task.task.id}:{lifecycle_status}:{correlation_id}"),
            )
        except (AttributeError, UnknownSessionError):
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
        abort_signal = self._register_active_session_id(
            session_id,
            run_id=run_id,
            metadata={
                "prompt": request.prompt,
                "run_id": run_id,
                **{key: value for key, value in request.metadata.items()},
                "request_metadata": {key: value for key, value in request.metadata.items()},
            },
        )
        try:
            events: list[EventEnvelope] = []
            output: str | None = None
            final_session: SessionState | None = None

            try:
                for chunk in self._stream_chunks(
                    request,
                    session_id=session_id,
                    run_id=run_id,
                    abort_signal=abort_signal,
                ):
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
                    response = RuntimeResponse(session=final_session, events=tuple(events), output=output)
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
            final_session = session_with_command_artifacts(
                final_session,
                output=output,
            )

            response = RuntimeResponse(session=final_session, events=tuple(events), output=output)
            self._persist_response(request=request, response=response)

            # Follow-up messages are delivered only after the current run has
            # reached a durable terminal state. They become ordinary prompts
            # on the same session and therefore share the existing runtime
            # approval, persistence, and event paths.
            if final_session.status == "completed":
                followup_metadata, followups = drain_runtime_messages(
                    final_session.metadata,
                    kind="follow_up",
                )
                if followups:
                    self._session_store.update_session_metadata(
                        workspace=self._workspace,
                        session_id=session_id,
                        metadata=followup_metadata,
                    )
                    for followup in followups:
                        followup_request = RuntimeRequest(
                            prompt=followup.content,
                            session_id=session_id,
                            parent_session_id=request.parent_session_id,
                            metadata=request.metadata,
                        )
                        yield from self._run_with_persistence(
                            followup_request,
                            allow_internal_metadata=allow_internal_metadata,
                        )
        finally:
            self._unregister_active_session_id(session_id, run_id=run_id)

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
        final_session = session_with_command_artifacts(
            final_session,
            output=output,
        )

        return RuntimeResponse(session=final_session, events=tuple(events), output=output)

    @staticmethod
    def _session_with_loaded_skill_metadata(
        session: SessionState,
        *,
        events: tuple[EventEnvelope, ...],
        force_loaded_skills: tuple[dict[str, object], ...] = (),
    ) -> SessionState:
        loaded_payloads = [event.payload for event in events if event.event_type == "runtime.skill_loaded"]
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

    def _persist_emitted_event(
        self,
        *,
        session_id: str,
        event_type: str,
        source: EventSource,
        payload: dict[str, object],
        dedupe_key: str | None = None,
    ) -> EventEnvelope:
        """Persist one service-emitted event; return its DB-assigned envelope.

        Startup/tail events that service.py emits outside the graph loop are no
        longer bulk-written by ``save_run`` (now a terminal seal-writer), so each
        is appended incrementally here. The store assigns the authoritative
        sequence, which the caller adopts for downstream ACP/hook/MCP arithmetic.
        """
        return self._session_store.append_session_events(
            workspace=self._workspace,
            session_id=session_id,
            events=((event_type, source, payload, dedupe_key),),
        )[0]

    def _persist_emitted_chunk(self, chunk: RuntimeStreamChunk) -> RuntimeStreamChunk:
        event = chunk.event
        if event is None:
            return chunk
        envelope = self._persist_emitted_event(
            session_id=event.session_id,
            event_type=event.event_type,
            source=event.source,
            payload=event.payload,
        )
        return RuntimeStreamChunk(kind="event", session=chunk.session, event=envelope)

    def _persist_emitted_chunks(
        self,
        chunks: Iterable[RuntimeStreamChunk],
        *,
        fallback_sequence: int,
    ) -> Generator[RuntimeStreamChunk, None, int]:
        """Persist a batch of service-emitted chunks, yielding DB-sequenced copies.

        The final DB-assigned sequence is returned via ``yield from`` so callers
        can keep their local ``sequence`` bookkeeping aligned with the store.
        """
        sequence = fallback_sequence
        for chunk in chunks:
            event = chunk.event
            if event is None:
                yield chunk
                continue
            envelope = self._persist_emitted_event(
                session_id=event.session_id,
                event_type=event.event_type,
                source=event.source,
                payload=event.payload,
            )
            sequence = envelope.sequence
            yield RuntimeStreamChunk(kind="event", session=chunk.session, event=envelope)
        return sequence

    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        if "provider_stream" in request.metadata:
            return self._run_with_persistence(request)

        stream_metadata = {**request.metadata, "provider_stream": True}
        self._validate_explicit_workflow_mode_metadata(stream_metadata)
        self._validate_command_workflow_metadata(stream_metadata)
        validated_stream_metadata = validate_runtime_request_metadata(self._metadata_without_workflow_mode(stream_metadata))
        validated_stream_metadata = self._restore_explicit_workflow_mode(
            validated_stream_metadata,
            stream_metadata,
        )
        request_with_stream = RuntimeRequest(
            prompt=request.prompt,
            session_id=request.session_id,
            parent_session_id=request.parent_session_id,
            metadata=validated_stream_metadata,
            allocate_session_id=request.allocate_session_id,
        )
        return self._run_with_persistence(request_with_stream)

    def _stream_chunks(
        self,
        request: RuntimeRequest,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        resolved_session_id = session_id or self._resolve_session_id(request)
        effective_config = self._runtime_config_for_request(request)
        if self._graph_override is None:
            self._validate_provider_execution_ready(effective_config)
        request_metadata = self._request_metadata_with_workflow_defaults(self._fresh_request_metadata(request.metadata))
        workflow_mode_resolution = self._workflow_mode_resolution_for_request_metadata(request_metadata)
        self._config_workflow_mode_resolution = workflow_mode_resolution
        workflow_snapshot = self._workflow_snapshot_for_resolution(workflow_mode_resolution)
        existing_workflow = request_metadata.get("workflow")
        if isinstance(existing_workflow, dict):
            workflow_snapshot = dict(cast(dict[str, object], existing_workflow))
        if "delegated_child" in workflow_snapshot:
            workflow_snapshot = self._workflow_snapshot_with_effective_mode(
                workflow_snapshot,
                workflow_mode_resolution.workflow_mode,
            )
        explicit_workflow_mode = workflow_mode_resolution.source != "default" or "workflow_mode" in request_metadata
        if explicit_workflow_mode:
            workflow_snapshot_for_session = workflow_snapshot
            request_metadata = {
                **request_metadata,
                "workflow_mode": workflow_mode_resolution.workflow_mode,
                "workflow": workflow_snapshot_for_session,
            }
        else:
            workflow_snapshot_for_session = workflow_snapshot
        existing_session = (
            self._load_existing_session_if_present(
                session_id=request.session_id,
            )
            if request.session_id is not None
            else None
        )
        if existing_session is not None and request.session_id is not None:
            queued_metadata, queued_steering = drain_runtime_messages(
                existing_session.session.metadata,
                kind="steering",
            )
            if queued_steering:
                steering_text = "\n\n".join(message.content for message in queued_steering)
                request = replace(
                    request,
                    prompt=(f"{request.prompt}\n\nRuntime steering messages:\n{steering_text}" if request.prompt.strip() else steering_text),
                )
                self._session_store.update_session_metadata(
                    workspace=self._workspace,
                    session_id=resolved_session_id,
                    metadata=queued_metadata,
                )
        rehydrated_conversation_segments = self._rehydrated_conversation_segments_for_existing_session(
            stored=existing_session,
            parent_session_id=request.parent_session_id,
        )
        rehydrated_tool_results = self._rehydrated_tool_results_for_existing_session(
            stored=existing_session,
            parent_session_id=request.parent_session_id,
        )
        if run_id is not None:
            ACTIVE_SESSION_REGISTRY.remember_metadata(
                workspace=self._workspace,
                session_id=resolved_session_id,
                run_id=run_id,
                metadata={
                    "prompt": request.prompt,
                    "run_id": run_id,
                    **request_metadata,
                    "request_metadata": {key: value for key, value in request_metadata.items()},
                },
            )
        hook_workflow_mode_resolution = workflow_mode_resolution if explicit_workflow_mode else None
        resolved_hook_presets = self._build_hook_preset_snapshot(
            effective_config.agent,
            workflow_mode_resolution=hook_workflow_mode_resolution,
        )
        session_request_metadata = dict(request_metadata)
        session_request_metadata.pop("background_rate_limit_retry", None)
        session_request_metadata.pop("show_thinking", None)
        parent_runtime_policy = None
        if request.parent_session_id is not None:
            parent_policy_metadata = self._parent_policy_metadata(request.parent_session_id)
            if parent_policy_metadata is not None:
                raw_parent_runtime_policy = parent_policy_metadata.get("runtime_policy")
                if raw_parent_runtime_policy is not None:
                    if not isinstance(raw_parent_runtime_policy, dict):
                        raise ValueError("persisted parent runtime_policy must be an object")
                    parent_runtime_policy = cast(dict[str, object], raw_parent_runtime_policy)
        persisted_runtime_policy = None
        if existing_session is not None and existing_session.session.session.parent_id == request.parent_session_id:
            raw_persisted_runtime_policy = existing_session.session.metadata.get("runtime_policy")
            if raw_persisted_runtime_policy is not None:
                if not isinstance(raw_persisted_runtime_policy, dict):
                    raise ValueError("persisted runtime_policy must be an object")
                persisted_runtime_policy = cast(dict[str, object], raw_persisted_runtime_policy)
        session = SessionState(
            session=SessionRef(id=resolved_session_id, parent_id=request.parent_session_id),
            status="running",
            turn=1,
            metadata={
                **session_request_metadata,
                "workspace": str(self._workspace),
                "runtime_config": self._runtime_config_metadata(
                    effective_config,
                    workflow_snapshot=workflow_snapshot_for_session if explicit_workflow_mode else None,
                    workflow_mode_resolution=hook_workflow_mode_resolution,
                ),
                "runtime_policy": materialize_runtime_policy_snapshot(
                    persisted_session_policy=persisted_runtime_policy,
                    agent_preset=effective_config.agent.preset if effective_config.agent is not None else "leader",
                    agent_manifest_id=effective_config.agent.preset if effective_config.agent is not None else "leader",
                    runtime_config={
                        **self._runtime_config_metadata(
                            effective_config,
                            workflow_snapshot=workflow_snapshot_for_session if explicit_workflow_mode else None,
                            workflow_mode_resolution=hook_workflow_mode_resolution,
                        ),
                    },
                    request_metadata=request_metadata,
                    parent_snapshot=parent_runtime_policy,
                ).as_payload(),
                **({"resolved_hook_presets": resolved_hook_presets.to_payload()} if resolved_hook_presets.presets else {}),
                "runtime_state": self._runtime_state_metadata(run_id=run_id),
            },
        )
        # Every fresh run must start from a writable row, ALWAYS. ``status`` in
        # {completed, failed} is per-TURN, not per-session: follow-up messages
        # re-enter the same session_id, so a previously sealed row would reject
        # this turn's first ``append_session_events`` with ``SessionSealedError``.
        # ``save_interrupted_checkpoint`` INSERTs a new row (first run) or
        # UPDATEs an existing one to ``status='interrupted'`` (un-sealing
        # completed/failed), and ``create_if_missing=True`` guarantees the row
        # exists before the first append. Resume / approval / question paths
        # enter through ``_resume_coordinator``, not this method, so their
        # checkpoints are untouched.
        self._session_store.save_interrupted_checkpoint(
            workspace=self._workspace,
            session_id=resolved_session_id,
            prompt=request.prompt,
            session_metadata=session.metadata,
            tool_results=(),
            last_event_sequence=0,
            output=None,
            create_if_missing=True,
            turn=session.turn,
        )

        runtime_policy_snapshot = session.metadata.get("runtime_policy")
        request_received_payload: dict[str, object] = {
            "prompt": request.prompt,
            **({"agent_preset": active_agent.preset} if (active_agent := effective_config.agent) is not None else {}),
        }
        if isinstance(runtime_policy_snapshot, dict):
            request_received_payload["runtime_policy"] = runtime_policy_observability_payload(cast(dict[str, object], runtime_policy_snapshot))
        request_received_envelope = self._persist_emitted_event(
            session_id=session.session.id,
            event_type="runtime.request_received",
            source="runtime",
            payload=request_received_payload,
        )
        sequence = request_received_envelope.sequence
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=request_received_envelope,
        )

        for diagnostic in self._category_model_diagnostics(
            request_metadata=request_metadata,
            effective_config=effective_config,
        ):
            envelope = self._persist_emitted_event(
                session_id=session.session.id,
                event_type=RUNTIME_CATEGORY_MODEL_DIAGNOSTIC,
                source="runtime",
                payload=diagnostic,
            )
            sequence = envelope.sequence
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=envelope,
            )

        reasoning_diagnostic = self._reasoning_controls_diagnostic_for_config(effective_config)
        if reasoning_diagnostic is not None:
            envelope = self._persist_emitted_event(
                session_id=session.session.id,
                event_type=RUNTIME_REASONING_DIAGNOSTIC,
                source="runtime",
                payload=reasoning_diagnostic,
            )
            sequence = envelope.sequence
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=envelope,
            )

        command_metadata = request_metadata.get("command")
        if isinstance(command_metadata, dict):
            envelope = self._persist_emitted_event(
                session_id=session.session.id,
                event_type=COMMAND_RESOLVED,
                source="runtime",
                payload={
                    **cast(dict[str, object], command_metadata),
                    "rendered_prompt": request.prompt,
                },
            )
            sequence = envelope.sequence
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=envelope,
            )

        if self._should_skip_mcp_startup_for_request(
            request_metadata=request_metadata,
            effective_config=effective_config,
        ) or self._is_background_child_mcp_deferred(
            request_metadata=request_metadata,
            effective_config=effective_config,
            workflow_snapshot=workflow_snapshot_for_session,
        ):
            self._tool_materialization = self._tool_materializer.base()
            self._tool_registry = self._tool_materialization.registry
        else:
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
                persisted_chunk = self._persist_emitted_chunk(chunk)
                sequence = cast(EventEnvelope, persisted_chunk.event).sequence
                yield persisted_chunk
            if mcp_failed_chunk is not None:
                yield self._persist_emitted_chunk(mcp_failed_chunk)
                return

        tool_materialization = self._tool_materialization_for_effective_config(
            effective_config,
            session.metadata,
        )
        session = self._session_with_agent_capability_snapshot(
            session=session,
            effective_config=effective_config,
            request_metadata=request_metadata,
            resolved_hook_presets=resolved_hook_presets,
            workflow_snapshot=workflow_snapshot_for_session,
            tool_materialization=tool_materialization,
        )
        tool_registry = tool_materialization.registry
        skill_registry = self._skill_registry_for_effective_config(effective_config)

        start_hook_outcome = self._run_lifecycle_hooks(
            session=session,
            sequence=sequence,
            surface="session_start",
            payload={"prompt": request.prompt},
        )
        sequence = yield from self._persist_emitted_chunks(
            start_hook_outcome.chunks,
            fallback_sequence=start_hook_outcome.last_sequence,
        )
        if start_hook_outcome.failed_error is not None:
            failed_chunk = self._lifecycle_hook_failure_chunk(
                session=session,
                sequence=sequence,
                surface="session_start",
                error=start_hook_outcome.failed_error,
            )
            if failed_chunk is not None:
                yield self._persist_emitted_chunk(failed_chunk)
                return

        loaded_skill_names = self._loaded_skill_names(skill_registry)

        startup_chunks, session, sequence, startup_failed_chunk = self._start_run_acp(
            session=session,
            sequence=sequence,
        )
        sequence = yield from self._persist_emitted_chunks(
            startup_chunks,
            fallback_sequence=sequence,
        )
        if startup_failed_chunk is not None:
            yield self._persist_emitted_chunk(startup_failed_chunk)
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
        skill_prompt_context = self._skill_prompt_context_for_assembly(
            skill_registry=skill_registry,
            applied_context=skill_snapshot.skill_prompt_context,
            selected_skill_names=skill_snapshot.selected_skill_names,
        )
        # Persist the resolved snapshot for every run, including runs with no
        # loaded skills. Resume/replay must have a deterministic snapshot
        # boundary even when the effective skills configuration is disabled.
        session = SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata={
                **session.metadata,
                **self._snapshot_to_session_metadata(skill_snapshot),
            },
        )
        skills_loaded_envelope = self._persist_emitted_event(
            session_id=session.session.id,
            event_type=RUNTIME_SKILLS_LOADED,
            source="runtime",
            payload={
                "skills": loaded_skill_names,
                "selected_skills": list(skill_snapshot.selected_skill_names),
                "catalog_context_length": len(catalog_skill_context),
            },
        )
        sequence = skills_loaded_envelope.sequence
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=skills_loaded_envelope,
        )

        if skill_snapshot.applied_skill_payloads:
            skills_applied_envelope = self._persist_emitted_event(
                session_id=session.session.id,
                event_type=RUNTIME_SKILLS_APPLIED,
                source="runtime",
                payload={
                    "skills": list(skill_snapshot.selected_skill_names),
                    "count": len(skill_snapshot.applied_skill_payloads),
                    "prompt_context_built": bool(skill_prompt_context),
                    "prompt_context_length": len(skill_prompt_context),
                },
            )
            sequence = skills_applied_envelope.sequence
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=skills_applied_envelope,
            )

        hook_preset_snapshot = self._hook_preset_event_payload_from_session_metadata(session.metadata)
        if hook_preset_snapshot is not None:
            hook_presets_envelope = self._persist_emitted_event(
                session_id=session.session.id,
                event_type=RUNTIME_HOOK_PRESETS_LOADED,
                source="runtime",
                payload=hook_preset_snapshot,
            )
            sequence = hook_presets_envelope.sequence
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=hook_presets_envelope,
            )

        assembled_context = self._assemble_provider_context(
            prompt=request.prompt,
            tool_results=rehydrated_tool_results,
            session_metadata=session.metadata,
            skill_prompt_context=skill_prompt_context,
            workflow_mode_prompt_context=self._workflow_mode_prompt_context(workflow_mode_resolution if explicit_workflow_mode else None),
            replayed_conversation_segments=rehydrated_conversation_segments,
        )
        session = self._session_with_context_window_payload_metadata(
            session,
            dict(assembled_context.metadata),
        )
        graph_request = GraphRunRequest(
            session=session,
            prompt=request.prompt,
            available_tools=self._provider_tool_definitions(tool_registry, effective_config),
            context_window=self._prepare_provider_context_window(
                prompt=request.prompt,
                tool_results=rehydrated_tool_results,
                session_metadata=session.metadata,
                abort_signal=abort_signal,
            ),
            assembled_context=assembled_context,
            metadata={
                **request_metadata,
                "agent_preset": serialize_runtime_agent_config(self._effective_runtime_config_from_metadata(session.metadata).agent),
                "provider_attempt": 0,
                "provider_stream": _coerce_bool_like(
                    request_metadata.get("provider_stream", False),
                    False,
                ),
                **(
                    {"reasoning_effort": effective_config.reasoning_effort}
                    if effective_config.reasoning_effort is not None and "reasoning_effort" not in request_metadata
                    else {}
                ),
            },
            abort_signal=abort_signal,
        )
        tool_results: list[ToolResult] = list(rehydrated_tool_results)
        graph = self._graph_for_session_metadata(session.metadata)

        last_chunk: RuntimeStreamChunk | None = None
        last_sequence = sequence
        deferred_failed_chunk: RuntimeStreamChunk | None = None
        graph_loop_error: Exception | None = None
        try:
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
                    if chunk.event.event_type == "runtime.failed":
                        deferred_failed_chunk = chunk
                        continue
                yield chunk
        except Exception as exc:
            graph_loop_error = exc

        if last_chunk is None:
            if graph_loop_error is not None:
                raise graph_loop_error
            return

        if deferred_failed_chunk is not None:
            failed_event = cast(EventEnvelope, deferred_failed_chunk.event)
            cleanup_sequence = failed_event.sequence - 1
            final_chunks, finalized_session, final_sequence = self._finalize_run_acp(
                session=deferred_failed_chunk.session,
                sequence=cleanup_sequence,
            )
            final_sequence = yield from self._persist_emitted_chunks(
                final_chunks,
                fallback_sequence=final_sequence,
            )
            end_hook_outcome = self._run_lifecycle_hooks(
                session=finalized_session,
                sequence=final_sequence,
                surface="session_end",
                payload={"session_status": finalized_session.status},
            )
            release_sequence = yield from self._persist_emitted_chunks(
                end_hook_outcome.chunks,
                fallback_sequence=end_hook_outcome.last_sequence,
            )
            if end_hook_outcome.failed_error is not None:
                hook_failed_chunk = self._lifecycle_hook_failure_chunk(
                    session=finalized_session,
                    sequence=end_hook_outcome.last_sequence,
                    surface="session_end",
                    error=end_hook_outcome.failed_error,
                )
                if hook_failed_chunk is not None:
                    persisted_hook_failed = self._persist_emitted_chunk(hook_failed_chunk)
                    yield persisted_hook_failed
                    release_sequence = persisted_hook_failed.event.sequence if persisted_hook_failed.event is not None else release_sequence
            for release_event in self._release_mcp_session_events(
                session_id=finalized_session.session.id,
                start_sequence=release_sequence + 1,
            ):
                envelope = self._persist_emitted_event(
                    session_id=release_event.session_id,
                    event_type=release_event.event_type,
                    source=release_event.source,
                    payload=release_event.payload,
                )
                release_sequence = envelope.sequence
                yield RuntimeStreamChunk(
                    kind="event",
                    session=finalized_session,
                    event=envelope,
                )
            yield RuntimeStreamChunk(
                kind="event",
                session=deferred_failed_chunk.session,
                event=self._resequence_event(failed_event, sequence=release_sequence + 1),
            )
            if graph_loop_error is not None:
                raise graph_loop_error
            return

        if graph_loop_error is not None:
            raise graph_loop_error

        if (
            last_chunk.event is not None
            and last_chunk.event.event_type == "runtime.tool_completed"
            and last_chunk.event.payload.get("permission_denied") is True
        ):
            return

        if last_chunk.session.status == "waiting":
            idle_hook_outcome = self._run_lifecycle_hooks(
                session=last_chunk.session,
                sequence=last_sequence,
                surface="session_idle",
                payload={"reason": self._waiting_reason_from_session(last_chunk.session)},
            )
            yield from self._persist_emitted_chunks(
                idle_hook_outcome.chunks,
                fallback_sequence=idle_hook_outcome.last_sequence,
            )
            if idle_hook_outcome.failed_error is not None:
                failed_chunk = self._lifecycle_hook_failure_chunk(
                    session=self._disconnect_acp_for_session_state(last_chunk.session),
                    sequence=idle_hook_outcome.last_sequence,
                    surface="session_idle",
                    error=idle_hook_outcome.failed_error,
                )
                if failed_chunk is not None:
                    yield self._persist_emitted_chunk(failed_chunk)
            return

        final_chunks, finalized_session, final_sequence = self._finalize_run_acp(
            session=last_chunk.session,
            sequence=last_sequence,
        )
        final_sequence = yield from self._persist_emitted_chunks(
            final_chunks,
            fallback_sequence=final_sequence,
        )
        end_hook_outcome = self._run_lifecycle_hooks(
            session=finalized_session,
            sequence=final_sequence,
            surface="session_end",
            payload={"session_status": finalized_session.status},
        )
        release_sequence = yield from self._persist_emitted_chunks(
            end_hook_outcome.chunks,
            fallback_sequence=end_hook_outcome.last_sequence,
        )
        if end_hook_outcome.failed_error is not None:
            failed_chunk = self._lifecycle_hook_failure_chunk(
                session=finalized_session,
                sequence=end_hook_outcome.last_sequence,
                surface="session_end",
                error=end_hook_outcome.failed_error,
            )
            if failed_chunk is not None:
                persisted_failed_chunk = self._persist_emitted_chunk(failed_chunk)
                yield persisted_failed_chunk
                release_sequence = persisted_failed_chunk.event.sequence if persisted_failed_chunk.event is not None else release_sequence
        for event in self._release_mcp_session_events(
            session_id=finalized_session.session.id,
            start_sequence=release_sequence + 1,
        ):
            envelope = self._persist_emitted_event(
                session_id=event.session_id,
                event_type=event.event_type,
                source=event.source,
                payload=event.payload,
            )
            yield RuntimeStreamChunk(kind="event", session=finalized_session, event=envelope)

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
        preserved_continuity_state: ContextProjection | None = None,
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
                policy=self._hook_execution_policy_from_metadata(session.metadata),
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
            action=outcome.action,
        )

    def _lifecycle_hook_failure_chunk(
        self,
        *,
        session: SessionState,
        sequence: int,
        surface: RuntimeHookSurface,
        error: str | None,
    ) -> RuntimeStreamChunk | None:
        if error is None:
            return None
        if self._config.hooks is None or self._config.hooks.failure_mode != "fail":
            logger.warning("%s hook failed for %s: %s", surface, session.session.id, error)
            return None
        return self._failed_chunk(session=session, sequence=sequence + 1, error=error)

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
                policy=self._hook_execution_policy_from_metadata(session.metadata),
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
            action=outcome.action,
        )

    def _hook_execution_policy_from_metadata(
        self,
        metadata: dict[str, object] | None,
    ) -> HookExecutionPolicy:
        mode = self._runtime_mode_for_policy_metadata(metadata)
        read_only = self._effective_runtime_read_only_for_policy_metadata(metadata)
        return HookExecutionPolicy(mode=mode, read_only=read_only)

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
        failure_payload = self._with_runtime_failure_details(failure_payload)
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

    @staticmethod
    def _with_runtime_failure_details(payload: dict[str, object]) -> dict[str, object]:
        normalized = dict(payload)
        error = normalized.get("error")
        if not isinstance(error, str) or not error:
            return normalized
        summary = normalized.get("error_summary")
        if not isinstance(summary, str) or not summary:
            summary = VoidCodeRuntime._format_runtime_error_summary(error)
            normalized["error_summary"] = summary
        guidance = normalized.get("retry_guidance")
        if not isinstance(guidance, str) or not guidance:
            retry_guidance = VoidCodeRuntime._retry_guidance_for_runtime_failure(normalized)
            if retry_guidance is not None:
                normalized["retry_guidance"] = retry_guidance
        if "error_details" not in normalized:
            details: dict[str, object] = {"message": error, "summary": summary}
            if isinstance(normalized.get("provider_error_kind"), str):
                details["provider_error_kind"] = normalized["provider_error_kind"]
            if isinstance(normalized.get("provider_error_details"), dict):
                details["provider_error_details"] = normalized["provider_error_details"]
            if normalized.get("cancelled") is True:
                details["cancelled"] = True
            normalized["error_details"] = details
        return normalized

    @staticmethod
    def _retry_guidance_for_runtime_failure(payload: dict[str, object]) -> str | None:
        provider_error_kind = payload.get("provider_error_kind")
        if isinstance(provider_error_kind, str) and provider_error_kind:
            guidance = guidance_for_provider_error_kind(cast(ProviderErrorKind, provider_error_kind))
            if guidance:
                return guidance
        if payload.get("cancelled") is True:
            return "Retry the request if you still want to continue this run."
        if payload.get("kind") == "interrupted":
            return "Retry the request if you want to resume execution after the interruption."
        return None

    @staticmethod
    def _format_runtime_error_summary(error: str) -> str:
        cleaned = error.removeprefix("Error: ").strip()
        if not cleaned:
            return error
        for prefix in ("Runtime failed:", "runtime failed:"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()
                break
        return cleaned or error

    def _persist_response(self, *, request: RuntimeRequest, response: RuntimeResponse) -> None:
        runtime_state = response.session.metadata.get("runtime_state")
        if response.session.status in {"completed", "failed"} and isinstance(runtime_state, dict) and "pending_tool_intent" in runtime_state:
            cleaned_state = dict(cast(dict[str, object], runtime_state))
            cleaned_state.pop("pending_tool_intent", None)
            response = RuntimeResponse(
                session=SessionState(
                    session=response.session.session,
                    status=response.session.status,
                    turn=response.session.turn,
                    metadata={**response.session.metadata, "runtime_state": cleaned_state},
                ),
                events=response.events,
                output=response.output,
            )
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
        # ``completed``/``failed`` is per-TURN, not per-session: overlapping
        # runs can share a session_id (follow-ups, background tasks reusing the
        # default session, explicit same-session streams). Only the last active
        # run may seal the terminal status; an older-finishing run must leave
        # the row ``interrupted`` so the still-active newer run keeps appending.
        seal_terminal_status = (
            ACTIVE_SESSION_REGISTRY.active_run_count(
                workspace=self._workspace,
                session_id=response.session.session.id,
            )
            <= 1
        )
        # Drain order at the session seal: every event this run produced was
        # already appended incrementally (``append_session_events``), so
        # ``save_run`` only seals the row snapshot (status, output, metadata,
        # resume checkpoint) and never regresses ``last_event_sequence``. The
        # seal must happen-before ``_run_with_persistence`` unregisters the
        # active run (its ``finally``), so the guarded window — a late event
        # arriving after the seal — is exactly the window in which
        # ``_sealed_session_status`` returns a sealed status and the event is
        # rejected/dropped instead of applied.
        self._session_store.save_run(
            workspace=self._workspace,
            request=request,
            response=response,
            seal_terminal_status=seal_terminal_status,
        )

    def _resolve_permission(
        self,
        *,
        session: SessionState,
        tool: ToolDefinition,
        tool_instance: Tool,
        tool_call: ToolCall,
        sequence: int,
        permission_policy: PermissionPolicy,
    ) -> _PermissionOutcome:
        effective_permission = self._effective_runtime_config_from_metadata(session.metadata).permission
        eval_result = self._permission_engine.evaluate(
            tool=tool,
            tool_instance=tool_instance,
            tool_call=tool_call,
            permission_rules=effective_permission.rules,
            permission_config=effective_permission,
        )

        path_scope = eval_result.path_scope
        canonical_path = eval_result.canonical_path
        operation_class = eval_result.operation_class
        rule_decision = eval_result.rule_decision
        matched_rule = eval_result.matched_rule
        policy_surface = eval_result.policy_surface
        external_decision = eval_result.external_decision

        # Referenced via extracted run-loop collaborator.
        permission = resolve_permission(
            tool,
            tool_call,
            policy=permission_policy,
            path_scope=path_scope,
            operation_class=operation_class,
            canonical_path=canonical_path,
            matched_rule=matched_rule,
            policy_surface=policy_surface,
            external_decision=external_decision,
            rule_decision=rule_decision,
            owner_session_id=session.session.id,
            owner_parent_session_id=session.session.parent_id,
            delegated_task_id=(
                cast(str, session.metadata["background_task_id"]) if isinstance(session.metadata.get("background_task_id"), str) else None
            ),
            runtime_mode=runtime_mode_from_metadata(session.metadata),
        )

        if path_scope == "workspace" and tool.read_only and operation_class == "read":
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
        request_payload: dict[str, object] = {
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
        }
        if (
            pending.policy_surface
            in {
                "external_directory_read",
                "external_directory_write",
                "permission.rules",
                "shell_policy",
            }
            or pending.path_scope is not None
            or pending.operation_class is not None
        ):
            request_payload.update(
                {
                    "path_scope": pending.path_scope,
                    "operation_class": pending.operation_class,
                    "canonical_path": pending.canonical_path,
                    "matched_rule": pending.matched_rule,
                    "policy_surface": pending.policy_surface,
                }
            )
        request_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type="runtime.approval_requested",
            source="runtime",
            payload=request_payload,
        )
        pending = replace(pending, request_event_sequence=request_event.sequence)
        return _PermissionOutcome(
            chunks=(RuntimeStreamChunk(kind="event", session=waiting_session, event=request_event),),
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
        resolution_payload: dict[str, object] = {
            "request_id": pending.request_id,
            "decision": decision,
        }
        if (
            pending.policy_surface
            in {
                "external_directory_read",
                "external_directory_write",
                "permission.rules",
                "shell_policy",
            }
            or pending.path_scope is not None
            or pending.operation_class is not None
        ):
            resolution_payload.update(
                {
                    "path_scope": pending.path_scope,
                    "operation_class": pending.operation_class,
                    "canonical_path": pending.canonical_path,
                    "matched_rule": pending.matched_rule,
                    "policy_surface": pending.policy_surface,
                }
            )
        resolution_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type="runtime.approval_resolved",
            source="runtime",
            payload=resolution_payload,
        )
        if decision == "deny":
            return _PermissionOutcome(
                chunks=(
                    RuntimeStreamChunk(
                        kind="event",
                        session=self._session_with_plan_state(session, status="in_progress"),
                        event=resolution_event,
                    ),
                ),
                last_sequence=sequence,
                denied=True,
                denied_approval=pending,
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

    def tool_effectiveness_report(self) -> ToolEffectivenessReport:
        return self._session_store.tool_effectiveness_report(workspace=self._workspace)

    def _require_memory_enabled(self) -> None:
        if not self._config.memory.enabled:
            raise RuntimeError("workspace memory is disabled by runtime config")

    def add_memory(
        self,
        *,
        content: str,
        kind: MemoryKind = "project",
        tags: tuple[str, ...] = (),
        source_session_id: str | None = None,
    ) -> MemoryRecord:
        self._require_memory_enabled()
        record = self._session_store.add_memory(
            workspace=self._workspace,
            content=content,
            kind=kind,
            tags=tags,
            source_session_id=source_session_id,
        )
        return record

    def list_memories(self, *, include_deleted: bool = False) -> tuple[MemoryRecord, ...]:
        self._require_memory_enabled()
        return self._session_store.list_memories(
            workspace=self._workspace,
            include_deleted=include_deleted,
        )

    def search_memories(self, *, query: str) -> tuple[MemorySearchResult, ...]:
        self._require_memory_enabled()
        manager_state = build_memory_manager(
            self._config.memory,
            workspace=self._workspace,
        ).current_state()
        if not (manager_state.semantic_search_available or manager_state.keyword_search_available):
            raise RuntimeError("workspace memory search requires semantic search, but semantic search is unavailable")
        return self._session_store.search_memories(workspace=self._workspace, query=query)

    def workspace_memory_prompt_context(self, memory_config: MemoryConfig | None = None) -> str:
        config = memory_config or self._config.memory
        if not config.enabled or not config.recall.enabled:
            return ""
        memories = self._session_store.list_memories(workspace=self._workspace)
        return _render_workspace_memory_context(
            memories[: config.recall.limit],
            max_chars=config.recall.max_chars,
        )

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        self._require_memory_enabled()
        return self._session_store.get_memory(workspace=self._workspace, memory_id=memory_id)

    def delete_memory(self, memory_id: str) -> MemoryRecord:
        self._require_memory_enabled()
        return self._session_store.delete_memory(workspace=self._workspace, memory_id=memory_id)

    def memory_status(self) -> RuntimeMemoryStatusSnapshot:
        diagnostics = self._session_store.storage_diagnostics(workspace=self._workspace)
        memories = self._session_store.list_memories(
            workspace=self._workspace,
            include_deleted=True,
        )
        deleted_count = sum(1 for memory in memories if memory.status == "deleted")
        manager_state = build_memory_manager(
            self._config.memory,
            workspace=self._workspace,
        ).current_state()
        return RuntimeMemoryStatusSnapshot(
            workspace_id=str(self._workspace),
            database_path=str(diagnostics["database_path"]),
            enabled=self._config.memory.enabled,
            scope=self._config.memory.scope,
            active_count=len(memories) - deleted_count,
            deleted_count=deleted_count,
            total_count=len(memories),
            recall_enabled=self._config.memory.recall.enabled,
            semantic_search=self._config.memory.semantic_search,
            sqlite_vec=self._config.memory.sqlite_vec.enabled,
            keyword_search_available=manager_state.keyword_search_available,
            semantic_search_available=manager_state.semantic_search_available,
            sqlite_vec_status=manager_state.sqlite_vec.status,
            sqlite_vec_detail=manager_state.sqlite_vec.detail,
        )

    def memory_event_payload(
        self,
        *,
        action: Literal["added", "deleted", "searched", "status_checked"],
        memory: MemoryRecord | None = None,
        query: str | None = None,
        result_count: int | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "action": action,
            "workspace_id": str(self._workspace),
        }
        if memory is not None:
            payload.update(
                {
                    "memory_id": memory.id,
                    "kind": memory.kind,
                    "status": memory.status,
                    "tag_count": len(memory.tags),
                }
            )
        if query is not None:
            payload["query"] = query
        if result_count is not None:
            payload["result_count"] = result_count
        return payload

    def memory_event_type(
        self,
        action: Literal["added", "deleted", "searched", "status_checked"],
    ) -> str:
        if action == "added":
            return RUNTIME_MEMORY_ADDED
        if action == "deleted":
            return RUNTIME_MEMORY_DELETED
        if action == "searched":
            return RUNTIME_MEMORY_SEARCHED
        return RUNTIME_MEMORY_STATUS_CHECKED

    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState:
        validated_request = self._validated_request(request)
        return self._background_task_supervisor.start_background_task(validated_request)

    def load_background_task(self, task_id: str) -> BackgroundTaskState:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        validate_background_task_id(task_id)
        task = self._session_store.load_background_task(workspace=self._workspace, task_id=task_id)
        return self._background_task_supervisor.task_with_observability(task)

    def load_background_task_result(
        self,
        task_id: str,
        *,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskResult:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        return self._background_task_supervisor.load_background_task_result(
            task_id,
            emit_result_read_hook=emit_result_read_hook,
        )

    def load_background_task_result_by_child_session(
        self,
        *,
        child_session_id: str,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskResult | None:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        validated_child_session_id = validate_session_reference_id(
            child_session_id,
            field_name="child_session_id",
        )
        task = self._session_store.load_background_task_by_child_session(
            workspace=self._workspace,
            child_session_id=validated_child_session_id,
        )
        if task is None:
            return None
        return self._background_task_supervisor.load_background_task_result(
            task.task.id,
            emit_result_read_hook=emit_result_read_hook,
        )

    def list_background_tasks(self) -> tuple[StoredBackgroundTaskSummary, ...]:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        return self._background_task_supervisor.summaries_with_observability(self._session_store.list_background_tasks(workspace=self._workspace))

    def list_background_tasks_by_parent_session(self, *, parent_session_id: str) -> tuple[StoredBackgroundTaskSummary, ...]:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        validated_parent_session_id = validate_session_reference_id(
            parent_session_id,
            field_name="parent_session_id",
        )
        return self._background_task_supervisor.summaries_with_observability(
            self._session_store.list_background_tasks_by_parent_session(
                workspace=self._workspace,
                parent_session_id=validated_parent_session_id,
            )
        )

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        return self._background_task_supervisor.cancel_background_task(task_id)

    def retry_background_task(self, task_id: str) -> BackgroundTaskState:
        return self._background_task_supervisor.retry_background_task(task_id)

    def start_continuation_loop(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        completion_promise: str = "DONE",
        max_iterations: int = 100,
        intensive: bool = False,
        strategy: ContinuationLoopStrategy = "continue",
    ) -> ContinuationLoopState:
        if not prompt.strip():
            raise ValueError("continuation loop prompt must be a non-empty string")
        if not completion_promise.strip():
            raise ValueError("continuation loop completion_promise must be non-empty")
        if max_iterations < 1:
            raise ValueError("continuation loop max_iterations must be positive")
        if session_id is not None:
            session_id = validate_session_reference_id(session_id, field_name="session_id")
        loop_id = f"loop-{uuid4().hex}"
        loop = ContinuationLoopState(
            loop=ContinuationLoopRef(id=loop_id),
            prompt=prompt,
            session_id=session_id,
            completion_promise=completion_promise,
            max_iterations=max_iterations,
            intensive=intensive,
            strategy=strategy,
            verification_status="pending" if intensive else "not_required",
        )
        self._session_store.create_continuation_loop(workspace=self._workspace, loop=loop)
        return self._session_store.load_continuation_loop(
            workspace=self._workspace,
            loop_id=loop_id,
        )

    def load_continuation_loop(self, loop_id: str) -> ContinuationLoopState:
        validate_continuation_loop_id(loop_id)
        return self._session_store.load_continuation_loop(
            workspace=self._workspace,
            loop_id=loop_id,
        )

    def list_continuation_loops(self) -> tuple[StoredContinuationLoopSummary, ...]:
        return self._session_store.list_continuation_loops(workspace=self._workspace)

    def record_continuation_loop_iteration(self, loop_id: str) -> ContinuationLoopState:
        validate_continuation_loop_id(loop_id)
        return self._session_store.record_continuation_loop_iteration(
            workspace=self._workspace,
            loop_id=loop_id,
        )

    def mark_continuation_loop_verification_pending(self, loop_id: str) -> ContinuationLoopState:
        validate_continuation_loop_id(loop_id)
        return self._session_store.mark_continuation_loop_verification_pending(
            workspace=self._workspace,
            loop_id=loop_id,
        )

    def mark_continuation_loop_verified(self, loop_id: str) -> ContinuationLoopState:
        validate_continuation_loop_id(loop_id)
        return self._session_store.mark_continuation_loop_verified(
            workspace=self._workspace,
            loop_id=loop_id,
        )

    def mark_continuation_loop_verification_failed(
        self,
        loop_id: str,
        *,
        error: str | None = None,
    ) -> ContinuationLoopState:
        validate_continuation_loop_id(loop_id)
        return self._session_store.mark_continuation_loop_verification_failed(
            workspace=self._workspace,
            loop_id=loop_id,
            error=error,
        )

    def mark_continuation_loop_terminal(
        self,
        loop_id: str,
        *,
        status: ContinuationLoopStatus,
        error: str | None = None,
    ) -> ContinuationLoopState:
        validate_continuation_loop_id(loop_id)
        return self._session_store.mark_continuation_loop_terminal(
            workspace=self._workspace,
            loop_id=loop_id,
            status=status,
            error=error,
        )

    def cancel_continuation_loop(self, loop_id: str) -> ContinuationLoopState:
        validate_continuation_loop_id(loop_id)
        return self._session_store.cancel_continuation_loop(
            workspace=self._workspace,
            loop_id=loop_id,
        )

    def session_result(self, *, session_id: str) -> RuntimeSessionResult:
        delegated_task = self._session_store.load_background_task_by_child_session(
            workspace=self._workspace,
            child_session_id=session_id,
        )
        if delegated_task is not None:
            self._session_store.stop_background_task_idle_reminder(
                workspace=self._workspace,
                task_id=delegated_task.task.id,
                stop_condition="result_read",
            )
        _ = self._load_session_result(session_id=session_id)
        self._background_task_supervisor.reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
        return self._load_session_result(session_id=session_id)

    def replay_session(self, *, session_id: str) -> RuntimeResponse:
        """Read the persisted session transcript without resume semantics."""
        validate_session_id(session_id)
        response = self._load_stored_response(session_id=session_id)
        projected_metadata = session_metadata_for_replay(response.session.metadata)
        return RuntimeResponse(
            session=SessionState(
                session=response.session.session,
                status=response.session.status,
                turn=response.session.turn,
                metadata=projected_metadata,
            ),
            events=self._events_with_runtime_policy_projection(
                response.events,
                metadata=projected_metadata,
            ),
            output=response.output,
        )

    def revert_session(self, *, session_id: str, sequence: int) -> RuntimeSessionRevertMarker:
        validate_session_id(session_id)
        marker = self._session_store.revert_session(
            workspace=self._workspace,
            session_id=session_id,
            sequence=sequence,
        )
        self._validate_session_workspace(
            self._session_store.load_session_result(
                workspace=self._workspace,
                session_id=session_id,
            ).session,
            session_id=session_id,
        )
        return marker

    def undo_session(self, *, session_id: str) -> RuntimeSessionRevertMarker:
        validate_session_id(session_id)
        marker = self._session_store.undo_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        self._validate_session_workspace(
            self._session_store.load_session_result(
                workspace=self._workspace,
                session_id=session_id,
            ).session,
            session_id=session_id,
        )
        return marker

    def unrevert_session(self, *, session_id: str) -> RuntimeSessionRevertMarker | None:
        validate_session_id(session_id)
        marker = self._session_store.unrevert_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        self._validate_session_workspace(
            self._session_store.load_session_result(
                workspace=self._workspace,
                session_id=session_id,
            ).session,
            session_id=session_id,
        )
        return marker

    def _load_session_result(self, *, session_id: str) -> RuntimeSessionResult:
        validate_session_id(session_id)
        result = self._session_store.load_session_result(
            workspace=self._workspace,
            session_id=session_id,
        )
        self._validate_session_workspace(result.session, session_id=session_id)
        raw_snapshot = result.session.metadata.get("agent_capability_snapshot")
        if raw_snapshot is None:
            raise ValueError("persisted session requires agent_capability_snapshot")
        if not isinstance(raw_snapshot, dict):
            raise ValueError("persisted agent_capability_snapshot must be an object")
        validate_agent_capability_snapshot(cast(dict[str, object], raw_snapshot))
        return result

    def resolve_tool_output_artifact(
        self,
        *,
        session_id: str,
        artifact_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, object]:
        """Resolve spilled tool output artifact metadata for a session."""

        validate_session_id(session_id)
        result = self._session_store.load_session_result(
            workspace=self._workspace,
            session_id=session_id,
        )
        self._validate_session_workspace(result.session, session_id=session_id)
        artifact = resolve_tool_output_artifact_metadata(
            result.transcript,
            artifact_id=artifact_id,
            tool_call_id=tool_call_id,
        )
        if artifact is None:
            return {
                "status": "artifact_not_found",
                "artifact_missing": True,
                "artifact_id": artifact_id,
                "tool_call_id": tool_call_id,
                "session_id": session_id,
            }
        read_result = read_tool_output_artifact(artifact, offset=0, limit=0)
        status = read_result.get("status")
        return {
            **artifact,
            "status": status if isinstance(status, str) else artifact.get("status", "unknown"),
            "artifact_missing": bool(read_result.get("artifact_missing")),
        }

    def read_tool_output_artifact(
        self,
        *,
        session_id: str,
        artifact_id: str | None = None,
        tool_call_id: str | None = None,
        offset: int = 0,
        limit: int = 2000,
    ) -> dict[str, object]:
        """Read a bounded slice from a spilled tool output artifact."""

        artifact = self.resolve_tool_output_artifact(
            session_id=session_id,
            artifact_id=artifact_id,
            tool_call_id=tool_call_id,
        )
        if artifact.get("status") == "artifact_not_found":
            return artifact
        return read_tool_output_artifact(artifact, offset=offset, limit=limit)

    def search_tool_output_artifact(
        self,
        *,
        session_id: str,
        pattern: str,
        artifact_id: str | None = None,
        tool_call_id: str | None = None,
        case_sensitive: bool = False,
        limit: int = 100,
    ) -> dict[str, object]:
        """Search a spilled tool output artifact by artifact id or tool call id."""

        artifact = self.resolve_tool_output_artifact(
            session_id=session_id,
            artifact_id=artifact_id,
            tool_call_id=tool_call_id,
        )
        if artifact.get("status") == "artifact_not_found":
            return artifact
        return search_tool_output_artifact(
            artifact,
            pattern=pattern,
            case_sensitive=case_sensitive,
            limit=limit,
        )

    def session_debug_snapshot(self, *, session_id: str) -> RuntimeSessionDebugSnapshot:
        validate_session_id(session_id)
        active = self._is_active_session_id(session_id)
        active_metadata = self._active_session_metadata(session_id) if active else None
        try:
            result = self._load_session_result(session_id=session_id)
        except (AttributeError, UnknownSessionError, ValueError):
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
        checkpoint_kind = (
            cast(str, resume_checkpoint.get("kind"))
            if isinstance(resume_checkpoint, dict) and isinstance(resume_checkpoint.get("kind"), str)
            else None
        )
        terminal = result.session.status in {"completed", "failed"}
        resumable = (
            result.session.status == "waiting"
            or result.session.status == "interrupted"
            or (result.session.status == "failed" and checkpoint_kind == "provider_failure_retryable")
        )
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
                (event for event in reversed(result.transcript) if event.event_type == "runtime.failed"),
                None,
            )
        )
        last_tool = self._last_tool_summary(result)
        provider_context = self._provider_context_debug_snapshot(result)
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
            resumable=resumable,
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
            resume_checkpoint_kind=checkpoint_kind,
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
                    path_scope=pending_approval.path_scope,
                    operation_class=pending_approval.operation_class,
                    canonical_path=pending_approval.canonical_path,
                    matched_rule=pending_approval.matched_rule,
                    policy_surface=pending_approval.policy_surface,
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
            revert_marker=result.revert_marker,
            last_event_sequence=result.last_event_sequence,
            last_relevant_event=last_relevant_event,
            last_failure_event=last_failure_event,
            failure=failure,
            last_tool=last_tool,
            provider_context=provider_context,
            hook_presets=self._debug_hook_preset_snapshot(result.session.metadata),
            suggested_operator_action=suggested_operator_action,
            operator_guidance=operator_guidance,
        )

    def list_notifications(self) -> tuple[RuntimeNotification, ...]:
        notifications = self._session_store.list_notifications(workspace=self._workspace)
        return tuple(notification for notification in notifications if self._session_belongs_to_workspace(notification.session.id))

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

    def storage_diagnostics(self) -> dict[str, object]:
        return self._session_store.storage_diagnostics(workspace=self._workspace)

    def export_session_bundle(
        self,
        *,
        session_id: str,
        options: SessionBundleOptions | None = None,
    ) -> SessionBundle:
        validate_session_id(session_id)
        _ = self._load_session_result(session_id=session_id)
        return build_session_bundle(
            session_store=self._session_store,
            workspace=self._workspace,
            session_id=session_id,
            options=options or SessionBundleOptions(),
            storage_diagnostics=self.storage_diagnostics(),
            config_summary=self._session_bundle_config_summary(session_id=session_id),
            provider_summary=self._session_bundle_provider_summary(session_id=session_id),
        )

    def export_session_bundle_file(
        self,
        *,
        session_id: str,
        output_path: Path,
        options: SessionBundleOptions | None = None,
        fmt: str | None = None,
    ) -> SessionBundle:
        bundle = self.export_session_bundle(session_id=session_id, options=options)
        if fmt is not None and fmt not in {"zip", "json"}:
            raise ValueError(f"unsupported session bundle format: {fmt!r}")
        _ = write_session_bundle(
            bundle,
            path=output_path,
            fmt=fmt,
        )
        return bundle

    def import_session_bundle_file(
        self,
        *,
        bundle_path: Path,
        dry_run: bool = False,
    ) -> SessionBundleImportResult:
        bundle = read_session_bundle(bundle_path)
        return apply_session_bundle(
            bundle,
            session_store=self._session_store,
            workspace=self._workspace,
            dry_run=dry_run,
        )

    def _session_bundle_config_summary(self, *, session_id: str) -> dict[str, object]:
        effective_config = self.effective_runtime_config(session_id=session_id)
        return {
            "session_id": session_id,
            "approval_mode": effective_config.approval_mode,
            "model": effective_config.model,
            "fallback_models": (list(effective_config.provider_fallback.fallback_models) if effective_config.provider_fallback is not None else []),
            "max_steps": effective_config.max_steps,
            "reasoning_effort": effective_config.reasoning_effort,
            "agent": serialize_runtime_agent_config(effective_config.agent),
            "resolved_provider": resolved_provider_snapshot(effective_config.resolved_provider),
        }

    def _session_bundle_provider_summary(self, *, session_id: str) -> dict[str, object]:
        readiness = self.provider_readiness(session_id=session_id)
        return {
            "provider": readiness.provider,
            "model": readiness.model,
            "configured": readiness.configured,
            "ok": readiness.ok,
            "status": readiness.status,
            "guidance": readiness.guidance,
            "auth_present": readiness.auth_present,
            "streaming_configured": readiness.streaming_configured,
            "streaming_supported": readiness.streaming_supported,
            "context_window": readiness.context_window,
            "max_output_tokens": readiness.max_output_tokens,
            "fallback_chain": list(readiness.fallback_chain),
        }

    def prune_runtime_storage(
        self,
        *,
        keep_sessions: int | None = None,
        keep_background_tasks: int | None = None,
        older_than: int | None = None,
    ) -> dict[str, int]:
        return self._session_store.prune_runtime_storage(
            workspace=self._workspace,
            keep_sessions=keep_sessions,
            keep_background_tasks=keep_background_tasks,
            older_than=older_than,
        )

    def reset_runtime_storage(self) -> dict[str, object]:
        return self._session_store.reset_runtime_storage(workspace=self._workspace)

    def effective_runtime_config(self, *, session_id: str | None = None) -> EffectiveRuntimeConfig:
        if session_id is None:
            return self._effective_runtime_config_from_metadata(None)
        validate_session_id(session_id)
        response = self._load_stored_response(session_id=session_id)
        return self._effective_runtime_config_from_metadata(response.session.metadata)

    def effective_category_model_config(self, *, session_id: str | None = None) -> dict[str, object]:
        categories, agents, base_model, _base_provider_fallback = self._display_routing_config(session_id=session_id)
        payload: dict[str, object] = {}
        for category in supported_subagent_categories():
            route = runtime_subagent_route_from_metadata(
                {"delegation": {"mode": "background", "category": category}},
                callable_subagent_presets=self._agent_registry.executable_subagent_ids(),
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
                "fallback_models": (list(category_config.fallback_models) if category_config is not None else []),
                "effective_model": model,
                "selected_preset": route.selected_preset,
            }
        return payload

    def effective_agent_model_config(self, *, session_id: str | None = None) -> dict[str, object]:
        _categories, agents, base_model, base_provider_fallback = self._display_routing_config(session_id=session_id)
        payload: dict[str, object] = {}
        for manifest in self._agent_registry.list_manifests():
            preset_agent = agents.get(manifest.id)
            model = preset_agent.model if preset_agent is not None else manifest.model_preference
            if model is None:
                model = base_model
            provider_fallback = self._provider_fallback_for_agent_selection(
                model=model,
                preset_agent=preset_agent,
                base_provider_fallback=base_provider_fallback,
            )
            fallback_models = list(provider_fallback.fallback_models) if provider_fallback is not None else []
            payload[manifest.id] = {
                "model": preset_agent.model if preset_agent is not None else None,
                "fallback_models": fallback_models,
                "effective_model": model,
                "effective_fallback_models": fallback_models,
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
            raise ValueError("persisted session metadata must include runtime_config object")
        payload = cast(dict[str, object], runtime_config)
        materialized = parse_persisted_runtime_config(payload)
        base_model = materialized.model
        base_provider_fallback = materialized.provider_fallback
        categories = parse_runtime_categories_payload(
            payload.get("categories"),
            source="persisted runtime_config.categories",
        )
        agents = parse_runtime_agents_payload(
            payload.get("agents"),
            source="persisted runtime_config.agents",
            hooks=self._config.hooks,
            agent_registry=self._agent_registry,
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
        return self._provider_catalog_query.models(provider_name)

    def provider_model_catalog(self, provider_name: str) -> dict[str, object] | None:
        return self._provider_catalog_query.catalog_payload(provider_name)

    def _hydrate_provider_model_catalog_cache(self) -> None:
        self._provider_catalog_cache.hydrate()

    def _persist_provider_model_catalog_cache(self) -> None:
        self._provider_catalog_cache.persist()

    def _metadata_for_provider_model(self, provider_name: str, model_name: str) -> ProviderModelMetadata | None:
        return self._provider_catalog_query.metadata_for_model(provider_name, model_name)

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
        return self._provider_summary_projector.project_all(
            self._model_provider_registry.providers,
            current_provider=self._current_provider_name(),
            label_for=self._provider_label,
            is_configured=self._provider_is_configured,
        )

    def provider_models_result(self, provider_name: str) -> ProviderModelsResult:
        configured = self._provider_is_configured(provider_name)
        catalog = self.provider_model_catalog(provider_name)
        if configured and catalog is None:
            _ = self.refresh_provider_models(provider_name)
        return self._provider_catalog_query.models_result(
            provider_name,
            configured=configured,
        )

    def provider_readiness(self, *, session_id: str | None = None) -> ProviderReadinessResult:
        effective_config = self.effective_runtime_config(session_id=session_id)
        return self._provider_readiness_for_effective_config(effective_config)

    def _provider_readiness_for_effective_config(self, effective_config: EffectiveRuntimeConfig) -> ProviderReadinessResult:
        active_target = effective_config.resolved_provider.active_target
        provider_name = active_target.selection.provider
        model_name = active_target.selection.model
        fallback_chain = tuple(_provider_target_label(target) for target in effective_config.resolved_provider.target_chain.all_targets)
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
        return RuntimeProviderReadinessProjector.project(
            ProviderReadinessFacts(
                provider=provider_name,
                model=model_name,
                configured=configured,
                auth_present=auth_present,
                auth_failure_kind=auth_failure_kind,
                auth_message=auth_message,
                streaming_configured=streaming_configured,
                streaming_supported=streaming_supported,
                context_window=context_window,
                max_output_tokens=max_output_tokens,
                fallback_chain=fallback_chain,
                reasoning_controls=self._reasoning_controls_diagnostic(
                    effective_config=effective_config,
                    provider_name=provider_name,
                    model_name=model_name,
                ),
            )
        )

    def _reasoning_controls_diagnostic(
        self,
        *,
        effective_config: EffectiveRuntimeConfig,
        provider_name: str | None,
        model_name: str | None,
    ) -> dict[str, object]:
        effort = effective_config.reasoning_effort
        payload: dict[str, object] = {
            "reasoning_effort_requested": effort is not None,
            "reasoning_effort": effort,
            "status": "not_requested" if effort is None else "unknown",
            "forwarded": False,
        }
        if provider_name is None or model_name is None:
            payload["status"] = "unavailable"
            payload["reason"] = "provider_model_unresolved"
            return payload
        supports = provider_supports_reasoning_effort(provider_name, model_name)
        payload["supports_reasoning_effort"] = supports
        if effort is None:
            return payload
        if supports is False:
            payload["status"] = "unsupported"
            payload["reason"] = "model_metadata_disallows_reasoning_effort"
            return payload
        payload["status"] = "forwarded"
        payload["forwarded"] = True
        payload["provider_parameter"] = "reasoning_effort"
        return payload

    def _reasoning_controls_diagnostic_for_config(
        self,
        effective_config: EffectiveRuntimeConfig,
    ) -> dict[str, object] | None:
        if effective_config.execution_engine != "provider":
            return None
        active_target = effective_config.resolved_provider.active_target.selection
        provider_name = active_target.provider
        model_name = active_target.model
        diagnostic = self._reasoning_controls_diagnostic(
            effective_config=effective_config,
            provider_name=provider_name,
            model_name=model_name,
        )
        if diagnostic.get("reasoning_effort_requested") is not True:
            return None
        return {
            "severity": "info",
            "category": "reasoning_controls",
            "provider": provider_name,
            "model": model_name,
            **diagnostic,
        }

    def inspect_provider(self, provider_name: str) -> ProviderInspectResult:
        if not provider_name or "/" in provider_name:
            raise ValueError("provider_name must be a non-empty provider id without '/'")
        summary = next(
            (provider for provider in self.list_provider_summaries() if provider.name == provider_name),
            self._provider_summary_projector.project_one(
                provider_name,
                current_provider=self._current_provider_name(),
                label_for=self._provider_label,
                is_configured=self._provider_is_configured,
            ),
        )
        validation = self.validate_provider_credentials(provider_name)
        models = self.provider_models_result(provider_name)
        current_model = self._provider_model.selection.model if self._provider_model.selection.provider == provider_name else None
        current_metadata = self._metadata_for_provider_model(provider_name, current_model) if current_model is not None else None
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
        configured = self._provider_is_configured(provider_name)
        if not configured:
            return RuntimeProviderValidationProjector.project(
                ProviderValidationFacts(
                    provider=provider_name,
                    configured=False,
                    auth_present=None,
                    models=self.provider_models_result(provider_name),
                )
            )
        auth_present, auth_failure_kind, auth_message = self._provider_auth_presence(provider_name)
        if auth_present is False:
            return RuntimeProviderValidationProjector.project(
                ProviderValidationFacts(
                    provider=provider_name,
                    configured=True,
                    auth_present=auth_present,
                    auth_failure_kind=auth_failure_kind,
                    auth_message=auth_message,
                )
            )
        _ = self.refresh_provider_models(provider_name)
        result = self.provider_models_result(provider_name)
        return RuntimeProviderValidationProjector.project(
            ProviderValidationFacts(
                provider=provider_name,
                configured=True,
                auth_present=auth_present,
                auth_failure_kind=auth_failure_kind,
                auth_message=auth_message,
                models=result,
            )
        )

    def _provider_auth_presence(self, provider_name: str | None) -> tuple[bool | None, str | None, str | None]:
        return self._provider_auth_inspector.presence(provider_name).as_tuple()

    @staticmethod
    def _optional_positive_int(value: object) -> int | None:
        return optional_positive_int(value)

    @staticmethod
    def _optional_bool(value: object) -> bool | None:
        return optional_bool(value)

    @staticmethod
    def _optional_positive_float(value: object) -> float | None:
        return optional_positive_float(value)

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return optional_string(value)

    @staticmethod
    def _optional_string_tuple(value: object) -> tuple[str, ...] | None:
        return optional_string_tuple(value)

    @staticmethod
    def _tool_feedback_mode(
        value: object,
    ) -> Literal["standard", "synthetic_user_message"] | None:
        return tool_feedback_mode(value)

    @staticmethod
    def _contract_metadata_from_payload(payload: dict[str, object]) -> ProviderModelMetadata:
        return contract_metadata_from_payload(payload)

    def list_agent_summaries(self) -> tuple[AgentSummary, ...]:
        summaries: list[AgentSummary] = []
        configured_agent = self._config.agent
        for manifest in self._agent_registry.list_manifests():
            if manifest.mode != "primary":
                continue

            agent_config = configured_agent if configured_agent is not None and configured_agent.preset == manifest.id else None
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
            configured = agent_config is not None or self._config.model is not None or provider_fallback is not None
            summaries.append(
                AgentSummary(
                    id=manifest.id,
                    label=manifest.name,
                    description=manifest.description,
                    mode=manifest.mode,
                    selectable=manifest.id in self._agent_registry.executable_primary_ids(),
                    configured=configured,
                    source_scope=manifest.source_scope,
                    source_path=manifest.source_path,
                    execution_engine=execution_engine,
                    model=resolved_model,
                    model_label=active_selection.model,
                    model_source=model_source,
                    provider=active_selection.provider,
                    fallback_chain=tuple(_provider_target_label(target) for target in resolved_provider.target_chain.all_targets),
                )
            )
        return tuple(summaries)

    def list_skill_summaries(self) -> tuple[SkillSummary, ...]:
        summaries: list[SkillSummary] = []
        for skill in sorted(self._skill_registry.all(), key=lambda item: item.name):
            summaries.append(
                SkillSummary(
                    name=skill.name,
                    description=skill.description,
                    origin=skill.origin,
                    source_path=str(skill.entry_path),
                )
            )
        return tuple(summaries)

    def list_command_summaries(self) -> tuple[CommandSummary, ...]:
        registry = load_command_registry(workspace=self._workspace)
        commands = registry.list()
        summaries: list[CommandSummary] = []
        for command in commands:
            summaries.append(self._command_summary(command))
        return tuple(summaries)

    @staticmethod
    def _command_summary(command: CommandDefinition) -> CommandSummary:
        return CommandSummary(
            name=command.name,
            description=command.description,
            source=command.source,
            enabled=command.enabled,
            hidden=command.hidden,
            agent=command.agent,
            model=command.model,
            subtask=command.subtask,
            path=(str(command.path) if command.path is not None else None),
        )

    def current_status(self) -> RuntimeStatusSnapshot:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
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
        mcp_configured_servers = mcp_state.configuration.servers
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
        lsp_server_details: list[dict[str, object]] = []
        for server_name in sorted(lsp_state.configuration.servers):
            server_state = lsp_state.servers.get(server_name)
            server_config = lsp_state.configuration.servers.get(server_name)
            lsp_server_details.append(
                {
                    "server": server_name,
                    "status": (
                        server_state.status
                        if server_state is not None
                        else "disabled"
                        if lsp_state.mode != "managed" or not lsp_state.configuration.configured_enabled
                        else "stopped"
                    ),
                    "available": bool(server_state and server_state.available),
                    "command": (list(server_config.command) if server_config is not None else []),
                    "error": (None if server_state is None else server_state.last_error),
                }
            )
        mcp_server_details: list[dict[str, object]] = []
        for server_name, server_config in sorted(mcp_configured_servers.items()):
            runtime_state = mcp_state.servers.get(server_name)
            command = (
                list(runtime_state.command) if runtime_state is not None and runtime_state.command else list(getattr(server_config, "command", ()))
            )
            server_status = (
                runtime_state.status
                if runtime_state is not None
                else "disabled"
                if mcp_state.mode != "managed" or not mcp_state.configuration.configured_enabled
                else "stopped"
            )
            mcp_server_details.append(
                {
                    "server": server_name,
                    "status": server_status,
                    "scope": (runtime_state.scope if runtime_state is not None else getattr(server_config, "scope", "runtime")),
                    "transport": getattr(server_config, "transport", "stdio"),
                    "workspace_root": (None if runtime_state is None else runtime_state.workspace_root),
                    "stage": None if runtime_state is None else runtime_state.stage,
                    "error": None if runtime_state is None else runtime_state.error,
                    "command": redact_mcp_command(command),
                    "retry_available": (False if runtime_state is None else runtime_state.retry_available),
                }
            )
        background_status_counts = self._background_task_supervisor.status_counts()
        return RuntimeStatusSnapshot(
            git=git,
            lsp=CapabilityStatusSnapshot(
                state=lsp_status,
                error=lsp_error,
                details={
                    "mode": lsp_state.mode,
                    "configured": bool(lsp_state.configuration.servers),
                    "configured_enabled": lsp_state.configuration.configured_enabled,
                    "configured_server_count": len(lsp_state.configuration.servers),
                    "running_server_count": sum(1 for server in lsp_servers if server.status == "running"),
                    "failed_server_count": sum(1 for server in lsp_servers if server.status == "failed"),
                    "servers": lsp_server_details,
                },
            ),
            mcp=CapabilityStatusSnapshot(
                state=mcp_status,
                error=mcp_error,
                details={
                    "mode": mcp_state.mode,
                    "configured": bool(mcp_configured_servers),
                    "configured_enabled": mcp_state.configuration.configured_enabled,
                    "configured_server_count": len(mcp_configured_servers),
                    "active_server_count": len(mcp_servers),
                    "running_server_count": sum(1 for server in mcp_servers if server.status == "running"),
                    "failed_server_count": sum(1 for server in mcp_servers if server.status == "failed"),
                    "retry_available": any(server.retry_available for server in mcp_servers),
                    "servers": mcp_server_details,
                },
            ),
            acp=self._acp_status_snapshot(acp_state),
            background_tasks=RuntimeBackgroundTaskStatusSnapshot(
                active_worker_slots=self._background_task_supervisor.active_worker_slots(),
                queued_count=background_status_counts.get("queued", 0),
                running_count=background_status_counts.get("running", 0),
                terminal_count=sum(background_status_counts.get(status, 0) for status in ("completed", "failed", "cancelled", "interrupted")),
                default_concurrency=self._config.background_task.default_concurrency,
                provider_concurrency=dict(self._config.background_task.provider_concurrency),
                model_concurrency=dict(self._config.background_task.model_concurrency),
                status_counts=background_status_counts,
            ),
            memory=self.memory_status(),
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
        return WorkspaceReviewService(workspace=self._workspace).snapshot(git=self._git_status_snapshot())

    def review_diff(self, path: str) -> ReviewFileDiff:
        return WorkspaceReviewService(workspace=self._workspace).diff(
            path=path,
            git=self._git_status_snapshot(),
        )

    def _git_status_snapshot(self) -> GitStatusSnapshot:
        result = subprocess.run(
            ["git", "-C", str(self._workspace), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
        )
        stdout = self._decode_subprocess_text_output(result.stdout)
        stderr = self._decode_subprocess_text_output(result.stderr)
        if result.returncode == 0:
            branch_result = subprocess.run(
                ["git", "-C", str(self._workspace), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            return GitStatusSnapshot(
                state="git_ready",
                root=stdout.strip() or str(self._workspace),
                branch=branch_result.stdout.strip() or None if branch_result.returncode == 0 else None,
            )
        if "not a git repository" in stderr.lower():
            return GitStatusSnapshot(
                state="not_git_repo",
                root=None,
                branch=None,
                error=stderr or None,
            )
        return GitStatusSnapshot(
            state="git_error",
            root=None,
            error=stderr or stdout.strip() or None,
            branch=None,
        )

    @staticmethod
    def _decode_subprocess_text_output(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8", errors="replace")
            except Exception:
                return value.decode(errors="replace")
        return ""

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
        return self._provider_auth_inspector.is_configured(provider_name)

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
        self._agent_registry = self._runtime_agent_registry()
        self._config = load_runtime_config(self._workspace)
        self._bind_tool_scope_resolver()
        self._model_provider_registry = ModelProviderRegistry.with_defaults(provider_configs=self._config.providers)
        self._bind_provider_catalog_collaborators()
        self._provider_auth_resolver = ProviderAuthResolver(
            providers=self._config.providers,
            env=os.environ,
        )
        self._bind_provider_auth_inspector()
        initial_agent = self._config.agent
        if initial_agent is None and self._config.execution_engine == "provider":
            initial_agent = RuntimeAgentConfig(preset="leader")
        if initial_agent is not None:
            initial_agent = parse_runtime_agent_payload(
                serialize_runtime_agent_config(initial_agent),
                source="runtime config agent",
                hooks=self._config.hooks,
                agent_registry=self._agent_registry,
            )
            assert initial_agent is not None
            self._validate_runtime_agent_for_execution(
                initial_agent,
                source="runtime config agent",
            )
        initial_model = initial_agent.model if initial_agent is not None and initial_agent.model is not None else self._config.model
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
            permission=self._config.permission,
            model=initial_model,
            execution_engine=initial_execution_engine,
            max_steps=self._config.max_steps,
            tool_timeout_seconds=self._config.tool_timeout_seconds,
            provider_fallback=initial_provider_fallback,
            providers=self._config.providers,
            resolved_provider=self._resolved_provider_config,
            agent=initial_agent,
            context_window=self._config.context_window,
            tools=self._config.tools,
            policy=self._config.policy,
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
        return debug_event(event)

    @staticmethod
    def _current_debug_status(
        *,
        result: RuntimeSessionResult,
        active: bool,
        pending_approval: PendingApproval | None,
        pending_question: PendingQuestion | None,
    ) -> str:
        return current_debug_status(
            result=result,
            active=active,
            pending_approval=pending_approval,
            pending_question=pending_question,
        )

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
        return debug_failure(
            result=result,
            last_failure_event=last_failure_event,
            last_tool=last_tool,
            pending_approval=pending_approval,
            pending_question=pending_question,
            resume_checkpoint=resume_checkpoint,
            persistence_error=persistence_error,
        )

    @staticmethod
    def _debug_session_state_inconsistency(
        *,
        result: RuntimeSessionResult,
        pending_approval: PendingApproval | None,
        pending_question: PendingQuestion | None,
        resume_checkpoint: dict[str, object] | None,
    ) -> str | None:
        return debug_session_state_inconsistency(
            result=result,
            pending_approval=pending_approval,
            pending_question=pending_question,
            resume_checkpoint=resume_checkpoint,
        )

    @staticmethod
    def _last_tool_summary(result: RuntimeSessionResult) -> RuntimeSessionDebugToolSummary | None:
        return last_tool_summary(result)

    @staticmethod
    def _artifact_debug_metadata(payload: dict[str, object]) -> dict[str, object]:
        return artifact_debug_metadata(payload)

    @classmethod
    def _payload_with_artifact_status(cls, payload: dict[str, object]) -> dict[str, object]:
        _ = cls
        return payload_with_artifact_status(payload)

    def _provider_context_debug_snapshot(
        self,
        result: RuntimeSessionResult,
    ) -> RuntimeProviderContextSnapshot:
        prompt, tool_results = self._prompt_and_tool_results_from_debug_events(result.transcript)
        if not prompt:
            prompt = result.prompt
        assembled_context = self._assemble_provider_context(
            prompt=prompt,
            tool_results=tuple(tool_results),
            session_metadata=result.session.metadata,
            skill_prompt_context=self._debug_skill_prompt_context(result.session.metadata),
        )
        context_window_metadata = result.session.metadata.get("context_window")
        if isinstance(context_window_metadata, dict):
            typed_context_window_metadata = cast(dict[str, object], context_window_metadata)
            preserved_transform_metadata = typed_context_window_metadata.get("context_transforms")
            if isinstance(preserved_transform_metadata, dict):
                assembled_context = RuntimeAssembledContext(
                    prompt=assembled_context.prompt,
                    tool_results=assembled_context.tool_results,
                    continuity_state=assembled_context.continuity_state,
                    segments=assembled_context.segments,
                    metadata={
                        **assembled_context.metadata,
                        "context_transforms": dict(cast(dict[str, object], preserved_transform_metadata)),
                    },
                    loaded_skills=assembled_context.loaded_skills,
                )
        effective_config = self._effective_runtime_config_from_metadata(result.session.metadata)
        return self._provider_context_snapshot_for_assembled_context(
            assembled_context=assembled_context,
            effective_config=effective_config,
        )

    def _provider_context_snapshot_for_assembled_context(
        self,
        *,
        assembled_context: RuntimeAssembledContext,
        effective_config: EffectiveRuntimeConfig,
    ) -> RuntimeProviderContextSnapshot:
        active_target = effective_config.resolved_provider.active_target
        provider = active_target.selection.provider or "unknown"
        model = active_target.selection.model or active_target.selection.raw_model or "unknown"
        tool_registry = self._tool_registry_for_effective_config(effective_config)
        context_window_config = effective_config.context_window or RuntimeContextWindowConfig()
        return inspect_provider_context(
            assembled_context=assembled_context,
            provider=provider,
            model=model,
            execution_engine=effective_config.execution_engine,
            available_tool_count=len(self._provider_tool_definitions(tool_registry, effective_config)),
            tool_feedback_mode=self._tool_feedback_mode_for_effective_config(effective_config),
            oversized_tool_feedback_chars=(context_window_config.provider_context_oversized_feedback_chars),
            diagnostic_policy_mode=context_window_config.provider_context_diagnostics,
        )

    @staticmethod
    def _tool_feedback_mode_for_effective_config(
        effective_config: EffectiveRuntimeConfig,
    ) -> ToolFeedbackMode:
        active_target = effective_config.resolved_provider.active_target
        if active_target.metadata is not None and active_target.metadata.tool_feedback_mode is not None:
            return active_target.metadata.tool_feedback_mode
        return "standard"

    def _provider_context_policy_decision_for_graph_request(
        self,
        *,
        graph_request: GraphRunRequest,
        effective_config: EffectiveRuntimeConfig,
    ) -> RuntimeProviderContextPolicyDecision | None:
        if effective_config.execution_engine != "provider":
            return None
        context_window_config = effective_config.context_window or RuntimeContextWindowConfig()
        if context_window_config.provider_context_diagnostics == "off" and context_window_config.context_transform_failure_policy != "block":
            return None
        snapshot = self._provider_context_snapshot_for_assembled_context(
            assembled_context=cast(RuntimeAssembledContext, graph_request.assembled_context),
            effective_config=effective_config,
        )
        return snapshot.policy_decision

    @staticmethod
    def _prompt_and_tool_results_from_debug_events(
        events: tuple[EventEnvelope, ...],
    ) -> tuple[str, list[ToolResult]]:
        return prompt_and_tool_results_from_debug_events(events)

    @staticmethod
    def _provider_visible_tool_result_data(payload: dict[str, object]) -> dict[str, object]:
        return provider_visible_tool_result_data(payload)

    def _debug_skill_prompt_context(self, metadata: dict[str, object]) -> str:
        snapshot = self._skill_snapshot_from_metadata(metadata)
        if snapshot is None:
            return ""
        return snapshot.skill_prompt_context

    @staticmethod
    def _operator_guidance(
        *,
        current_status: str,
        pending_approval: PendingApproval | None,
        pending_question: PendingQuestion | None,
        active: bool,
        resumable: bool,
        terminal: bool,
        failure: RuntimeSessionDebugFailure | None,
    ) -> tuple[str, str]:
        return operator_guidance(
            current_status=current_status,
            pending_approval=pending_approval,
            pending_question=pending_question,
            active=active,
            resumable=resumable,
            terminal=terminal,
            failure=failure,
        )

    def _active_only_session_debug_snapshot(
        self,
        *,
        session_id: str,
    ) -> RuntimeSessionDebugSnapshot:
        active_metadata = self._active_session_metadata(session_id) or {}
        request_metadata = active_metadata.get("request_metadata")
        session_metadata = {
            **(dict(cast(dict[str, object], request_metadata)) if isinstance(request_metadata, dict) else {}),
            "workspace": str(self._workspace),
        }
        prompt = cast(str, active_metadata["prompt"]) if isinstance(active_metadata.get("prompt"), str) else ""
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
        active_request_metadata = VoidCodeRuntime._fresh_request_metadata(cast(RuntimeRequestMetadataPayload, request_metadata))
        persisted_request_metadata = VoidCodeRuntime._request_metadata_from_session_metadata(result.session.metadata)
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
            "reasoning_effort",
            "skills",
            "background_run",
            "background_task_id",
        }
        request_metadata = {key: value for key, value in metadata.items() if key in request_metadata_keys}
        return VoidCodeRuntime._fresh_request_metadata(cast(RuntimeRequestMetadataPayload, request_metadata))

    @staticmethod
    def _run_id_from_session_metadata(metadata: dict[str, object]) -> str | None:
        return run_id_from_session_metadata(metadata)

    def queue_steering(self, session_id: str, content: str) -> tuple[dict[str, object], ...]:
        """Persist a message to deliver before the next provider turn."""
        return self._queue_runtime_message(session_id, content=content, kind="steering")

    def queue_follow_up(self, session_id: str, content: str) -> tuple[dict[str, object], ...]:
        """Persist a message to deliver after the current run reaches idle."""
        return self._queue_runtime_message(session_id, content=content, kind="follow_up")

    def _queue_runtime_message(
        self,
        session_id: str,
        *,
        content: str,
        kind: Literal["steering", "follow_up"],
    ) -> tuple[dict[str, object], ...]:
        validate_session_id(session_id)
        response = self._load_stored_response(session_id=session_id)
        # Terminal-seal guard: a steer/follow-up is a late event once the
        # session is terminal. It is accepted while a run is active (delivered
        # before the next provider turn) or while waiting on approval/question;
        # it is rejected once the session is sealed so the queued message can
        # never mutate a terminal session's truth.
        sealed_status = self._sealed_session_status(session_id=session_id)
        if sealed_status is not None:
            raise SessionSealedError(f"session {session_id!r} is {sealed_status}: refusing to queue {kind} message on a terminal session")
        metadata = enqueue_runtime_message(response.session.metadata, content=content, kind=kind)
        self._session_store.update_session_metadata(
            workspace=self._workspace,
            session_id=session_id,
            metadata=metadata,
        )
        raw = metadata.get("pending_messages")
        if not isinstance(raw, list):
            return ()
        return tuple(cast(dict[str, object], item) for item in raw if isinstance(item, dict))

    def drain_queued_messages(
        self,
        session_id: str,
        *,
        kind: Literal["steering", "follow_up"],
    ) -> tuple[str, ...]:
        validate_session_id(session_id)
        response = self._load_stored_response(session_id=session_id)
        metadata, messages = drain_runtime_messages(response.session.metadata, kind=kind)
        self._session_store.update_session_metadata(
            workspace=self._workspace,
            session_id=session_id,
            metadata=metadata,
        )
        return tuple(message.content for message in messages)

    def _persist_tool_execution_intent(self, session: SessionState, intent: dict[str, object]) -> None:
        runtime_state = session.metadata.get("runtime_state")
        state = dict(cast(dict[str, object], runtime_state)) if isinstance(runtime_state, dict) else {}
        pending = dict(intent)
        state["pending_tool_intent"] = pending
        metadata = {**session.metadata, "runtime_state": state}
        try:
            self._session_store.update_session_metadata(
                workspace=self._workspace,
                session_id=session.session.id,
                metadata=metadata,
            )
        except UnknownSessionError:
            # The initial run snapshot may not have committed yet.
            logger.debug("tool intent persistence deferred for new session %s", session.session.id)

    def _clear_tool_execution_intent(self, session: SessionState) -> None:
        try:
            persisted_session = self._load_stored_response(session_id=session.session.id).session
        except UnknownSessionError:
            logger.debug("tool intent cleanup deferred for new session %s", session.session.id)
            return
        runtime_state = persisted_session.metadata.get("runtime_state")
        if not isinstance(runtime_state, dict) or "pending_tool_intent" not in runtime_state:
            return
        state = dict(cast(dict[str, object], runtime_state))
        state.pop("pending_tool_intent", None)
        try:
            self._session_store.update_session_metadata(
                workspace=self._workspace,
                session_id=session.session.id,
                metadata={**persisted_session.metadata, "runtime_state": state},
            )
        except UnknownSessionError:
            logger.debug("tool intent cleanup deferred for new session %s", session.session.id)

    def pending_tool_intent(self, session_id: str) -> dict[str, object] | None:
        """Return an unsettled tool intent left by an interrupted process."""
        validate_session_id(session_id)
        response = self._load_stored_response(session_id=session_id)
        raw_runtime_state = response.session.metadata.get("runtime_state")
        if not isinstance(raw_runtime_state, dict):
            return None
        runtime_state = cast(dict[str, object], raw_runtime_state)
        intent = runtime_state.get("pending_tool_intent")
        return cast(dict[str, object], intent) if isinstance(intent, dict) else None

    def pending_tool_recovery(self, session_id: str) -> dict[str, object] | None:
        """Return the deterministic recovery action for an unsettled tool."""
        raw_intent = self.pending_tool_intent(session_id)
        if raw_intent is None:
            return None
        try:
            intent = ToolExecutionIntent(
                tool_call_id=str(raw_intent["tool_call_id"]),
                tool_name=str(raw_intent["tool_name"]),
                arguments=cast(dict[str, object], raw_intent["arguments"]),
                replay_policy=cast(Literal["safe", "never"], raw_intent["replay_policy"]),
                status=cast(Literal["pending", "completed", "interrupted"], raw_intent.get("status", "pending")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"persisted pending tool intent for session {session_id!r} is corrupt") from exc
        return {
            "action": recovery_action(intent),
            "message": (
                "The interrupted tool is safe to replay."
                if recovery_action(intent) == "replay"
                else "The interrupted tool may have side effects and must not be replayed automatically."
            ),
            "intent": intent.metadata_payload(),
        }

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> RuntimeResponse:
        validate_session_id(session_id)
        if approval_request_id is None and approval_decision is None:
            checkpoint = self._load_resume_checkpoint(session_id=session_id)
            if checkpoint is not None and checkpoint.get("kind") == "provider_failure_retryable":
                self._background_task_supervisor.reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
                return self._resume_provider_failure_response(
                    session_id=session_id,
                    checkpoint=checkpoint,
                    finalize_background_task=True,
                )
            if checkpoint is not None and checkpoint.get("kind") == "interrupted":
                self._background_task_supervisor.reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
                return self._resume_interrupted_response(
                    session_id=session_id,
                    checkpoint=checkpoint,
                    finalize_background_task=True,
                )
            self._background_task_supervisor.reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
            return self._load_replay_response(session_id=session_id)
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
        self._background_task_supervisor.finalize_background_task_from_session_response(session_response=response)
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
            checkpoint = self._load_resume_checkpoint(session_id=session_id)
            if checkpoint is not None and checkpoint.get("kind") == "provider_failure_retryable":
                self._background_task_supervisor.reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
                run_id = os.urandom(8).hex()
                abort_signal = self._register_active_session_id(
                    session_id,
                    run_id=run_id,
                    metadata={
                        "resume": True,
                        "resume_kind": "provider_failure_retryable",
                        "run_id": run_id,
                    },
                )
                try:
                    yield from self._resume_provider_failure_stream(
                        session_id=session_id,
                        checkpoint=checkpoint,
                        run_id=run_id,
                        abort_signal=abort_signal,
                        finalize_background_task=True,
                    )
                finally:
                    self._unregister_active_session_id(session_id, run_id=run_id)
                return
            if checkpoint is not None and checkpoint.get("kind") == "interrupted":
                self._background_task_supervisor.reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
                run_id = os.urandom(8).hex()
                abort_signal = self._register_active_session_id(
                    session_id,
                    run_id=run_id,
                    metadata={
                        "resume": True,
                        "resume_kind": "interrupted",
                        "run_id": run_id,
                    },
                )
                try:
                    yield from self._resume_interrupted_stream(
                        session_id=session_id,
                        checkpoint=checkpoint,
                        run_id=run_id,
                        abort_signal=abort_signal,
                        finalize_background_task=True,
                    )
                finally:
                    self._unregister_active_session_id(session_id, run_id=run_id)
                return
            self._background_task_supervisor.reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
            response = self._load_replay_response(session_id=session_id)
            yield from self._replay_response(response)
            return
        if approval_request_id is None or approval_decision is None:
            raise ValueError("approval resume requires request id and decision")
        self._validate_resume_targets_owned_request(
            session_id=session_id,
            approval_request_id=approval_request_id,
        )
        run_id = os.urandom(8).hex()
        abort_signal = self._register_active_session_id(
            session_id,
            run_id=run_id,
            metadata={
                "resume": True,
                "resume_kind": "approval",
                "approval_request_id": approval_request_id,
                "run_id": run_id,
            },
        )
        try:
            yield from self._resume_pending_approval_stream(
                session_id=session_id,
                approval_request_id=approval_request_id,
                approval_decision=approval_decision,
                run_id=run_id,
                abort_signal=abort_signal,
                finalize_background_task=True,
            )
        finally:
            self._unregister_active_session_id(session_id, run_id=run_id)

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
            raise ValueError("approval resume must target the child session that owns the approval request")
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
                child_response = self._background_task_supervisor.load_background_task_child_response(task=task)
                owned_request_id = (
                    self._waiting_request_id_from_response(
                        child_response,
                        request_kind=request_kind,
                    )
                    if child_response is not None
                    else (task.approval_request_id if request_kind == "approval" else task.question_request_id)
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
        self._background_task_supervisor.finalize_background_task_from_session_response(session_response=response)
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
        run_id = os.urandom(8).hex()
        abort_signal = self._register_active_session_id(
            session_id,
            run_id=run_id,
            metadata={
                "resume": True,
                "resume_kind": "question",
                "question_request_id": question_request_id,
                "run_id": run_id,
            },
        )
        try:
            yield from self._answer_pending_question_stream(
                session_id=session_id,
                question_request_id=question_request_id,
                responses=responses,
                run_id=run_id,
                abort_signal=abort_signal,
                finalize_background_task=True,
            )
        finally:
            self._unregister_active_session_id(session_id, run_id=run_id)

    def _pending_approval_from_response(self, response: RuntimeResponse) -> PendingApproval:
        return pending_approval_from_response(response)

    @staticmethod
    def _request_event_and_resolution_state(
        events: tuple[EventEnvelope, ...],
        *,
        request_kind: Literal["approval", "question"],
        request_id: str,
    ) -> tuple[EventEnvelope | None, bool]:
        return request_event_and_resolution_state(
            events,
            request_kind=request_kind,
            request_id=request_id,
        )

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
            raise ValueError("approval request was already resolved; stale approval replay is not allowed")
        if request_event is None:
            if checkpoint is None:
                raise ValueError("persisted pending approval has no matching approval request event")
            if checkpoint.get("pending_approval_request_id") != pending.request_id:
                raise ValueError("persisted approval resume checkpoint request id does not match pending approval")
            if checkpoint.get("pending_approval_tool_name") != pending.tool_name or checkpoint.get("pending_approval_arguments") != pending.arguments:
                raise ValueError("persisted pending approval no longer matches the recorded approval request payload")
            if checkpoint.get("pending_approval_owner_session_id") != pending.owner_session_id:
                raise ValueError("persisted pending approval owner_session_id does not match the recorded approval request")
            if checkpoint.get("pending_approval_owner_parent_session_id") != pending.owner_parent_session_id:
                raise ValueError("persisted pending approval owner_parent_session_id does not match the recorded approval request")
            if checkpoint.get("pending_approval_delegated_task_id") != pending.delegated_task_id:
                raise ValueError("persisted pending approval delegated_task_id does not match the recorded approval request")
            checkpoint_sequence = checkpoint.get("pending_approval_request_event_sequence")
            if (
                pending.request_event_sequence is not None
                and checkpoint_sequence is not None
                and checkpoint_sequence != pending.request_event_sequence
            ):
                raise ValueError("persisted pending approval sequence does not match the recorded approval request")
            return
        if pending.request_event_sequence is not None and request_event.sequence != pending.request_event_sequence:
            raise ValueError("persisted pending approval sequence does not match the recorded approval request")
        payload = request_event.payload
        if payload.get("tool") != pending.tool_name or payload.get("arguments") != pending.arguments:
            raise ValueError("persisted pending approval no longer matches the recorded approval request payload")
        if payload.get("owner_session_id") != pending.owner_session_id:
            raise ValueError("persisted pending approval owner_session_id does not match the recorded approval request")
        if payload.get("owner_parent_session_id") != pending.owner_parent_session_id:
            raise ValueError("persisted pending approval owner_parent_session_id does not match the recorded approval request")
        if payload.get("delegated_task_id") != pending.delegated_task_id:
            raise ValueError("persisted pending approval delegated_task_id does not match the recorded approval request")

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
            raise ValueError("question request was already answered; stale question replay is not allowed")
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
                raise ValueError("persisted pending question has no matching question request event")
            if checkpoint.get("pending_question_request_id") != pending.request_id:
                raise ValueError("persisted question resume checkpoint request id does not match pending question")
            if checkpoint.get("pending_question_tool_name") != pending.tool_name:
                raise ValueError("persisted pending question tool does not match the recorded question request")
            if checkpoint.get("pending_question_prompts") != expected_questions:
                raise ValueError("persisted pending question no longer matches the recorded question request payload")
            return
        payload = request_event.payload
        if payload.get("tool") != pending.tool_name:
            raise ValueError("persisted pending question tool does not match the recorded question request")
        if payload.get("questions") != expected_questions:
            raise ValueError("persisted pending question no longer matches the recorded question request payload")

    def _pending_question_from_response(self, response: RuntimeResponse) -> PendingQuestion | None:
        return pending_question_from_response(response)

    def _resume_pending_approval_stream(
        self,
        *,
        session_id: str,
        approval_request_id: str,
        approval_decision: PermissionResolution,
        run_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
        finalize_background_task: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        yield from self._resume_coordinator.resume_pending_approval_stream(
            session_id=session_id,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
            run_id=run_id,
            abort_signal=abort_signal,
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
        run_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
        finalize_background_task: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        yield from self._resume_coordinator.answer_pending_question_stream(
            session_id=session_id,
            question_request_id=question_request_id,
            responses=responses,
            run_id=run_id,
            abort_signal=abort_signal,
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

    @staticmethod
    def _waiting_reason_from_session(session: SessionState) -> str:
        plan_state = session.metadata.get("plan_state")
        if not isinstance(plan_state, dict):
            return "waiting"
        plan_state_payload = cast(dict[str, object], plan_state)
        status = plan_state_payload.get("status")
        if status == "waiting_approval":
            return "waiting_for_approval"
        if status == "waiting_question":
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

    def _resume_provider_failure_response(
        self,
        *,
        session_id: str,
        checkpoint: dict[str, object],
        finalize_background_task: bool = False,
    ) -> RuntimeResponse:
        return self._resume_coordinator.resume_provider_failure_response(
            session_id=session_id,
            checkpoint=checkpoint,
            finalize_background_task=finalize_background_task,
        )

    def _resume_interrupted_response(
        self,
        *,
        session_id: str,
        checkpoint: dict[str, object],
        finalize_background_task: bool = False,
    ) -> RuntimeResponse:
        return self._resume_coordinator.resume_interrupted_response(
            session_id=session_id,
            checkpoint=checkpoint,
            finalize_background_task=finalize_background_task,
        )

    def _resume_provider_failure_stream(
        self,
        *,
        session_id: str,
        checkpoint: dict[str, object],
        run_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
        finalize_background_task: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        yield from self._resume_coordinator.resume_provider_failure_stream(
            session_id=session_id,
            checkpoint=checkpoint,
            run_id=run_id,
            abort_signal=abort_signal,
            finalize_background_task=finalize_background_task,
        )

    def _resume_interrupted_stream(
        self,
        *,
        session_id: str,
        checkpoint: dict[str, object],
        run_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
        finalize_background_task: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        yield from self._resume_coordinator.resume_interrupted_stream(
            session_id=session_id,
            checkpoint=checkpoint,
            run_id=run_id,
            abort_signal=abort_signal,
            finalize_background_task=finalize_background_task,
        )

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
                    "completed" if response.session.status == "completed" else response.session.status,
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
    def _replayed_chunk_session(*, response_session: SessionState, event: EventEnvelope) -> SessionState:
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
            or event.event_type
            in {
                "runtime.acp_disconnected",
            }
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

        raw_metadata = {key: value for key, value in request.metadata.items()}
        if parent_session_id is not None:
            parent_metadata = self._parent_policy_metadata(parent_session_id)
            if parent_metadata is not None:
                raw_metadata = self._metadata_with_inherited_child_policy(
                    child_metadata=raw_metadata,
                    parent_metadata=parent_metadata,
                )
        raw_workflow_mode = raw_metadata.get("workflow_mode")
        self._validate_explicit_workflow_mode_metadata(raw_metadata)
        self._validate_command_workflow_metadata(raw_metadata)
        if raw_workflow_mode is not None:
            _ = self._workflow_mode_resolution_for_request_metadata(raw_metadata)
        metadata = validate_runtime_request_metadata(
            self._metadata_without_workflow_mode(raw_metadata),
            allow_internal_fields=allow_internal_metadata,
        )
        metadata = self._restore_explicit_workflow_mode(metadata, raw_metadata)
        existing_session = self._load_existing_session_if_present(session_id=session_id) if session_id is not None else None
        governance_parent_session_id = parent_session_id
        if governance_parent_session_id is None and existing_session is not None:
            governance_parent_session_id = existing_session.session.session.parent_id
        metadata = self._metadata_with_resolved_subagent_route(
            metadata,
            allow_internal_fields=allow_internal_metadata,
            parent_session_id=governance_parent_session_id,
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
                existing_parent_label = existing_parent_session_id if existing_parent_session_id is not None else "<top-level>"
                raise RuntimeRequestError(
                    f"session {session_id} already belongs to {existing_parent_label} and cannot be rebound to parent session {parent_session_id}"
                )

        prompt, metadata = self._resolve_prompt_command_for_request(
            prompt=request.prompt,
            metadata=metadata,
            allow_internal_fields=allow_internal_metadata,
        )

        return RuntimeRequest(
            prompt=prompt,
            session_id=session_id,
            parent_session_id=resolved_parent_session_id,
            metadata=metadata,
            allocate_session_id=request.allocate_session_id,
        )

    def _request_with_inherited_child_policy(self, request: RuntimeRequest) -> RuntimeRequest:
        if request.parent_session_id is None:
            return request
        parent_metadata = self._parent_policy_metadata(request.parent_session_id)
        if parent_metadata is None:
            return request

        child_metadata = dict(request.metadata)
        inherited_metadata = self._metadata_with_inherited_child_policy(
            child_metadata=child_metadata,
            parent_metadata=parent_metadata,
        )
        if inherited_metadata == child_metadata:
            return request
        return RuntimeRequest(
            prompt=request.prompt,
            session_id=request.session_id,
            parent_session_id=request.parent_session_id,
            metadata=cast(RuntimeRequestMetadataPayload, inherited_metadata),
            allocate_session_id=request.allocate_session_id,
        )

    def _parent_policy_metadata(self, parent_session_id: str) -> dict[str, object] | None:
        parent_response = self._load_existing_session_if_present(session_id=parent_session_id)
        if parent_response is not None:
            return parent_response.session.metadata
        return self._active_session_metadata(parent_session_id)

    def _metadata_with_inherited_child_policy(
        self,
        *,
        child_metadata: dict[str, object],
        parent_metadata: dict[str, object],
    ) -> dict[str, object]:
        inherited = dict(child_metadata)
        parent_mode = runtime_mode_from_metadata(parent_metadata)
        child_mode = runtime_mode_from_metadata(child_metadata)
        inherited_mode = self._stricter_runtime_mode(parent_mode, child_mode)
        if inherited_mode != "normal" or "mode" in child_metadata:
            inherited["mode"] = inherited_mode

        parent_read_only = self._effective_runtime_read_only_for_policy_metadata(parent_metadata)
        child_read_only = runtime_read_only_from_metadata(child_metadata)
        if parent_read_only or child_read_only or "read_only" in child_metadata:
            inherited["read_only"] = parent_read_only or child_read_only

        return inherited

    @staticmethod
    def _stricter_runtime_mode(parent_mode: str, child_mode: str) -> str:
        mode_rank = {"normal": 0, "analyze": 1, "plan": 2}
        return parent_mode if mode_rank[parent_mode] >= mode_rank[child_mode] else child_mode

    def _resolve_prompt_command_for_request(
        self,
        *,
        prompt: str,
        metadata: RuntimeRequestMetadataPayload,
        allow_internal_fields: bool,
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
        prompt, normalized = apply_runtime_command_effects(
            host=self,
            resolution=resolution,
            metadata=normalized,
        )
        command_metadata: dict[str, object] = {
            "name": resolution.invocation.name,
            "source": resolution.invocation.source,
            "arguments": list(resolution.invocation.arguments),
            "raw_arguments": resolution.invocation.raw_arguments,
            "original_prompt": resolution.invocation.original_prompt,
        }
        command_agent = resolution.definition.agent
        if command_agent is not None and "agent" not in normalized:
            normalized["agent"] = {"preset": command_agent}
        command_workflow_mode = resolution.definition.workflow_mode
        if command_workflow_mode is not None:
            command_metadata["workflow_mode"] = command_workflow_mode
        if command_workflow_mode is not None and "workflow_mode" not in normalized:
            normalized["workflow_mode"] = command_workflow_mode
        normalized["command"] = command_metadata
        _ = self._workflow_mode_resolution_for_request_metadata(normalized)
        if prompt == resolution.invocation.original_prompt:
            prompt = resolution.invocation.rendered_prompt
        self._validate_command_workflow_metadata(normalized)
        validated = validate_runtime_request_metadata(
            self._metadata_without_workflow_mode(normalized),
            allow_internal_fields=allow_internal_fields or "workflow_plan" in normalized,
        )
        workflow_mode = normalized.get("workflow_mode")
        if isinstance(workflow_mode, str):
            validated = cast(
                RuntimeRequestMetadataPayload,
                {**cast(dict[str, object], validated), "workflow_mode": workflow_mode},
            )
        return prompt, validated

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
                parent_depth = self._delegation_depth_from_metadata(parent_response.session.metadata)
                remaining_spawn_budget = self._remaining_spawn_budget_from_metadata(parent_response.session.metadata)
            elif (active_parent_metadata := self._active_session_metadata(parent_session_id)) is not None:
                parent_depth = self._delegation_depth_from_metadata(active_parent_metadata)
                remaining_spawn_budget = self._remaining_spawn_budget_from_metadata(active_parent_metadata)

        request_depth = parent_depth + 1
        if request_depth > _DELEGATION_GOVERNANCE.max_depth:
            raise RuntimeRequestError(
                f"delegation depth limit exceeded: requested depth {request_depth} exceeds max {_DELEGATION_GOVERNANCE.max_depth}"
            )

        if existing_session_id is None:
            if remaining_spawn_budget < 1:
                raise RuntimeRequestError("delegation spawn budget exhausted for parent session")
            remaining_spawn_budget -= 1

        delegation["depth"] = request_depth
        delegation["remaining_spawn_budget"] = remaining_spawn_budget
        normalized["delegation"] = delegation
        self._validate_command_workflow_metadata(normalized)
        validated = validate_runtime_request_metadata(
            self._metadata_without_workflow_mode(normalized),
            allow_internal_fields=(
                "background_run" in normalized
                or "background_rate_limit_retry" in normalized
                or "background_task_id" in normalized
                or "workflow" in normalized
            ),
        )
        if "workflow_mode" in normalized:
            validated = cast(
                RuntimeRequestMetadataPayload,
                {
                    **cast(dict[str, object], validated),
                    "workflow_mode": normalized["workflow_mode"],
                },
            )
        return validated

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
        return prompt_from_events(events)

    @staticmethod
    def _provider_attempt_from_metadata(metadata: dict[str, object]) -> int:
        # Referenced via extracted collaborators.
        return provider_attempt_from_metadata(metadata)

    @staticmethod
    def _provider_retry_attempt_from_metadata(metadata: dict[str, object]) -> int:
        return provider_retry_attempt_from_metadata(metadata)

    def _provider_transient_retry_config(
        self,
        *,
        provider_name: str,
        session_metadata: dict[str, object],
    ) -> ProviderTransientRetryConfig:
        providers = self._effective_runtime_config_from_metadata(session_metadata).providers
        if providers is None:
            return DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG
        if provider_name == "opencode-go":
            provider_config = providers.opencode_go
        elif provider_name == "openai":
            provider_config = providers.openai
        elif provider_name == "anthropic":
            provider_config = providers.anthropic
        elif provider_name == "google":
            provider_config = providers.google
        elif provider_name == "copilot":
            provider_config = providers.copilot
        elif provider_name == "litellm":
            provider_config = providers.litellm
        elif provider_name == "opencode":
            provider_config = providers.opencode
        elif provider_name == "deepseek":
            provider_config = providers.deepseek
        elif provider_name == "glm":
            provider_config = providers.glm
        elif provider_name == "grok":
            provider_config = providers.grok
        elif provider_name == "minimax":
            provider_config = providers.minimax
        elif provider_name == "kimi":
            provider_config = providers.kimi
        elif provider_name == "qwen":
            provider_config = providers.qwen
        else:
            provider_config = providers.custom.get(provider_name)
        if provider_config is None or provider_config.transient_retry is None:
            return DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG
        return provider_config.transient_retry

    @staticmethod
    def _context_window_config_from_policy(
        policy: ContextWindowPolicy | None,
    ) -> RuntimeContextWindowConfig | None:
        return context_window_config_from_policy(policy)

    @staticmethod
    def _context_window_policy_from_config(
        config: RuntimeContextWindowConfig | None,
        *,
        resolved_provider: ResolvedProviderConfig | None,
        provider_attempt: int = 0,
    ) -> ContextWindowPolicy:
        return context_window_policy_from_config(
            config,
            resolved_provider=resolved_provider,
            provider_attempt=provider_attempt,
        )

    def _prepare_provider_context_window(
        self,
        *,
        prompt: str,
        tool_results: tuple[ToolResult, ...],
        session_metadata: dict[str, object],
        policy: ContextWindowPolicy | None = None,
        abort_signal: ProviderAbortSignal | None = None,
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

    def _rehydrated_tool_results_for_existing_session(
        self,
        *,
        stored: RuntimeResponse | None = None,
        session_id: str | None = None,
        parent_session_id: str | None,
    ) -> tuple[ToolResult, ...]:
        if stored is None and session_id is not None:
            stored = self._load_existing_session_if_present(session_id=session_id)
        if stored is None:
            return ()
        stored_parent_session_id = stored.session.session.parent_id
        if parent_session_id is not None and stored_parent_session_id != parent_session_id:
            return ()
        _prompt, tool_results = self._prompt_and_tool_results_from_debug_events(stored.events)
        return tuple(self._eligible_rehydrated_tool_results(tool_results))

    @staticmethod
    def _rehydrated_conversation_segments_for_existing_session(
        *,
        stored: RuntimeResponse | None = None,
        session_id: str | None = None,
        parent_session_id: str | None,
    ) -> tuple[RuntimeContextSegment, ...]:
        if stored is None and session_id is not None:
            return ()
        if stored is None:
            return ()
        stored_parent_session_id = stored.session.session.parent_id
        if parent_session_id is not None and stored_parent_session_id != parent_session_id:
            return ()

        user_segments: list[RuntimeContextSegment] = []
        for event in stored.events:
            if event.event_type != "runtime.request_received":
                continue
            prompt = event.payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                continue
            user_segments.append(
                RuntimeContextSegment(
                    role="user",
                    content=prompt,
                    metadata={
                        "source": "replayed_conversation",
                        "tier": "recent",
                        "kind": "prior_user_prompt",
                        "sequence": event.sequence,
                    },
                )
            )

        assistant_segments: list[RuntimeContextSegment] = []
        if isinstance(stored.output, str) and stored.output.strip():
            assistant_segments.append(
                RuntimeContextSegment(
                    role="assistant",
                    content=stored.output,
                    metadata={
                        "source": "replayed_conversation",
                        "tier": "recent",
                        "kind": "prior_assistant_output",
                    },
                )
            )

        return tuple((*user_segments, *assistant_segments))

    @staticmethod
    def _next_sequence_for_existing_session(
        *,
        stored: RuntimeResponse | None,
        parent_session_id: str | None,
    ) -> int:
        if stored is None:
            return 1
        if parent_session_id is not None and stored.session.session.parent_id != parent_session_id:
            return 1
        return (stored.events[-1].sequence + 1) if stored.events else 1

    @staticmethod
    def _eligible_rehydrated_tool_results(
        tool_results: list[ToolResult],
    ) -> list[ToolResult]:
        eligible: list[ToolResult] = []
        for result in tool_results:
            if result.tool_name in {"read", "grep", "glob", "ast_grep"}:
                eligible.append(result)
                continue
            if result.tool_name != "shell_exec":
                continue
            command = result.data.get("command")
            if isinstance(command, str) and command.strip():
                eligible.append(result)
        return eligible

    def _assemble_provider_context(
        self,
        *,
        prompt: str,
        tool_results: tuple[ToolResult, ...],
        session_metadata: dict[str, object],
        skill_prompt_context: str = "",
        workflow_mode_prompt_context: str = "",
        preserved_system_segments: tuple[str, ...] = (),
        replayed_conversation_segments: tuple[RuntimeContextSegment, ...] = (),
    ) -> RuntimeAssembledContext:
        if not workflow_mode_prompt_context and session_metadata.get("workflow_mode") is not None:
            workflow = self._workflow_snapshot_from_metadata(session_metadata)
            if workflow is not None:
                raw_effective = workflow.get("effective")
                raw_mode = cast(dict[str, object], raw_effective).get("mode") if isinstance(raw_effective, dict) else None
                if isinstance(raw_mode, str) and raw_mode:
                    mode = get_builtin_workflow_mode(raw_mode)
                    if mode is not None:
                        workflow_mode_prompt_context = self._workflow_mode_prompt_context(
                            WorkflowModeResolution(
                                mode=mode,
                                source="workflow_mode",
                                workflow_mode=mode.id,
                            )
                        )
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
        if raw_agent_preset is None:
            raw_runtime_config = session_metadata.get("runtime_config")
            if isinstance(raw_runtime_config, dict):
                raw_agent_preset = cast(dict[str, object], raw_runtime_config).get("agent")
        agent_preset = cast(dict[str, object], raw_agent_preset) if isinstance(raw_agent_preset, dict) else None
        model_family = effective_config.resolved_provider.active_target.selection.provider
        tool_feedback_mode = self._tool_feedback_mode_for_effective_config(effective_config)
        agent_prompt_context = render_agent_prompt(agent_preset, model_family=model_family) or ""
        workspace_memory_context = self.workspace_memory_prompt_context(self._config.memory)
        hook_preset_context = self._hook_preset_context_from_metadata(
            session_metadata,
            agent=effective_config.agent,
        )
        context_transform_result = build_provider_context_transform_result(
            workspace=self._workspace,
            tool_results=tool_results,
            hook_preset_context=hook_preset_context,
            failure_policy=effective_config.context_window.context_transform_failure_policy
            if effective_config.context_window is not None
            else "warn",
            registry=self._context_transform_registry_for_agent(effective_config.agent),
        )
        assembled_context = assemble_provider_context(
            prompt=prompt,
            tool_results=tool_results,
            session_metadata=session_metadata,
            policy=policy or self._default_context_window_policy,
            agent_prompt_context=agent_prompt_context,
            prompt_profile_name=effective_config.agent.prompt_profile if effective_config.agent is not None else None,
            hook_preset_context=hook_preset_context,
            context_transform_result=context_transform_result,
            skill_prompt_context=skill_prompt_context,
            workflow_mode_prompt_context=workflow_mode_prompt_context,
            preserved_system_segments=preserved_system_segments,
            loaded_skills=loaded_skills,
            preserved_continuity_state=self._continuity_state_from_session_metadata(session_metadata),
            workspace_memory_context=workspace_memory_context,
            workspace=self._workspace,
            replay_retained_tool_messages=tool_feedback_mode != "synthetic_user_message",
            replayed_conversation_segments=replayed_conversation_segments,
        )
        delegation = session_metadata.get("delegation")
        if not isinstance(delegation, dict):
            return assembled_context
        return RuntimeAssembledContext(
            prompt=assembled_context.prompt,
            tool_results=assembled_context.tool_results,
            continuity_state=assembled_context.continuity_state,
            segments=assembled_context.segments,
            metadata={**assembled_context.metadata, "delegation": dict(delegation)},
            loaded_skills=assembled_context.loaded_skills,
        )

    @staticmethod
    def _continuity_state_from_session_metadata(
        session_metadata: dict[str, object],
    ) -> ContextProjection | None:
        # Referenced via extracted collaborators.
        runtime_state = session_metadata.get("runtime_state")
        if not isinstance(runtime_state, dict):
            return None
        runtime_state_payload = cast(dict[str, object], runtime_state)
        continuity = runtime_state_payload.get("context_projection")
        if not isinstance(continuity, dict):
            return None
        return continuity_state_from_metadata_payload(cast(dict[str, object], continuity))

    @staticmethod
    def _session_with_context_window_metadata(session: SessionState, context_window: RuntimeContextWindow) -> SessionState:
        return VoidCodeRuntime._session_with_context_window_payload_metadata(session, context_window.metadata_payload())

    @staticmethod
    def _session_with_context_window_payload_metadata(session: SessionState, context_window_payload: dict[str, object]) -> SessionState:
        return session_with_context_window_payload_metadata(session, context_window_payload)

    @staticmethod
    def _session_with_todo_state(
        session: SessionState,
        *,
        raw_todos: object,
        revision: int,
    ) -> tuple[SessionState, dict[str, object]]:
        return session_with_todo_state(session, raw_todos=raw_todos, revision=revision)

    @staticmethod
    def _session_with_provider_usage_metadata(session: SessionState, usage: ProviderTokenUsage | None) -> SessionState:
        return session_with_provider_usage_metadata(session, usage)

    @staticmethod
    def _reasoning_capture_state() -> _ReasoningCaptureState:
        # Referenced via extracted run-loop collaborator.
        return _ReasoningCaptureState()

    def _reasoning_output_diagnostic(
        self,
        *,
        session: SessionState,
        capture_state: _ReasoningCaptureState,
    ) -> dict[str, object] | None:
        # Referenced via extracted run-loop collaborator.
        if capture_state.output_diagnostic_emitted or not capture_state.stream_observed:
            return None
        capture_state.output_diagnostic_emitted = True
        effective_config = self._effective_runtime_config_from_metadata(session.metadata)
        if effective_config.execution_engine != "provider":
            return None
        active_target = effective_config.resolved_provider.active_target.selection
        provider_name = active_target.provider
        model_name = active_target.model
        metadata = self._metadata_for_provider_model(provider_name, model_name) if provider_name is not None and model_name is not None else None
        supports_reasoning = metadata.supports_reasoning if metadata is not None else None
        if capture_state.reasoning_observed:
            severity = "info"
            reason = "reasoning_output_observed"
        elif supports_reasoning is True:
            severity = "warning"
            reason = "reasoning_capable_model_returned_no_reasoning_output"
        else:
            severity = "info"
            reason = "no_reasoning_output_observed"
        return {
            "severity": severity,
            "category": "reasoning_output",
            "reason": reason,
            "provider": provider_name,
            "model": model_name,
            "reasoning_output_observed": capture_state.reasoning_observed,
            "supports_reasoning": supports_reasoning,
            "captured_part_count": capture_state.part_count,
            "captured_text_char_count": capture_state.text_char_count,
        }

    @staticmethod
    def _renumber_events(
        events: tuple[GraphEvent, ...],
        *,
        session_id: str,
        start_sequence: int,
        reasoning_capture_state: _ReasoningCaptureState | None = None,
    ) -> tuple[EventEnvelope, ...]:
        # Referenced via extracted run-loop collaborator.
        return renumber_events(
            events,
            session_id=session_id,
            start_sequence=start_sequence,
            reasoning_capture_state=reasoning_capture_state,
        )

    @staticmethod
    def _loaded_skill_names(skill_registry: SkillRegistry) -> list[str]:
        return loaded_skill_names(skill_registry)

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
        return request_skill_names_from_metadata(metadata, key=key)

    def _build_skill_snapshot(
        self,
        skill_registry: SkillRegistry,
        *,
        metadata: dict[str, object] | None,
        agent: RuntimeAgentConfig | None,
        source: Literal["run", "resume", "replay"],
    ) -> SkillExecutionSnapshot:
        binding_snapshot = self._skill_binding_snapshot(
            metadata,
            require_capability=source != "run",
        )
        if metadata is not None:
            persisted_snapshot = self._skill_snapshot_from_metadata(metadata)
            if persisted_snapshot is not None:
                if binding_snapshot is None:
                    raise ValueError("persisted skill snapshot requires agent capability binding snapshot")
                if persisted_snapshot.binding_snapshot != binding_snapshot:
                    raise ValueError("persisted skill snapshot binding does not match agent capability snapshot")
                return persisted_snapshot
        if source != "run":
            raise ValueError(f"{source} requires a persisted skill snapshot")

        selected_skill_names = self._selected_skill_names_for_agent(
            agent,
            request_skill_names=self._request_skill_names_from_metadata(metadata, key="skills"),
            persisted_selected_skill_names=(self._persisted_selected_skill_names(metadata) if metadata is not None else None),
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
        return effective_selected_skill_names(selected_skill_names, force_load_skill_names)

    def _skill_binding_snapshot(
        self,
        metadata: dict[str, object] | None,
        *,
        require_capability: bool,
    ) -> dict[str, object] | None:
        if metadata is not None:
            if "agent_capability_snapshot" in metadata:
                raw_capability_snapshot = metadata["agent_capability_snapshot"]
                if not isinstance(raw_capability_snapshot, dict):
                    raise ValueError("persisted agent_capability_snapshot must be an object")
                validate_agent_capability_snapshot(cast(dict[str, object], raw_capability_snapshot))
                return self._skill_binding_snapshot_from_agent_capability_snapshot(cast(dict[str, object], raw_capability_snapshot))
        if require_capability:
            raise ValueError("persisted session requires agent_capability_snapshot")
        source_runtime_config = None
        if metadata is not None:
            raw_runtime_config = metadata.get("runtime_config")
            if isinstance(raw_runtime_config, dict):
                source_runtime_config = cast(dict[str, object], raw_runtime_config)
        if source_runtime_config is None:
            source_runtime_config = self._runtime_config_metadata(self._effective_runtime_config_from_metadata(metadata))
        snapshot: dict[str, object] = {}
        for key in _SKILL_BINDING_SCOPE_KEYS:
            if key in source_runtime_config:
                snapshot[key] = source_runtime_config[key]
        return snapshot

    @staticmethod
    def _skill_binding_snapshot_from_agent_capability_snapshot(
        capability_snapshot: dict[str, object],
    ) -> dict[str, object]:
        return skill_binding_snapshot_from_agent_capability_snapshot(capability_snapshot)

    def _agent_capability_snapshot(
        self,
        *,
        effective_config: EffectiveRuntimeConfig,
        tool_materialization: RuntimeToolMaterialization,
        metadata: dict[str, object],
        request_metadata: dict[str, object],
        resolved_hook_presets: ResolvedHookPresetSnapshot,
        workflow_snapshot: dict[str, object] | None = None,
        parent_capability_snapshot: dict[str, object] | None = None,
    ) -> dict[str, object]:
        runtime_config = metadata.get("runtime_config")
        runtime_config_payload = cast(dict[str, object], runtime_config) if isinstance(runtime_config, dict) else {}
        agent = effective_config.agent
        manifest = self._agent_registry.get(agent.preset) if agent is not None else None
        force_load_skills = self._request_skill_names_from_metadata(
            request_metadata,
            key="force_load_skills",
        )
        request_skill_names = self._request_skill_names_from_metadata(
            request_metadata,
            key="skills",
        )
        selected_skills = self._selected_skill_names_for_agent(
            agent,
            request_skill_names=request_skill_names,
        )
        mcp_state = self._mcp_manager.current_state()
        tool_snapshot = agent_capability_tool_snapshot(
            tool_materialization.registry,
            agent,
            tool_materialization.generation,
        )
        skill_snapshot = {
            "manifest_refs": list(agent.manifest_skill_refs) if agent is not None else [],
            "selected_names": list(selected_skills or ()),
            "force_loaded_names": list(dict.fromkeys(force_load_skills or ()).keys() if force_load_skills is not None else ()),
            "scope": "target_session",
        }
        hook_snapshot = {
            "manifest_refs": list(agent.manifest_hook_refs) if agent is not None else [],
            "resolved_refs": list(resolved_hook_presets.refs),
            "snapshot": resolved_hook_presets.to_payload(),
            "materialization": "guidance_only",
            "authority": "non_authoritative",
        }
        mcp_snapshot = {
            "binding_intent": agent_mcp_binding_payload(agent, manifest),
            "configured_enabled": mcp_state.configuration.configured_enabled,
            "mode": mcp_state.mode,
            "configured_servers": list(mcp_state.configuration.servers),
            "governance": "runtime_session_scoped_config_gated",
        }
        delegation_snapshot = agent_capability_delegation_snapshot(
            metadata=metadata,
            parent_capability_snapshot=parent_capability_snapshot,
        )
        return {
            "snapshot_version": AGENT_CAPABILITY_SNAPSHOT_VERSION,
            "precedence": {
                "order": [
                    "builtin_manifest_defaults",
                    "runtime_config_overrides",
                    "request_metadata_overrides",
                    "delegated_force_load_skills",
                ],
                "notes": {
                    "skills": (
                        "manifest skill_refs select catalog-visible defaults; request skills and force_load_skills apply only to this session"
                    ),
                    "hooks": ("hook preset refs materialize guidance snapshots only and do not execute lifecycle commands or expand permissions"),
                    "mcp": (
                        "agent MCP binding is declarative intent; runtime/session-scoped MCP lifecycle and tool allowlists remain runtime-governed"
                    ),
                },
            },
            "agent": agent_capability_agent_snapshot(agent, manifest),
            "prompt": agent_capability_prompt_snapshot(
                agent,
                manifest,
                runtime_config_payload,
            ),
            "tools": tool_snapshot,
            "skills": skill_snapshot,
            "hooks": hook_snapshot,
            "mcp": mcp_snapshot,
            "delegation": delegation_snapshot,
            **(
                {"workflow": workflow_snapshot}
                if workflow_snapshot is not None
                else {"workflow": runtime_workflow}
                if isinstance(runtime_workflow := runtime_config_payload.get("workflow"), dict)
                else {"workflow": metadata_workflow}
                if isinstance(metadata_workflow := metadata.get("workflow"), dict)
                else {}
            ),
            "runtime": {
                "approval_mode": effective_config.approval_mode,
                "max_steps": effective_config.max_steps,
                "tool_timeout_seconds": effective_config.tool_timeout_seconds,
                "permission": runtime_config_payload.get("permission"),
            },
            "execution": {
                "execution_engine": effective_config.execution_engine,
                "model": effective_config.model,
                "fallback_models": (
                    list(effective_config.provider_fallback.fallback_models) if effective_config.provider_fallback is not None else []
                ),
                "resolved_provider": runtime_config_payload.get("resolved_provider"),
                "reasoning_effort": effective_config.reasoning_effort,
            },
        }

    def _session_with_agent_capability_snapshot(
        self,
        *,
        session: SessionState,
        effective_config: EffectiveRuntimeConfig,
        request_metadata: dict[str, object],
        resolved_hook_presets: ResolvedHookPresetSnapshot,
        tool_materialization: RuntimeToolMaterialization,
        workflow_snapshot: dict[str, object] | None = None,
    ) -> SessionState:
        parent_capability_snapshot = self._parent_capability_snapshot_for_session(session)
        return SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata={
                **session.metadata,
                "agent_capability_snapshot": self._agent_capability_snapshot(
                    effective_config=effective_config,
                    tool_materialization=tool_materialization,
                    metadata=session.metadata,
                    request_metadata=request_metadata,
                    resolved_hook_presets=resolved_hook_presets,
                    workflow_snapshot=workflow_snapshot,
                    parent_capability_snapshot=parent_capability_snapshot,
                ),
            },
        )

    def _parent_capability_snapshot_for_session(
        self,
        session: SessionState,
    ) -> dict[str, object] | None:
        parent_session_id = session.session.parent_id
        if parent_session_id is None:
            return None
        parent_metadata = self._parent_policy_metadata(parent_session_id)
        if parent_metadata is None:
            return None
        raw_snapshot = parent_metadata.get("agent_capability_snapshot")
        if raw_snapshot is None:
            return None
        if not isinstance(raw_snapshot, dict):
            raise ValueError("persisted parent agent_capability_snapshot must be an object")
        return validate_agent_capability_snapshot(cast(dict[str, object], raw_snapshot))

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
        return snapshot_to_session_metadata(snapshot)

    def _build_hook_preset_snapshot(
        self,
        agent: RuntimeAgentConfig | None,
        *,
        workflow_mode_resolution: WorkflowModeResolution | None = None,
    ) -> ResolvedHookPresetSnapshot:
        refs = hook_preset_refs_for_mode_and_agent(
            workflow_mode_resolution.mode if workflow_mode_resolution is not None else None,
            agent,
        )
        return resolve_hook_preset_refs(refs)

    @staticmethod
    def _hook_preset_refs_for_agent(agent: RuntimeAgentConfig | None) -> tuple[str, ...]:
        return hook_preset_refs_for_agent(agent)

    def _hook_preset_context_from_metadata(
        self,
        metadata: dict[str, object],
        *,
        agent: RuntimeAgentConfig | None,
    ) -> str:
        raw_snapshot: object | None = metadata.get("resolved_hook_presets")
        if isinstance(raw_snapshot, dict):
            raw_snapshot_payload: object | None = cast(dict[object, object], raw_snapshot)
        else:
            raw_snapshot_payload = None
            raw_runtime_config = metadata.get("runtime_config")
            if isinstance(raw_runtime_config, dict):
                runtime_config_payload = cast(dict[object, object], raw_runtime_config)
                nested_snapshot: object = runtime_config_payload.get("resolved_hook_presets")
                if isinstance(nested_snapshot, dict):
                    raw_snapshot_payload = cast(dict[object, object], nested_snapshot)
        snapshot = hook_preset_snapshot_from_payload(raw_snapshot_payload)
        if snapshot is None:
            snapshot = self._build_hook_preset_snapshot(agent)
        return snapshot.guidance_context()

    def _context_transform_registry_for_agent(
        self,
        agent: RuntimeAgentConfig | None,
    ) -> RuntimeContextTransformRegistry:
        refs = agent.context_transform_refs if agent is not None else ()
        return self._context_transform_registry.filtered(refs)

    @staticmethod
    def _force_loaded_skill_payloads(
        snapshot: SkillExecutionSnapshot,
    ) -> tuple[dict[str, object], ...]:
        return force_loaded_skill_payloads(snapshot)

    def _skill_snapshot_from_metadata(
        self,
        metadata: dict[str, object],
    ) -> SkillExecutionSnapshot | None:
        return skill_snapshot_from_metadata(metadata)

    @staticmethod
    def _selected_skill_names_for_agent(
        agent: RuntimeAgentConfig | None,
        *,
        request_skill_names: tuple[str, ...] | None,
        persisted_selected_skill_names: tuple[str, ...] | None = None,
    ) -> tuple[str, ...] | None:
        return selected_skill_names_for_agent(
            agent,
            request_skill_names=request_skill_names,
            persisted_selected_skill_names=persisted_selected_skill_names,
        )

    @staticmethod
    def _fresh_request_metadata(metadata: RuntimeRequestMetadataPayload) -> dict[str, object]:
        return fresh_request_metadata(cast(dict[str, object], metadata))

    @staticmethod
    def _persisted_selected_skill_names(
        metadata: dict[str, object],
    ) -> tuple[str, ...] | None:
        return persisted_selected_skill_names(metadata)

    @staticmethod
    def _available_runtime_contexts(
        skill_registry: SkillRegistry,
        skill_names: Iterable[str],
    ) -> tuple[SkillRuntimeContext, ...]:
        return available_runtime_contexts(skill_registry, skill_names)

    @staticmethod
    def _catalog_skill_context(
        skill_registry: SkillRegistry,
        *,
        available_skill_names: tuple[str, ...],
        selected_skill_names: tuple[str, ...],
    ) -> str:
        return catalog_skill_context(
            skill_registry,
            available_skill_names=available_skill_names,
            selected_skill_names=selected_skill_names,
        )

    def _runtime_config_metadata(
        self,
        config: EffectiveRuntimeConfig | None = None,
        *,
        workflow_snapshot: object | None = None,
        workflow_mode_resolution: WorkflowModeResolution | None = None,
    ) -> dict[str, object]:
        effective_config = config or self._effective_runtime_config_from_metadata(None)
        runtime_config_metadata = serialize_runtime_config_core(effective_config)
        runtime_config_metadata["resolved_provider"] = resolved_provider_snapshot(effective_config.resolved_provider)
        resolved_hook_presets = self._build_hook_preset_snapshot(
            effective_config.agent,
            workflow_mode_resolution=workflow_mode_resolution,
        )
        if resolved_hook_presets.presets:
            runtime_config_metadata["resolved_hook_presets"] = resolved_hook_presets.to_payload()
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
        if isinstance(workflow_snapshot, dict) and "delegated_child" in workflow_snapshot and workflow_mode_resolution is not None:
            workflow_snapshot = self._workflow_snapshot_with_effective_mode(
                cast(dict[str, object], workflow_snapshot),
                workflow_mode_resolution.workflow_mode,
            )
        if isinstance(workflow_snapshot, dict):
            runtime_config_metadata["workflow"] = dict(cast(dict[str, object], workflow_snapshot))
        elif workflow_mode_resolution is not None:
            runtime_config_metadata["workflow"] = self._workflow_snapshot_for_resolution(workflow_mode_resolution)
        return runtime_config_metadata

    def _config_with_request_agent_override(
        self,
        resolved: EffectiveRuntimeConfig,
        raw_agent: object,
        *,
        allow_subagent_presets: bool = False,
    ) -> EffectiveRuntimeConfig:
        raw_agent_payload = raw_agent if isinstance(raw_agent, dict) else {}
        explicit_prompt_materialization = "prompt_materialization" in raw_agent_payload
        preserved_custom_prompt_materialization = (
            resolved.agent.prompt_materialization
            if resolved.agent is not None
            and isinstance(resolved.agent.prompt_materialization, Mapping)
            and resolved.agent.prompt_materialization.get("source") == "custom_markdown"
            else None
        )
        agent = parse_runtime_agent_payload(
            raw_agent,
            source="request metadata 'agent'",
            hooks=self._config.hooks,
            agent_registry=self._agent_registry,
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
        execution_engine = _agent_effective_execution_engine(resolved.execution_engine, agent)
        provider_fallback = agent.provider_fallback if agent.provider_fallback is not None else resolved.provider_fallback
        merged_agent = RuntimeAgentConfig(
            preset=agent.preset,
            prompt_profile=(
                agent.prompt_profile if agent.prompt_profile is not None else resolved.agent.prompt_profile if resolved.agent is not None else None
            ),
            prompt=(agent.prompt if agent.prompt is not None else resolved.agent.prompt if resolved.agent is not None else None),
            prompt_append=(
                agent.prompt_append if agent.prompt_append is not None else resolved.agent.prompt_append if resolved.agent is not None else None
            ),
            prompt_ref=(agent.prompt_ref if agent.prompt_ref is not None else resolved.agent.prompt_ref if resolved.agent is not None else None),
            prompt_source=(
                agent.prompt_source if agent.prompt_source is not None else resolved.agent.prompt_source if resolved.agent is not None else None
            ),
            prompt_materialization=(
                agent.prompt_materialization
                if explicit_prompt_materialization and agent.prompt_materialization is not None
                else preserved_custom_prompt_materialization
                if preserved_custom_prompt_materialization is not None
                else agent.prompt_materialization
                if agent.prompt_materialization is not None
                else None
            ),
            manifest_source_scope=agent.manifest_source_scope,
            manifest_source_path=agent.manifest_source_path,
            manifest_tool_allowlist=agent.manifest_tool_allowlist,
            manifest_skill_refs=agent.manifest_skill_refs,
            manifest_hook_refs=agent.manifest_hook_refs,
            hook_refs=(agent.hook_refs if agent.hook_refs else resolved.agent.hook_refs if resolved.agent is not None else ()),
            context_transform_refs=(
                agent.context_transform_refs
                if agent.context_transform_refs
                else resolved.agent.context_transform_refs
                if resolved.agent is not None
                else ()
            ),
            model=model,
            execution_engine=execution_engine,
            tools=(agent.tools if agent.tools is not None else resolved.agent.tools if resolved.agent is not None else None),
            skills=(agent.skills if agent.skills is not None else resolved.agent.skills if resolved.agent is not None else None),
            mcp_binding=(agent.mcp_binding if agent.mcp_binding is not None else resolved.agent.mcp_binding if resolved.agent is not None else None),
            provider_fallback=provider_fallback,
        )
        resolved_provider = resolve_provider_config(
            model,
            provider_fallback,
            registry=self._model_provider_registry,
        )
        return EffectiveRuntimeConfig(
            approval_mode=resolved.approval_mode,
            permission=resolved.permission,
            model=model,
            execution_engine=execution_engine,
            max_steps=resolved.max_steps,
            tool_timeout_seconds=resolved.tool_timeout_seconds,
            reasoning_effort=resolved.reasoning_effort,
            provider_fallback=provider_fallback,
            providers=resolved.providers,
            resolved_provider=resolved_provider,
            agent=merged_agent,
            context_window=resolved.context_window,
            tools=resolved.tools,
            policy=resolved.policy,
        )

    def _validate_runtime_agent_for_execution(
        self,
        agent: RuntimeAgentConfig,
        *,
        source: str,
        allow_subagent_presets: bool = False,
    ) -> None:
        executable_primary = self._agent_registry.executable_primary_ids()
        executable_subagents = self._agent_registry.executable_subagent_ids()
        if agent.preset in executable_primary:
            return
        if allow_subagent_presets and agent.preset in executable_subagents:
            return
        valid = ", ".join(sorted(executable_primary))
        if allow_subagent_presets:
            valid = ", ".join(sorted((*executable_primary, *executable_subagents)))
        if allow_subagent_presets:
            raise ValueError(
                f"{source}: agent preset '{agent.preset}' is not executable for this runtime delegation path; executable agent presets are: {valid}"
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
        parent_session_id: str | None = None,
    ) -> RuntimeRequestMetadataPayload:
        resolved_route = runtime_subagent_route_from_metadata(
            metadata,
            callable_subagent_presets=self._agent_registry.executable_subagent_ids(),
        )
        if resolved_route is None:
            return metadata

        normalized_metadata = dict(cast(dict[str, object], metadata))
        raw_delegation_metadata = normalized_metadata["delegation"]
        if not isinstance(raw_delegation_metadata, dict):
            raise RuntimeRequestError("request metadata 'delegation' must be an object when provided")
        delegation_metadata = dict(cast(dict[str, object], raw_delegation_metadata))
        delegation_metadata["selected_preset"] = resolved_route.selected_preset
        delegation_metadata["selected_execution_engine"] = resolved_route.execution_engine
        workflow_snapshot = self._workflow_metadata_for_delegated_child(
            metadata=normalized_metadata,
            selected_child_preset=resolved_route.selected_preset,
            parent_session_id=parent_session_id,
        )

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
                    **({"model": delegated_model} if delegated_model is not None else {}),
                    **({"fallback_models": list(delegated_provider_fallback.fallback_models)} if delegated_provider_fallback is not None else {}),
                },
                source="delegation.selected_preset",
                hooks=self._config.hooks,
                agent_registry=self._agent_registry,
            )
            assert agent is not None
        else:
            agent = parse_runtime_agent_payload(
                raw_agent,
                source="request metadata 'agent'",
                hooks=self._config.hooks,
                agent_registry=self._agent_registry,
            )
            if agent is None:
                raise RuntimeRequestError("request metadata 'agent' must be an object when provided")
            if agent.preset != resolved_route.selected_preset:
                raise RuntimeRequestError(f"request metadata 'agent.preset' must match delegated child preset '{resolved_route.selected_preset}'")
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
        if workflow_snapshot:
            workflow_snapshot = {
                **workflow_snapshot,
                **(
                    {
                        "requested": normalized_workflow["requested"],
                        "effective": normalized_workflow["effective"],
                        "mode": normalized_workflow["mode"],
                        "source": normalized_workflow["source"],
                    }
                    if (normalized_workflow := self._workflow_snapshot_from_metadata({"workflow": workflow_snapshot})) is not None
                    else {}
                ),
                "delegated_child": {
                    "inherited_from_parent": True,
                    "selected_child_preset": resolved_route.selected_preset,
                    "override": False,
                    "policy_enforcement": "audit_metadata_only",
                },
            }
            normalized_metadata["workflow"] = workflow_snapshot
        self._validate_command_workflow_metadata(normalized_metadata)
        validated = validate_runtime_request_metadata(
            self._metadata_without_workflow_mode(normalized_metadata),
            allow_internal_fields=allow_internal_fields or "workflow" in normalized_metadata,
        )
        if "workflow_mode" in normalized_metadata:
            validated = cast(
                RuntimeRequestMetadataPayload,
                {
                    **cast(dict[str, object], validated),
                    "workflow_mode": normalized_metadata["workflow_mode"],
                },
            )
        return validated

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
        return delegated_model_for_route_from_configs(
            category=category,
            selected_preset=selected_preset,
            request_agent=request_agent,
            categories=categories,
            agents=agents,
            base_model=base_model,
        )

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
        category_config = self._category_config(category)
        if category_config is not None and category_config.fallback_models and model is not None:
            return RuntimeProviderFallbackConfig(
                preferred_model=model,
                fallback_models=tuple(fallback_model for fallback_model in category_config.fallback_models if fallback_model != model),
            )
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
        return provider_fallback_for_agent_selection(
            model=model,
            preset_agent=preset_agent,
            base_provider_fallback=base_provider_fallback,
        )

    @staticmethod
    def _provider_fallback_with_preferred_model(
        provider_fallback: RuntimeProviderFallbackConfig,
        preferred_model: str,
    ) -> RuntimeProviderFallbackConfig:
        return provider_fallback_with_preferred_model(provider_fallback, preferred_model)

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
        if delegation.get("category") != "brain":
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
                "requested_category": "brain",
                "provider": provider_name,
                "model": model_name,
                "message": ("task category 'brain' resolved to a model whose provider metadata does not support reasoning"),
            },
        )

    def _runtime_state_metadata(
        self,
        *,
        run_id: str | None = None,
    ) -> dict[str, object]:
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
                "last_delegation": (acp_state.last_delegation.as_payload() if acp_state.last_delegation is not None else None),
            },
        }

    @staticmethod
    def _resolved_hook_preset_snapshot_from_session_metadata(
        metadata: dict[str, object],
    ) -> ResolvedHookPresetSnapshot | None:
        return resolved_hook_preset_snapshot_from_session_metadata(metadata)

    @classmethod
    def _hook_preset_event_payload_from_session_metadata(
        cls,
        metadata: dict[str, object],
    ) -> dict[str, object] | None:
        _ = cls
        return hook_preset_event_payload_from_session_metadata(metadata)

    @classmethod
    def _debug_hook_preset_snapshot(
        cls,
        metadata: dict[str, object],
    ) -> RuntimeHookPresetSnapshot | None:
        _ = cls
        return debug_hook_preset_snapshot(metadata)

    @staticmethod
    def _envelopes_for_lsp_events(
        *,
        session_id: str,
        start_sequence: int,
        lsp_events: tuple[object, ...],
    ) -> tuple[EventEnvelope, ...]:
        return envelopes_for_lsp_events(
            session_id=session_id,
            start_sequence=start_sequence,
            lsp_events=lsp_events,
        )

    @staticmethod
    def _envelopes_for_acp_events(
        *,
        session_id: str,
        start_sequence: int,
        acp_events: tuple[object, ...],
    ) -> tuple[EventEnvelope, ...]:
        return envelopes_for_acp_events(
            session_id=session_id,
            start_sequence=start_sequence,
            acp_events=acp_events,
        )

    @staticmethod
    def _envelopes_for_mcp_events(
        *,
        session_id: str,
        start_sequence: int,
        mcp_events: tuple[object, ...],
    ) -> tuple[EventEnvelope, ...]:
        return envelopes_for_mcp_events(
            session_id=session_id,
            start_sequence=start_sequence,
            mcp_events=mcp_events,
        )

    def _permission_policy_for_session(self, metadata: dict[str, object] | None) -> PermissionPolicy:
        # Referenced via extracted resume collaborator.
        return permission_policy_for_session(base_policy=self._permission_policy, metadata=metadata)

    @staticmethod
    def _approval_request_id_from_waiting_response(response: RuntimeResponse) -> str | None:
        # Referenced via extracted background-task collaborator.
        return approval_request_id_from_waiting_response(response)

    @staticmethod
    def _waiting_request_id_from_response(
        response: RuntimeResponse,
        *,
        request_kind: Literal["approval", "question"],
    ) -> str | None:
        return waiting_request_id_from_response(response, request_kind=request_kind)

    def _effective_runtime_config_from_metadata(self, metadata: dict[str, object] | None) -> EffectiveRuntimeConfig:
        approval_mode: PermissionDecision = self._config.approval_mode
        model = self._config.model
        execution_engine = self._config.execution_engine
        max_steps = self._config.max_steps
        reasoning_effort = self._config.reasoning_effort
        providers = self._config.providers
        provider_fallback = self._config.provider_fallback
        agent = self._config.agent
        if agent is None and execution_engine == "provider":
            agent = RuntimeAgentConfig(preset="leader")
        context_window = self._context_window_config_override or self._config.context_window
        allow_persisted_subagent_presets = False
        if metadata is not None:
            allow_persisted_subagent_presets = (
                runtime_subagent_route_from_metadata(
                    metadata,
                    callable_subagent_presets=self._agent_registry.executable_subagent_ids(),
                )
                is not None
            )
        if agent is not None:
            agent = parse_runtime_agent_payload(
                serialize_runtime_agent_config(agent),
                source="runtime config agent",
                hooks=self._config.hooks,
                agent_registry=self._agent_registry,
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
                agent_registry=self._agent_registry,
            )
            assert agent is not None
            self._validate_runtime_agent_for_execution(
                agent,
                source="runtime config agent",
            )
        execution_engine_override = agent.execution_engine if agent is not None and agent.execution_engine is not None else None
        model_override = agent.model if agent is not None and agent.model is not None else None
        provider_fallback_override = agent.provider_fallback if agent is not None and agent.provider_fallback is not None else None
        if execution_engine != "deterministic" and execution_engine_override is not None:
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
                permission=self._config.permission,
                model=model,
                execution_engine=execution_engine,
                max_steps=max_steps,
                tool_timeout_seconds=self._config.tool_timeout_seconds,
                reasoning_effort=reasoning_effort,
                provider_fallback=provider_fallback,
                providers=providers,
                resolved_provider=resolved_provider,
                agent=agent,
                context_window=context_window,
                tools=self._config.tools,
                policy=self._config.policy,
            )

        persisted_runtime_config = metadata.get("runtime_config")
        if not isinstance(persisted_runtime_config, dict):
            raise ValueError("persisted session metadata must include runtime_config")

        runtime_config = cast(dict[str, object], persisted_runtime_config)
        # Older persisted metadata can contain only the fields that were
        # explicitly overridden for that session. Materialize those partial
        # records against the runtime defaults before applying the strict
        # persisted-config parser used for fully formed snapshots.
        required_keys = {
            "approval_mode",
            "permission",
            "execution_engine",
            "max_steps",
            "tool_timeout_seconds",
            "fallback_models",
        }
        if not required_keys.issubset(runtime_config) and "permission" not in runtime_config:
            partial_tools = self._config.tools
            if "tools" in runtime_config:
                partial_tools = parse_runtime_tools_payload(
                    runtime_config["tools"],
                    source="persisted runtime_config.tools",
                )
            partial_engine = runtime_config.get("execution_engine", self._config.execution_engine)
            if not isinstance(partial_engine, str):
                raise ValueError("persisted runtime_config execution_engine is invalid")
            return EffectiveRuntimeConfig(
                approval_mode=self._config.approval_mode,
                permission=self._config.permission,
                model=self._config.model,
                execution_engine=cast(ExecutionEngineName, partial_engine),
                max_steps=self._config.max_steps,
                tool_timeout_seconds=self._config.tool_timeout_seconds,
                reasoning_effort=self._config.reasoning_effort,
                provider_fallback=self._config.provider_fallback,
                providers=self._config.providers,
                agent=self._config.agent,
                context_window=self._config.context_window,
                tools=partial_tools,
                policy=self._config.policy,
            )
        materialized = parse_persisted_runtime_config(
            runtime_config,
            allow_legacy_permission_scopes=True,
        )
        approval_mode = materialized.approval_mode
        permission = materialized.permission
        policy = materialized.policy
        model = materialized.model
        execution_engine = materialized.execution_engine
        max_steps = materialized.max_steps
        tool_timeout_seconds = materialized.tool_timeout_seconds
        reasoning_effort = materialized.reasoning_effort
        providers = materialized.providers
        provider_fallback = materialized.provider_fallback
        tools = materialized.tools
        context_window = materialized.context_window
        if materialized.has_agent:
            agent = parse_runtime_agent_payload(
                materialized.raw_agent,
                source="persisted runtime_config.agent",
                hooks=self._config.hooks,
                agent_registry=self._agent_registry,
            )
            if agent is not None:
                self._validate_runtime_agent_for_execution(
                    agent,
                    source="persisted runtime_config.agent",
                    allow_subagent_presets=allow_persisted_subagent_presets,
                )
        else:
            agent = None
        raw_resolved_provider = materialized.raw_resolved_provider
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
            permission=permission,
            model=model,
            execution_engine=execution_engine,
            max_steps=max_steps,
            tool_timeout_seconds=tool_timeout_seconds,
            reasoning_effort=reasoning_effort,
            provider_fallback=provider_fallback,
            providers=providers,
            resolved_provider=resolved_provider,
            agent=agent,
            context_window=context_window,
            tools=tools,
            policy=policy,
        )

    def _provider_chain_for_session_metadata(self, metadata: dict[str, object] | None) -> ResolvedProviderChain:
        effective_config = self._effective_runtime_config_from_metadata(metadata)
        return effective_config.resolved_provider.target_chain

    def _graph_for_session_metadata(self, metadata: dict[str, object] | None) -> RuntimeGraph:
        if self._graph_override is not None:
            return self._graph_override

        effective_config = self._effective_runtime_config_from_metadata(metadata)
        if isinstance((metadata or {}).get("command"), dict):
            return self._build_graph_for_engine_from_config(effective_config, use_cache=False)

        # Reuse self._graph if the session's config matches the runtime's config
        if (
            effective_config.execution_engine == self._initial_effective_config.execution_engine
            and effective_config.model == self._initial_effective_config.model
            and effective_config.max_steps == self._initial_effective_config.max_steps
            and effective_config.reasoning_effort == self._initial_effective_config.reasoning_effort
            and effective_config.provider_fallback == self._initial_effective_config.provider_fallback
            and effective_config.providers == self._initial_effective_config.providers
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

    def _load_replay_response(self, *, session_id: str) -> RuntimeResponse:
        response = self._load_stored_response(session_id=session_id)
        # Replay is still a runtime boundary: reject malformed persisted
        # configuration instead of silently projecting unverifiable state.
        if "runtime_config" in response.session.metadata:
            self._effective_runtime_config_from_metadata(response.session.metadata)
        projected_metadata = session_metadata_for_replay(response.session.metadata)
        replay_events = self._events_with_runtime_policy_projection(
            response.events,
            metadata=projected_metadata,
        )
        return RuntimeResponse(
            session=SessionState(
                session=response.session.session,
                status=response.session.status,
                turn=response.session.turn,
                metadata=projected_metadata,
            ),
            events=replay_events,
            output=response.output,
        )

    def _events_with_runtime_policy_projection(
        self,
        events: tuple[EventEnvelope, ...],
        *,
        metadata: dict[str, object],
    ) -> tuple[EventEnvelope, ...]:
        raw_policy = metadata.get("runtime_policy")
        if not isinstance(raw_policy, dict):
            return events
        projected: list[EventEnvelope] = []
        for event in events:
            if event.event_type != "runtime.request_received":
                projected.append(event)
                continue
            projected.append(
                EventEnvelope(
                    session_id=event.session_id,
                    sequence=event.sequence,
                    event_type=event.event_type,
                    source=event.source,
                    payload={
                        **event.payload,
                        "runtime_policy": runtime_policy_observability_payload(cast(dict[str, object], raw_policy)),
                    },
                )
            )
        return tuple(projected)

    def _sealed_session_status(self, *, session_id: str) -> SessionStatus | None:
        """Return the terminal status sealing ``session_id``, or None when mutable.

        Single authoritative runtime-level terminal-seal guard for late events.

        A session's truth is mutable only while:

        - a run is active on it (``ACTIVE_SESSION_REGISTRY`` owns the event
          stream and terminal bookkeeping), or
        - the persisted status is ``waiting`` (pending approval/question —
          resume is pending, steering is still intended), or
        - an explicit re-entry is in progress (fresh run / follow-up /
          approval/question resume un-seal via ``save_interrupted_checkpoint``
          or ``save_run``).

        Otherwise the persisted status decides: ``completed``/``failed`` are
        always sealed, and ``interrupted`` is sealed too — the run that left
        the row ``interrupted`` has ended, so any event arriving from it now is
        late (tool result, provider delta, steer/follow-up) and must be
        rejected or dropped, never applied. Only an explicit resume re-opens an
        ``interrupted`` session.

        Every late-event entry point (interaction queue, background-task
        completion finalization, replay) consults this guard before mutating
        session truth; the storage-level check in ``append_session_event`` /
        ``append_session_events`` remains the last line of defense.
        """
        if ACTIVE_SESSION_REGISTRY.contains(workspace=self._workspace, session_id=session_id):
            return None
        load_status = getattr(self._session_store, "load_session_status", None)
        if callable(load_status):
            try:
                status = load_status(workspace=self._workspace, session_id=session_id)
            except UnknownSessionError:
                return None
        else:
            try:
                status = self._load_stored_response(session_id=session_id).session.status
            except UnknownSessionError:
                return None
        if is_session_status_terminal(status):
            return status
        return None

    def _is_active_session_id(self, session_id: str) -> bool:
        return ACTIVE_SESSION_REGISTRY.contains(workspace=self._workspace, session_id=session_id)

    def _register_active_session_id(
        self,
        session_id: str,
        *,
        run_id: str,
        metadata: dict[str, object] | None = None,
    ) -> ProviderAbortSignal:
        return ACTIVE_SESSION_REGISTRY.register(
            workspace=self._workspace,
            session_id=session_id,
            run_id=run_id,
            metadata=metadata or {},
        )

    def _unregister_active_session_id(self, session_id: str, *, run_id: str | None = None) -> None:
        ACTIVE_SESSION_REGISTRY.unregister(
            workspace=self._workspace,
            session_id=session_id,
            run_id=run_id,
        )

    def _active_session_metadata(self, session_id: str) -> dict[str, object] | None:
        return ACTIVE_SESSION_REGISTRY.metadata(
            workspace=self._workspace,
            session_id=session_id,
        )

    def _active_run_abort_signal(
        self,
        *,
        session_id: str,
        run_id: str,
    ) -> ProviderAbortSignal | None:
        return ACTIVE_SESSION_REGISTRY.abort_signal(
            workspace=self._workspace,
            session_id=session_id,
            run_id=run_id,
        )

    def interrupt_active_run(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        reason: str | None = None,
    ) -> ActiveRunInterruptResult:
        validate_session_id(session_id)
        return ACTIVE_SESSION_REGISTRY.interrupt(
            workspace=self._workspace,
            session_id=session_id,
            run_id=run_id,
            reason=reason,
        )

    def cancel_session(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        reason: str | None = None,
    ) -> ActiveRunInterruptResult:
        return self.interrupt_active_run(session_id, run_id=run_id, reason=reason)

    def _session_belongs_to_workspace(self, session_id: str) -> bool:
        try:
            response = self._load_existing_session_if_present(session_id=session_id)
        except ValueError:
            return False
        if response is None:
            return False
        return True


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
    denied_approval: PendingApproval | None = None


@dataclass(frozen=True, slots=True)
class _RuntimeHookOutcome:
    chunks: tuple[RuntimeStreamChunk, ...]
    last_sequence: int
    failed_error: str | None = None
    action: Literal["continue", "cancel"] = "continue"
