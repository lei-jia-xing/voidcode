from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, cast, final

from ..agent import (
    AgentManifest,
    AgentManifestRegistry,
    AgentPromptMaterialization,
    get_builtin_agent_manifest,
    list_builtin_agent_manifests,
    load_agent_manifest_registry,
)
from ..agent.prompts import render_agent_prompt
from ..command import is_native_command
from ..common.json_values import json_wire_object
from ..core.questions import QuestionAnswer
from ..core.tool_context import ArtifactRead, TranscriptRead
from ..core.transcript import ContextSegment, ToolResultView, project_report
from ..core.turns import FinalTurn, ReportedCall, ToolTurn, TurnProducer, TurnRequest
from ..hook.before_agent_start import (
    BeforeAgentStartBinding,
    BeforeAgentStartDeclaration,
    BeforeAgentStartRegistry,
    BeforeAgentStartRequest,
)
from ..hook.plan import ResolvedHookPlan, hook_plan_from_session_metadata, materialize_hook_plan
from ..hook.presets import (
    ResolvedHookPresetSnapshot,
    resolve_hook_preset_refs,
)
from ..hook.typed import (
    ToolInputHandlerBinding,
    ToolInputHandlerDeclaration,
    ToolInputHandlerRegistry,
    tool_input_arguments_sha256,
)
from ..mcp import McpCachedToolSurface, McpToolDescriptor, list_builtin_mcp_descriptors
from ..provider.auth import (
    ProviderAuthResolver,
)
from ..provider.config import (
    PROVIDER_WIRES,
    ProviderFallbackConfig,
    parse_provider_configs_payload,
    serialize_provider_configs,
)
from ..provider.model_catalog import ProviderModelMetadata, ToolFeedbackMode
from ..provider.models import (
    BoundProviderConfig,
    ProviderDescriptor,
    ProviderModelSelection,
    ResolvedProviderChain,
    ResolvedProviderConfig,
    ResolvedProviderModel,
)
from ..provider.naming import split_provider_model_reference
from ..provider.protocol import ModelTurnProvider, ProviderAbortSignal, ProviderTurnRequest, ProviderTurnResult
from ..provider.reasoning_effort import normalize_reasoning_effort
from ..provider.registry import ModelProviderRegistry, materialize_builtin_provider
from ..provider.resolution import resolve_provider_config
from ..provider.snapshot import (
    parse_resolved_provider_snapshot,
    resolved_provider_snapshot,
)
from ..skills import SkillMetadata, SkillRegistry
from ..tools.contracts import (
    Tool,
    ToolCall,
    ToolDefinition,
)
from ..tools.local_custom import LocalCustomTool, LocalCustomToolManifest, local_custom_tool_definition
from ..tools.mcp import McpTool, mcp_tool_definition
from . import skills
from .acp import (
    AcpAdapter,
    AcpAdapterState,
    build_acp_adapter,
    disconnect_acp_for_session_state,
    emit_acp_events,
    emit_current_acp_drain,
)
from .active_session import (
    ACTIVE_SESSION_REGISTRY,
    ActiveRunInterruptResult,
)
from .agent_capability import (
    AGENT_CAPABILITY_SNAPSHOT_VERSION,
    agent_capability_agent_snapshot,
    agent_capability_delegation_snapshot,
    agent_capability_prompt_snapshot,
    agent_capability_tool_snapshot,
    agent_mcp_binding_payload,
    composition_ref_from_session_metadata,
    validate_agent_capability_snapshot,
)
from .background.models import (
    BackgroundTaskState,
    StoredBackgroundTaskSummary,
    is_background_task_terminal,
)
from .background.process import BackgroundProcessManager
from .background.routing import (
    delegated_model_for_route_from_configs,
    provider_fallback_for_agent_selection,
)
from .background.substrate import (
    TaskSubstrate,
)
from .background.supervisor import RuntimeBackgroundTaskSupervisor
from .bundle import (
    SessionBundle,
    SessionBundleImportResult,
    SessionBundleOptions,
)
from .composition import (
    ActiveComposition,
    BuiltinDeclaration,
    ComponentDeclaration,
    ComponentSelection,
    ComponentSlot,
    CompositionOwner,
    CompositionRef,
    FrozenComposition,
    LifecycleScope,
    PackageDeclaration,
    PhaseContext,
    PhaseOutcome,
    PhaseRecord,
    SessionCompositionOwner,
    ValidatedConfig,
)
from .config.loading import (
    RuntimeAgentConfig,
    RuntimeAgentInternalState,
    RuntimeConfig,
    RuntimeContextWindowConfig,
    RuntimeProviderFallbackConfig,
    RuntimeWebSettings,
    load_runtime_config,
    parse_runtime_agent_payload,
    save_global_web_settings,
    serialize_runtime_agent_config,
    serialize_runtime_agents_config,
)
from .config.materializer import (
    RunConfig,
    parse_persisted_runtime_config,
    serialize_runtime_config_core,
)
from .context.continuity import (
    events_after_manual_compaction,
    replayed_conversation_segments_from_events,
    verified_checkpoint_session_metadata,
)
from .context.provider import inspect_provider_context
from .context.rules import build_rule_catalog, read_rule_uri, rulebook_snapshot_from_payload, rulebook_snapshot_payload
from .context.transforms import (
    RuntimeContextTransformDeclaration,
    RuntimeContextTransformProvider,
    RuntimeContextTransformRegistry,
    build_provider_context_transform_result,
    builtin_runtime_context_transform_declarations,
    materialize_builtin_context_transform,
)
from .context.window import (
    BeforeCompactInput,
    CompactionBudget,
    ContextProjection,
    ContextWindowPolicy,
    ContinuitySummaryKind,
    RuntimeAssembledContext,
    RuntimeContextWindow,
    assemble_provider_context,
    provider_usage_anchor_tokens,
    result_payload_text,
)
from .context.window_policy import (
    compaction_budget_from_config,
    context_window_config_from_policy,
    context_window_policy_from_config,
)
from .contracts import (
    AgentSummary,
    BackgroundTaskGroupResult,
    BackgroundTaskResult,
    CapabilityStatusSnapshot,
    CommandSummary,
    ProviderInspectResult,
    ProviderModelsResult,
    ProviderReadinessResult,
    ProviderSummary,
    ProviderValidationResult,
    ReviewCommitLogEntry,
    ReviewFileDiff,
    ReviewTarget,
    RuntimeProviderContextPolicyDecision,
    RuntimeProviderContextSnapshot,
    RuntimeRequest,
    RuntimeRequestError,
    RuntimeRequestMetadataPayload,
    RuntimeResponse,
    RuntimeSessionDebugEvent,
    RuntimeSessionDebugFailure,
    RuntimeSessionDebugSnapshot,
    RuntimeSessionDebugToolSummary,
    RuntimeSessionResult,
    RuntimeStatusSnapshot,
    RuntimeStreamChunk,
    SessionBusyConflictError,
    SessionEventBatch,
    SkillSummary,
    WorkspaceReviewSnapshot,
    runtime_subagent_route_from_metadata,
    validate_id,
    validate_runtime_request_metadata,
)
from .coordinators.finalize import FinalizeCoordinator
from .coordinators.inspection import InspectionCoordinator
from .coordinators.stream_prep import StreamPrepCoordinator
from .effectiveness import ToolEffectivenessReport
from .event_envelopes import (
    envelopes_for_mcp_events,
)
from .events import (
    RUNTIME_BEFORE_AGENT_START,
    RUNTIME_HOOK_PRESETS_LOADED,
    RUNTIME_REASONING_DIAGNOSTIC,
    RUNTIME_SKILLS_APPLIED,
    RUNTIME_SKILLS_LOADED,
    EventEnvelope,
    EventSource,
    runtime_policy_observability_payload,
)
from .execution import chunk_builders
from .execution.delegation.task import TaskCommand
from .execution.delegation.task_batch import TaskBatchCommand
from .execution.process.background_process import BackgroundProcessCommand
from .execution.provider_execution_metadata import (
    run_id_from_session_metadata,
)
from .execution.resume_checkpoint import validated_resume_checkpoint_envelope
from .execution.seams import (
    resolve_runtime_session_routing,
    select_turn_producer_for_config,
)
from .execution.tool_executor import RuntimeToolExecutor
from .execution.tool_resources import ToolCatalogReader, WorkspaceDiagnostics
from .execution.turn_adapter import turn_request_for_session, turn_session_snapshot
from .fact_codec import reported_call_from_event
from .hook_preset_metadata import (
    hook_preset_event_payload_from_session_metadata,
    hook_preset_refs_for_agent,
)
from .hook_runtime import (
    HOOK_RECURSION_ENV_VAR,
    hook_cancel_reason,
    hook_execution_policy_from_metadata,
    run_lifecycle_hooks_for_session,
)
from .lsp import LspManager, LspManagerState, LspRequestResult, build_lsp_manager
from .mcp import McpManager, build_mcp_manager
from .mcp_tool_cache import McpToolCatalogCache
from .paths import mcp_tool_catalog_cache_path, provider_catalog_cache_path
from .permissions.context import RuntimePermissionContextResolver
from .permissions.engine import PermissionEngine
from .permissions.patch_paths import extract_paths_from_patch
from .permissions.policy import (
    ApprovalMode,
    PendingApproval,
    PermissionPolicy,
    PermissionResolution,
    resolve_permission,
    runtime_effective_read_only_from_metadata,
    runtime_read_only_from_metadata,
    validate_collaboration_mode,
)
from .permissions.responses import (
    waiting_request_id_from_response,
)
from .policy import (
    materialize_runtime_policy_snapshot,
)
from .providers.catalog_cache import RuntimeProviderCatalogCache
from .providers.catalog_query import RuntimeProviderCatalogQuery
from .providers.inspection import (
    ProviderSummaryProjector,
    RuntimeProviderAuthInspector,
)
from .providers.metadata import (
    ReasoningEffortCapability,
)
from .question import PendingQuestion
from .resume import RuntimeResumeCoordinator
from .review import WorkspaceReviewService
from .run_loop import RuntimeRunLoopCoordinator
from .runtime_debug import (
    current_debug_status,
    debug_event,
    debug_failure,
    last_tool_summary,
    operator_guidance,
    prompt_and_tool_results_from_debug_events,
)
from .runtime_surface import PermissionOutcome, RuntimeSurface
from .session import (
    SessionEntrySummary,
    SessionRef,
    SessionState,
    SessionStatus,
    StoredSessionForestEntry,
    StoredSessionLineageEntry,
    StoredSessionSummary,
    reload_persisted_session,
)
from .session_metadata_helpers import (
    _DELEGATION_GOVERNANCE,
    continuity_state_from_session_metadata,
    delegation_depth_from_metadata,
    parse_delegation_metadata,
    parse_runtime_state_metadata,
    remaining_spawn_budget_from_metadata,
    runtime_state_metadata_payload,
    runtime_state_pending_tool_intent,
    session_metadata_with_runtime_state_updates,
    session_model_identity,
    session_with_context_compacted_state,
    session_with_context_window_payload_metadata,
    session_with_current_acp_metadata,
    session_with_plan_state,
)
from .skill_metadata import (
    effective_selected_skill_names,
    fresh_request_metadata,
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
    skill_prompt_context_for_assembly,
)
from .status_projection import project_acp_status
from .storage import RuntimeRepositories, SessionSealedError, SqliteSessionStore
from .tools.builtins import BUILTIN_TOOL_NAMES, builtin_tool_definitions, materialize_builtin_tool
from .tools.materialization import materialize, materialize_unscoped, workspace_local_tool_manifests_provider
from .tools.materializer import RuntimeToolMaterialization, RuntimeToolMaterializer, tool_provenance
from .tools.registry import ToolPolicyDecision, ToolRegistry, agent_required_tool_patterns
from .tools.scope import RuntimeToolScopeResolver

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_EXECUTABLE_AGENT_PRESETS = frozenset({"default"})

#: System prompt for the opt-in compaction summary (one module-level constant
#: next to the call that uses it, ``VoidCodeRuntime.summarize_continuity``).
_CONTINUITY_SUMMARY_SYSTEM_PROMPT = (
    "You compress conversation history that is about to be discarded from a coding agent's context window.\n"
    "Summarize the work: the objective, user constraints that must survive verbatim, what was completed, "
    "decisions taken, files/commands/errors that matter, verification state, and open blockers.\n"
    "Be specific and factual; never invent progress, files or results that the transcript does not show.\n"
    "Output the summary text only -- no preamble, no commentary, no markdown fences."
)

#: Wall clock for one summary call. The call is speculative output, so it gets a
#: hard bound rather than the provider's own (default 300s) turn timeout.
_CONTINUITY_SUMMARY_TIMEOUT_SECONDS = 60.0
#: Chars of context handed to the model: the whole-input cap, then the per-result
#: preview cap applied oldest-first (the order pruning discards in).
_CONTINUITY_SUMMARY_INPUT_CHARS = 60_000
_CONTINUITY_SUMMARY_RESULT_CHARS = 4_000
#: Chars kept from the answer: a summary longer than this is not a summary, and
#: keeping it bounded keeps the continuity segment bounded too.
_CONTINUITY_SUMMARY_OUTPUT_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class _PlainTextProviderContext:
    """Minimal ``AssembledContext`` for the one-shot summary call.

    The call sends one system instruction plus the discarded text; every other
    protocol field is empty, so the wire adapters' tool/replay paths stay inert.
    """

    prompt: str

    @property
    def tool_results(self) -> tuple[ToolResultView, ...]:
        return ()

    @property
    def continuity_state(self) -> None:
        return None

    @property
    def segments(self) -> tuple[ContextSegment, ...]:
        return (
            ContextSegment(role="system", content=_CONTINUITY_SUMMARY_SYSTEM_PROMPT),
            ContextSegment(role="user", content=self.prompt),
        )

    @property
    def metadata(self) -> dict[str, object]:
        return {}


def _continuity_summary_input(tool_results: Sequence[ReportedCall | ToolResultView]) -> str:
    """Bounded, provider-visible text of the results pruning is discarding.

    Oldest first (the pruning order), each result previewed under its own cap so
    one oversized payload cannot crowd out the rest, and the whole input capped
    again so the summary call is never bigger than the content it replaces.
    """
    parts: list[str] = []
    used = 0
    for index, item in enumerate(tool_results):
        result = project_report(item) if isinstance(item, ReportedCall) else item
        body = result_payload_text(result)
        preview = body[:_CONTINUITY_SUMMARY_RESULT_CHARS]
        parts.append(f"[{index}] tool={result.tool_name} status={result.status}\n{preview}")
        used += len(preview) + _CONTINUITY_SUMMARY_RESULT_CHARS // 2
        if used >= _CONTINUITY_SUMMARY_INPUT_CHARS:
            break
    return "\n\n".join(parts)[:_CONTINUITY_SUMMARY_INPUT_CHARS]


_SKILL_BINDING_SCOPE_KEYS = (
    "approval_mode",
    "tool_timeout_seconds",
    "reasoning_effort",
    "model",
    "fallback_models",
    "providers",
    "resolved_provider",
    "agent",
    "lsp",
    "mcp",
)


def _approval_requested_hook_payload(pending: PendingApproval) -> dict[str, object]:
    keys = sorted(pending.arguments)
    if len(keys) > 64:
        keys = [*keys[:64], "[additional argument keys omitted]"]
    payload: dict[str, object] = {
        "request_id": pending.request_id,
        "tool": pending.tool_name,
        "target_summary": pending.target_summary,
        "reason": pending.reason,
        "policy": {"mode": pending.policy_mode},
        "argument_keys": keys,
        "arguments_sha256": tool_input_arguments_sha256(pending.arguments),
    }
    if pending.path_scope is not None:
        payload["path_scope"] = pending.path_scope
    if pending.operation_class is not None:
        payload["operation_class"] = pending.operation_class
    if pending.policy_surface is not None:
        payload["policy_surface"] = pending.policy_surface
    return payload


def _permission_chunks_with_hook_reason(
    chunks: tuple[RuntimeStreamChunk, ...],
    reason: str,
) -> tuple[RuntimeStreamChunk, ...]:
    rebuilt: list[RuntimeStreamChunk] = []
    for chunk in chunks:
        event = chunk.event
        if event is None:
            rebuilt.append(chunk)
            continue
        payload = dict(event.payload)
        payload.setdefault("hook_cancel_reason", reason)
        payload.setdefault("reason", reason)
        rebuilt.append(
            RuntimeStreamChunk(
                kind=chunk.kind,
                session=chunk.session,
                event=EventEnvelope(
                    session_id=event.session_id,
                    sequence=event.sequence,
                    event_type=event.event_type,
                    source=event.source,
                    payload=payload,
                ),
            )
        )
    return tuple(rebuilt)


def _builtin_provider_component_config(value: object) -> ValidatedConfig:
    if not isinstance(value, Mapping):
        raise ValueError("builtin provider component config must be an object")
    payload = json_wire_object(value)
    provider_name = payload.get("provider_name")
    providers = payload.get("providers")
    if not isinstance(provider_name, str) or not provider_name or not isinstance(providers, dict):
        raise ValueError("builtin provider component config is incomplete")
    parsed = parse_provider_configs_payload(
        providers,
        source=f"composition.providers.{provider_name}",
        env=os.environ,
    )
    descriptor = ModelProviderRegistry.with_defaults(provider_configs=parsed).resolve_static(provider_name)
    return ValidatedConfig(
        value=descriptor,
        snapshot={"provider_name": provider_name, "providers": providers},
    )


def _builtin_provider_component_factory(value: object, _context: object) -> object:
    if not isinstance(value, ProviderDescriptor):
        raise ValueError("builtin provider factory requires its admitted descriptor")
    return materialize_builtin_provider(value)


def _builtin_tool_component_config(value: object) -> ValidatedConfig:
    if not isinstance(value, Mapping) or not isinstance(value.get("name"), str):
        raise ValueError("builtin tool component config requires a name")
    definition = next((item for item in builtin_tool_definitions() if item.name == value["name"]), None)
    if definition is None:
        raise ValueError(f"unknown builtin tool component: {value['name']}")
    return ValidatedConfig(
        value=definition,
        snapshot={"name": definition.name, "schema": json_wire_object(definition.input_schema)},
    )


def _builtin_tool_component_factory(value: object, context: object) -> object:
    if not isinstance(value, ToolDefinition) or not isinstance(context, VoidCodeRuntime):
        raise ValueError("builtin tool factory requires its admitted definition and runtime")
    return materialize_builtin_tool(value.name, hooks_config=context._config.hooks)


def _builtin_agent_component_config(value: object) -> ValidatedConfig:
    if not isinstance(value, Mapping) or not isinstance(value.get("name"), str):
        raise ValueError("builtin agent component config requires a name")
    if value.get("source_scope") in ("project", "user"):
        manifest = AgentManifest.from_payload(value)
        return ValidatedConfig(value=manifest, snapshot=manifest.to_payload())
    if value.get("source_scope") == "package":
        raise ValueError("package-origin agent cannot masquerade as runtime-owned declarative config")
    requested = value.get("id", value["name"])
    manifest = get_builtin_agent_manifest(requested) if isinstance(requested, str) else None
    if manifest is None:
        manifest = next((item for item in list_builtin_agent_manifests() if item.name == value["name"]), None)
    if manifest is None:
        raise ValueError(f"unknown builtin agent component: {value['name']}")
    return ValidatedConfig(value=manifest, snapshot=manifest.to_payload())


def _builtin_agent_component_factory(value: object, _context: object) -> object:
    if not isinstance(value, AgentManifest):
        raise ValueError("builtin agent factory requires its admitted manifest")
    return value


def _builtin_context_component_config(value: object) -> ValidatedConfig:
    if not isinstance(value, Mapping) or not isinstance(value.get("name"), str):
        raise ValueError("builtin context component config requires a name")
    declaration = next(
        (item for item in builtin_runtime_context_transform_declarations() if item.provider_id == value["name"]),
        None,
    )
    if declaration is None:
        raise ValueError(f"unknown builtin context component: {value['name']}")
    return ValidatedConfig(
        value=declaration,
        snapshot={"name": declaration.provider_id, "version": declaration.provider_version},
    )


def _builtin_context_component_factory(value: object, _context: object) -> object:
    if not isinstance(value, RuntimeContextTransformDeclaration):
        raise ValueError("builtin context factory requires its admitted declaration")
    return materialize_builtin_context_transform(value.provider_id)


def _builtin_runtime_package(root: Path, provider_names: Iterable[str]) -> BuiltinDeclaration:
    components = [
        ComponentDeclaration(
            slot="provider",
            name=name,
            factory="voidcode.runtime.service:_builtin_provider_component_factory",
            validate_config="voidcode.runtime.service:_builtin_provider_component_config",
        )
        for name in sorted(set(provider_names))
    ]
    components.extend(
        ComponentDeclaration(
            slot="tool",
            name=definition.name,
            factory="voidcode.runtime.service:_builtin_tool_component_factory",
            validate_config="voidcode.runtime.service:_builtin_tool_component_config",
        )
        for definition in builtin_tool_definitions()
    )
    components.extend(
        ComponentDeclaration(
            slot="agent",
            name=str(manifest.id),
            factory="voidcode.runtime.service:_builtin_agent_component_factory",
            validate_config="voidcode.runtime.service:_builtin_agent_component_config",
        )
        for manifest in list_builtin_agent_manifests()
    )
    components.append(
        ComponentDeclaration(
            slot="agent",
            name="declarative",
            factory="voidcode.runtime.service:_builtin_agent_component_factory",
            validate_config="voidcode.runtime.service:_builtin_agent_component_config",
        )
    )
    components.extend(
        ComponentDeclaration(
            slot="context",
            name=declaration.provider_id,
            factory="voidcode.runtime.service:_builtin_context_component_factory",
            validate_config="voidcode.runtime.service:_builtin_context_component_config",
        )
        for declaration in builtin_runtime_context_transform_declarations()
    )
    return BuiltinDeclaration(
        declaration=PackageDeclaration(
            schema_version=1,
            files=(
                "voidcode/agent/builtin.py",
                "voidcode/agent/models.py",
                "voidcode/command/models.py",
                "voidcode/hook/before_agent_start.py",
                "voidcode/hook/typed.py",
                "voidcode/provider/registry.py",
                "voidcode/runtime/context/transforms.py",
                "voidcode/runtime/service.py",
                "voidcode/runtime/tools/builtins.py",
            ),
            components=tuple(components),
        ),
        root=root,
    )


MAX_REVIEW_DIFF_BYTES: Final[int] = 256 * 1024


def _validate_prompt_slash_command(prompt: str) -> None:
    stripped = prompt.strip()
    if not stripped.startswith("/") or stripped == "/":
        return
    token = stripped[1:].split(maxsplit=1)[0]
    if "/" in token:
        return
    if is_native_command(token):
        raise RuntimeRequestError(f"native command /{token} requires interactive dispatch")
    raise RuntimeRequestError(f"unrecognized command '/{token}'")


@final
class VoidCodeRuntime(RuntimeSurface):
    """Headless runtime entrypoint for a local request."""

    _workspace: Path
    _base_tool_registry: ToolRegistry
    _tool_materializer: RuntimeToolMaterializer
    _tool_materialization: RuntimeToolMaterialization
    _turn_producer: TurnProducer | None
    _turn_producer_override: TurnProducer | None
    _config: RuntimeConfig
    _initial_config: RunConfig
    _permission_policy: PermissionPolicy
    _repositories: RuntimeRepositories
    _model_provider_registry: ModelProviderRegistry
    _composition_owner: CompositionOwner
    _provider_model: ResolvedProviderModel
    _provider_chain: ResolvedProviderChain
    _provider_auth_resolver: ProviderAuthResolver
    _skill_registry: SkillRegistry
    _skill_registry_is_injected: bool
    _lsp_manager: LspManager
    _mcp_manager: McpManager
    _mcp_manager_is_injected: bool
    _acp_adapter: AcpAdapter
    _context_window_config_override: RuntimeContextWindowConfig | None
    _agent_registry: AgentManifestRegistry
    _run_loop_coordinator: RuntimeRunLoopCoordinator
    _resume_coordinator: RuntimeResumeCoordinator
    _stream_prep_coordinator: StreamPrepCoordinator
    _finalize_coordinator: FinalizeCoordinator
    _background_task_supervisor: RuntimeBackgroundTaskSupervisor
    _background_process_manager: BackgroundProcessManager
    _context_transform_registry: RuntimeContextTransformRegistry
    _before_agent_start_registry: BeforeAgentStartRegistry
    _activated_before_agent_start_registries: dict[CompositionRef, BeforeAgentStartRegistry]
    _installed_agent_distributions: dict[str, str]
    _default_context_window_policy = ContextWindowPolicy()

    def __init__(
        self,
        *,
        workspace: Path,
        tool_registry: ToolRegistry | None = None,
        turn_producer: TurnProducer | None = None,
        config: RuntimeConfig | None = None,
        permission_policy: PermissionPolicy | None = None,
        repositories: RuntimeRepositories | None = None,
        model_provider_registry: ModelProviderRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
        lsp_manager: LspManager | None = None,
        mcp_manager: McpManager | None = None,
        acp_adapter: AcpAdapter | None = None,
        context_window_policy: ContextWindowPolicy | None = None,
        context_transform_registry: RuntimeContextTransformRegistry | None = None,
        tool_input_handler_registry: ToolInputHandlerRegistry | None = None,
        before_agent_start_registry: BeforeAgentStartRegistry | None = None,
        composition_owner: CompositionOwner | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._permission_context_resolver = RuntimePermissionContextResolver(workspace=self._workspace)
        self._config = config or load_runtime_config(self._workspace)
        self._bind_tool_scope_resolver()
        self._permission_engine = PermissionEngine(
            _context_resolver=self._permission_context_resolver,
            _permission_config=self._config.permission,
            _patch_path_extractor=extract_paths_from_patch,
        )
        self._model_provider_registry = model_provider_registry or ModelProviderRegistry.with_defaults(provider_configs=self._config.providers)
        builtin_pkg = _builtin_runtime_package(
            Path(__file__).resolve().parents[2],
            self._model_provider_registry.descriptors,
        )
        if composition_owner is not None:
            self._composition_owner = composition_owner
            if "voidcode" not in self._composition_owner._builtins:
                self._composition_owner._builtins["voidcode"] = builtin_pkg
        else:
            self._composition_owner = CompositionOwner(builtins=(builtin_pkg,))
        self._installed_agent_distributions = {}
        installed_agent_manifests: list[AgentManifest] = []
        self._installed_tool_definitions: list[ToolDefinition] = []
        installed_context_transforms: list[RuntimeContextTransformDeclaration] = []
        installed_typed_inputs: list[ToolInputHandlerDeclaration] = []
        installed_before_agent_start: list[BeforeAgentStartDeclaration] = []
        if self._config.components:
            preflight = self._composition_owner.prepare(self._config.components, intent={"component_preflight": True})
            for component in preflight.binding.components:
                if component.distribution == "voidcode":
                    continue
                validated = self._composition_owner.validated_value(preflight, component.key)
                if component.slot == "provider":
                    if not isinstance(validated, ProviderDescriptor) or validated.provider_name != component.name:
                        raise ValueError("installed provider component validator returned a mismatched descriptor")
                    self._model_provider_registry.register(validated)
                elif component.slot == "tool":
                    if not isinstance(validated, ToolDefinition) or validated.name != component.name:
                        raise ValueError("installed tool component validator returned a mismatched definition")
                    self._installed_tool_definitions.append(validated)
                elif component.slot == "agent":
                    if not isinstance(validated, AgentManifest) or (validated.id != component.name and validated.name != component.name):
                        raise ValueError("installed agent component validator returned a mismatched manifest")
                    installed_agent_manifests.append(validated)
                    self._installed_agent_distributions[component.name] = component.distribution
                    self._installed_agent_distributions[validated.id] = component.distribution
                elif component.slot == "context":
                    if not isinstance(validated, RuntimeContextTransformDeclaration) or validated.provider_id != component.name:
                        raise ValueError("installed context component validator returned a mismatched declaration")
                    installed_context_transforms.append(validated)
                elif component.slot == "typed-input":
                    if not isinstance(validated, ToolInputHandlerDeclaration) or validated.name != component.name:
                        raise ValueError("installed typed-input component validator returned a mismatched declaration")
                    installed_typed_inputs.append(validated)
                elif component.slot == "before-agent-start":
                    if not isinstance(validated, BeforeAgentStartDeclaration) or validated.name != component.name:
                        raise ValueError("installed before-agent-start component validator returned a mismatched declaration")
                    installed_before_agent_start.append(validated)
        self._agent_registry = self._runtime_agent_registry(installed_manifests=installed_agent_manifests)
        self._installed_context_transforms = tuple(installed_context_transforms)
        self._installed_typed_inputs = tuple(installed_typed_inputs)
        self._before_agent_start_registry = before_agent_start_registry or BeforeAgentStartRegistry()
        for declaration in installed_before_agent_start:
            self._before_agent_start_registry.register(declaration)
        self._activated_provider_bindings: dict[CompositionRef, tuple[ResolvedProviderConfig, BoundProviderConfig]] = {}
        self._active_compositions: dict[CompositionRef, ActiveComposition] = {}
        self._activated_context_transform_registries: dict[CompositionRef, RuntimeContextTransformRegistry] = {}
        self._activated_tool_input_registries: dict[CompositionRef, ToolInputHandlerRegistry] = {}
        self._activated_before_agent_start_registries: dict[CompositionRef, BeforeAgentStartRegistry] = {}
        self._bind_provider_catalog_collaborators()
        self._provider_summary_projector = ProviderSummaryProjector()
        self._hydrate_provider_model_catalog_cache()
        initial_agent = self._config.agent
        if initial_agent is None:
            initial_agent = RuntimeAgentConfig(preset="default")
        if initial_agent is not None:
            initial_agent = parse_runtime_agent_payload(
                serialize_runtime_agent_config(initial_agent, include_runtime_internal=True),
                source="runtime config agent",
                hooks=self._config.hooks,
                agent_registry=self._agent_registry,
                allow_runtime_internal=True,
            )
            assert initial_agent is not None
            self._validate_runtime_agent_for_execution(
                initial_agent,
                source="runtime config agent",
            )
        initial_model = initial_agent.model if initial_agent is not None and initial_agent.model is not None else self._config.model
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
        self._mcp_manager = mcp_manager or build_mcp_manager(
            self._config.mcp,
            tool_catalog_cache=McpToolCatalogCache(path=mcp_tool_catalog_cache_path()),
        )
        self._skill_lock = threading.Lock()
        self._skill_registry_is_injected = skill_registry is not None
        self._skill_registry = skill_registry or StreamPrepCoordinator.build_skill_registry_for_workspace(self._workspace, self._config.skills)
        base_registry = tool_registry or self._build_base_tool_registry()
        if self._installed_tool_definitions:
            all_definitions = (*base_registry.definitions(), *self._installed_tool_definitions)
            base_registry = ToolRegistry.from_definitions(all_definitions)
            if tool_registry:
                base_registry.tools.update(tool_registry.tools)
        self._base_tool_registry = base_registry
        installed_tool_names = frozenset(d.name for d in self._installed_tool_definitions)
        base_provenance = tuple(
            tool_provenance(
                definition,
                source_kind=(
                    "package"
                    if definition.name in installed_tool_names
                    else ("base" if definition.name not in self._base_tool_registry.tools else "package")
                ),
                source_id=(
                    f"package:{definition.name}"
                    if definition.name in installed_tool_names
                    else (
                        f"base:{definition.name}"
                        if definition.name not in self._base_tool_registry.tools
                        else (
                            f"{type(self._base_tool_registry.tools[definition.name]).__module__}:"
                            f"{type(self._base_tool_registry.tools[definition.name]).__qualname__}"
                        )
                    )
                ),
            )
            for definition in self._base_tool_registry.definitions()
        )
        self._tool_materializer = RuntimeToolMaterializer(self._base_tool_registry, base_provenance)
        self._tool_materialization = self._tool_materializer.base()
        self._turn_producer_override = turn_producer
        self._context_window_config_override = context_window_config_from_policy(context_window_policy)
        initial_context_window = self._context_window_config_override or self._config.context_window
        self._initial_config = RunConfig(
            approval_mode=self._config.approval_mode,
            permission=self._config.permission,
            model=initial_model,
            tool_timeout_seconds=self._config.tool_timeout_seconds,
            provider_fallback=initial_provider_fallback,
            providers=self._config.providers,
            resolved_provider=self._resolved_provider_config,
            agent=initial_agent,
            context_window=initial_context_window,
            tools=self._config.tools,
            policy=self._config.policy,
            reminders=self._config.reminders,
            components=self._config.components,
        )
        self._permission_policy = permission_policy or PermissionPolicy(mode=self._config.approval_mode)
        if repositories is None:
            sqlite_store = SqliteSessionStore()
            repositories = RuntimeRepositories(
                events=sqlite_store,
                sessions=sqlite_store,
                run_writer=sqlite_store,
                recovery=sqlite_store,
                tasks=sqlite_store,
                maintenance=sqlite_store,
                process_persistence=sqlite_store,
            )
        self._repositories = repositories
        self._acp_adapter = acp_adapter or build_acp_adapter(self._config.acp)
        self._context_transform_registry = context_transform_registry or RuntimeContextTransformRegistry.from_declarations(
            builtin_runtime_context_transform_declarations()
        ).bind(materialize_builtin_context_transform)
        for declaration in self._installed_context_transforms:
            self._context_transform_registry.register(declaration)
        self._tool_input_handler_registry = tool_input_handler_registry or ToolInputHandlerRegistry()
        for declaration in self._installed_typed_inputs:
            self._tool_input_handler_registry.register(declaration)
        self._default_context_window_policy = context_window_policy_from_config(initial_context_window)
        self._background_task_supervisor = RuntimeBackgroundTaskSupervisor(
            self,
            events=self._repositories.events,
            sessions=self._repositories.sessions,
            run_writer=self._repositories.run_writer,
            recovery=self._repositories.recovery,
            tasks=self._repositories.tasks,
            workspace=self._workspace,
            config=self._config,
            acp_adapter=self._acp_adapter,
        )
        self._run_loop_coordinator = RuntimeRunLoopCoordinator(
            self,
            events=self._repositories.events,
            sessions=self._repositories.sessions,
            recovery=self._repositories.recovery,
            workspace=self._workspace,
            config=self._config,
            permission_policy=self._permission_policy,
            acp_adapter=self._acp_adapter,
            mcp_manager=self._mcp_manager,
            lsp_manager=self._lsp_manager,
            provider_catalog_query=self._provider_catalog_query,
            tool_input_handler_registry=self._tool_input_handler_registry,
            tool_executor=RuntimeToolExecutor(
                workspace=self._workspace,
                lsp=WorkspaceDiagnostics(self._workspace, self.request_diagnostics),
                lsp_diagnostics_on_write=bool(self._config.lsp is not None and self._config.lsp.diagnostics_on_write),
                tool_catalog=ToolCatalogReader(self.tool_catalog_lookup),
                read_artifact=self.read_tool_output_artifact,
                read_transcript=self.read_tool_transcript,
                read_rule=read_rule_uri,
                resolve_skill=self._resolve_skill,
                lsp_request=self.request_lsp,
                lsp_document_state=self._lsp_manager.document_state,
                mcp_request=self.request_mcp_tool,
                task_command=TaskCommand(runtime=self, substrate=self._background_task_supervisor).invoke,
                task_batch_command=TaskBatchCommand(runtime=self, substrate=self._background_task_supervisor).invoke,
                process_command=BackgroundProcessCommand(runtime=self).invoke,
            ),
        )
        self._resume_coordinator = RuntimeResumeCoordinator(
            self,
            events=self._repositories.events,
            sessions=self._repositories.sessions,
            recovery=self._repositories.recovery,
            workspace=self._workspace,
            config=self._config,
            permission_policy=self._permission_policy,
            acp_adapter=self._acp_adapter,
            mcp_manager=self._mcp_manager,
            background_task_supervisor=self._background_task_supervisor,
            run_loop_coordinator=self._run_loop_coordinator,
        )
        self._inspection_coordinator = InspectionCoordinator(
            self,
            events=self._repositories.events,
            sessions=self._repositories.sessions,
            run_writer=self._repositories.run_writer,
            recovery=self._repositories.recovery,
            tasks=self._repositories.tasks,
            maintenance=self._repositories.maintenance,
            workspace=self._workspace,
            config=self._config,
            lsp_manager=self._lsp_manager,
            mcp_manager=self._mcp_manager,
            acp_adapter=self._acp_adapter,
            model_provider_registry=self._model_provider_registry,
            provider_catalog_cache=self._provider_catalog_cache,
            provider_catalog_query=self._provider_catalog_query,
            provider_summary_projector=self._provider_summary_projector,
            provider_auth_inspector=self._provider_auth_inspector,
            provider_auth_resolver=self._provider_auth_resolver,
            skill_registry=self._skill_registry,
            agent_registry=self._agent_registry,
            background_task_supervisor=self._background_task_supervisor,
            resolved_provider_config=self._resolved_provider_config,
            provider_model=self._provider_model,
            load_background_task=self.load_background_task,
            refresh_mcp_tools=self._refresh_mcp_tools,
            admit_composition=self._composition_owner.refresh,
            is_active_session=self._is_active_session_id,
            active_session_metadata=self._active_session_metadata,
        )
        self._stream_prep_coordinator = StreamPrepCoordinator(
            self,
            default_context_window_policy=self._default_context_window_policy,
            config_with_request_agent_override=self._config_with_request_agent_override,
            context_transform_registry_for_agent=self._context_transform_registry_for_agent,
            workspace=self._workspace,
            global_skills_config=self._config.skills,
            skill_registry=self._skill_registry if self._skill_registry_is_injected else None,
            skill_registry_is_injected=self._skill_registry_is_injected,
            mcp_manager=self._mcp_manager,
            mcp_manager_is_injected=self._mcp_manager_is_injected,
            turn_producer_override_present=lambda: self._turn_producer_override is not None,
        )
        self._finalize_coordinator = FinalizeCoordinator(
            self,
            events=self._repositories.events,
            sessions=self._repositories.sessions,
            run_writer=self._repositories.run_writer,
            recovery=self._repositories.recovery,
            workspace=self._workspace,
            config=self._config,
            acp_adapter=self._acp_adapter,
            mcp_manager=self._mcp_manager,
            background_task_supervisor=self._background_task_supervisor,
            is_active_session=self._is_active_session_id,
            active_run_count=lambda session_id: ACTIVE_SESSION_REGISTRY.active_run_count(
                workspace=self._workspace,
                session_id=session_id,
            ),
        )
        self._background_process_manager = BackgroundProcessManager(
            persistence=self._repositories.process_persistence,
            workspace=self._workspace,
        )

    @property
    def workspace(self) -> Path:
        """The canonical workspace directory this runtime manages."""
        return self._workspace

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
        self._tool_scope_resolver = RuntimeToolScopeResolver()

    def __enter__(self) -> VoidCodeRuntime:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = exc_type, exc, tb
        # Shutdown drain order (must be preserved):
        #   1. Drain background-task workers FIRST — ``shutdown_background_tasks``
        #      joins every live worker so each child/background-task result is
        #      durably finalized (task row terminal state, parent-session
        #      notification events, lifecycle hooks) before anything is torn
        #      down, and, for anything that could not finish, revokes its
        #      execution ownership first (the still-live worker can then no
        #      longer write task state, child-session events or parent
        #      notifications) before terminalizing its task row
        #      (``interrupted`` for keep-alive tasks, ``failed`` otherwise).
        #      See docs/contracts/task-delegation.md.
        #   2. Stop spawned background processes.
        #   3. Tear down ACP/MCP/LSP adapters LAST — MCP connections are
        #      runtime-scoped and shared across sessions (never released at
        #      session/run end), so adapter shutdown is purely a client-surface
        #      close and must not race the durable session writes above.
        self.shutdown_background_tasks()
        self._background_process_manager.stop_all()
        _ = self.disconnect_acp()
        _ = self.shutdown_mcp()
        _ = self.shutdown_lsp()

    def shutdown_background_tasks(self, *, timeout_seconds: float = 2.0) -> None:
        """Drain background-task workers before runtime teardown.

        Enforced ordering inside ``RuntimeBackgroundTaskSupervisor.shutdown``:
        set the shutdown flag, join every live worker (each worker finalizes
        its task durably before exiting), then seize ownership of any worker
        that could not finish within the timeout — revoke its execution lease,
        then mark its task row terminal (``interrupted`` for keep-alive tasks,
        ``failed`` otherwise). After this returns, every task row is terminal,
        the results that finished in time are durable, and seized workers can
        no longer write runtime truth from their own thread (tool threads
        inherit the caller's lease, so tool-side commits are covered too); a
        manager-owned thread that persists on its own is outside this guarantee
        — see the boundary invariant in
        docs/contracts/task-delegation.md.
        """
        self._background_task_supervisor.shutdown(timeout_seconds=timeout_seconds)

    def tool_catalog_lookup(self, tool_name: str) -> ToolDefinition | None:
        """Read-only registry lookup for on-demand tool documentation.

        Resolves against the current materialization (base + MCP + local tools)
        so doc reads stay consistent with the live registry even after MCP
        refreshes.
        """
        return self._tool_materialization.registry.definition(tool_name)

    def _build_base_tool_registry(self) -> ToolRegistry:
        definitions = builtin_tool_definitions()
        if self._lsp_manager is None or self._lsp_manager.current_state().mode != "managed":
            definitions = tuple(definition for definition in definitions if definition.name != "lsp")
        return ToolRegistry.from_definitions(definitions)

    @property
    def background_process_manager(self) -> BackgroundProcessManager:
        return self._background_process_manager

    def _tool_materialization_with_effective_local_tools(
        self,
        config: RunConfig,
    ) -> RuntimeToolMaterialization:
        local_config = config.tools.local if config.tools is not None else None
        manifests = workspace_local_tool_manifests_provider(self._workspace)(local_config)
        return materialize_unscoped(
            config,
            materialization=self._tool_materialization,
            materializer=self._tool_materializer,
            local_tool_manifests_provider=lambda _: manifests,
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
            emitted, updated_session, last_sequence = emit_current_acp_drain(
                self._acp_adapter,
                session=session,
                start_sequence=sequence + 1,
            )
            failed_session = session_with_current_acp_metadata(updated_session, self._acp_adapter.current_state())
            failed_chunk = chunk_builders.failed_chunk(
                session=failed_session,
                sequence=last_sequence + 1,
                error=str(exc),
                payload={"kind": "acp_startup_failed"},
            )
            return emitted, failed_session, last_sequence + 1, failed_chunk
        emitted, updated_session, last_sequence = emit_acp_events(
            self._acp_adapter,
            session=session,
            start_sequence=sequence + 1,
            acp_events=acp_events,
        )
        if not emitted:
            updated_session = session_with_current_acp_metadata(updated_session, self._acp_adapter.current_state())
        return emitted, updated_session, last_sequence or sequence, None

    def run_config_for_request(self, request: RuntimeRequest) -> RunConfig:
        return self._stream_prep_coordinator.run_config_for_request(request)

    def _provider_component_config(self, provider_name: str) -> dict[str, object]:
        payload = serialize_provider_configs(self._config.providers, include_secrets=False) or {}
        wire = PROVIDER_WIRES.get(provider_name)
        if wire is not None:
            selected = {wire.payload_key: payload[wire.payload_key]} if wire.payload_key in payload else {}
        else:
            custom = payload.get("custom")
            selected = {"custom": {provider_name: custom[provider_name]}} if isinstance(custom, dict) and provider_name in custom else {}
        return {"provider_name": provider_name, "providers": selected}

    def prepare_execution_composition(self, config: RunConfig) -> FrozenComposition:
        return self._prepare_execution_composition(config)

    def _prepare_execution_composition(self, config: RunConfig) -> FrozenComposition:
        selections = list(config.components)
        selected = {(item.distribution, item.slot, item.name) for item in selections}
        package_providers = {item.name for item in selections if item.slot == "provider"}

        def add_builtin(slot: ComponentSlot, name: str, configuration: Mapping[str, object]) -> None:
            identity = ("voidcode", slot, name)
            if identity not in selected:
                selections.append(
                    ComponentSelection(
                        distribution="voidcode",
                        slot=slot,
                        name=name,
                        configuration=configuration,
                        provenance=("run_config",),
                    )
                )
                selected.add(identity)

        for definition in builtin_tool_definitions():
            add_builtin("tool", definition.name, {"name": definition.name})
        for declaration in builtin_runtime_context_transform_declarations():
            add_builtin("context", declaration.provider_id, {"name": declaration.provider_id})
        if config.agent is not None and config.agent.preset is not None:
            agent_name = str(config.agent.preset)
            if agent_name not in {item.name for item in selections if item.slot == "agent"}:
                manifest = self._agent_registry.get(agent_name)
                if manifest is None:
                    raise ValueError(f"unknown agent preset: '{agent_name}'")
                if manifest.source_scope == "package":
                    distribution = self._installed_agent_distributions.get(agent_name)
                    if distribution is None:
                        raise ValueError(f"installed agent '{agent_name}' has no admitted package component in this runtime")
                    identity = (distribution, "agent", agent_name)
                    if identity not in selected:
                        selections.append(
                            ComponentSelection(
                                distribution=distribution,
                                slot="agent",
                                name=agent_name,
                                configuration={},
                                provenance=("run_config",),
                            )
                        )
                        selected.add(identity)
                elif manifest.source_scope in ("project", "user"):
                    add_builtin("agent", "declarative", manifest.to_payload())
                else:
                    add_builtin("agent", agent_name, manifest.to_payload())

        selected_provider_names: set[str] = set()
        for target in config.resolved_provider.target_chain.all_targets:
            descriptor = target.descriptor
            if descriptor is None:
                raise ValueError("selected provider target has no admitted static descriptor")
            if descriptor.provider_name in selected_provider_names:
                continue
            selected_provider_names.add(descriptor.provider_name)
            if descriptor.provider_name in package_providers:
                continue
            add_builtin(
                "provider",
                descriptor.provider_name,
                self._provider_component_config(descriptor.provider_name),
            )
        return self._composition_owner.prepare(
            selections,
            intent={
                "resolved_provider": resolved_provider_snapshot(config.resolved_provider),
                "provider_target_metadata": [
                    target.metadata.payload() if target.metadata is not None else None for target in config.resolved_provider.target_chain.all_targets
                ],
            },
        )

    def _activate_execution_composition(
        self,
        prep: _PreparedStreamSession,
    ) -> RunConfig:
        return self._activate_frozen_composition(
            prep.frozen_composition,
            composition_ref=prep.composition_ref,
            config=prep.config,
        )

    def _activate_frozen_composition(
        self,
        frozen: FrozenComposition,
        *,
        composition_ref: CompositionRef,
        config: RunConfig,
    ) -> RunConfig:
        if composition_ref.workspace != str(self._workspace):
            raise ValueError("composition reference names a different workspace")
        active = self._composition_owner.activate(
            frozen,
            read_persisted=lambda: self._repositories.recovery.load_execution_composition(ref=composition_ref),
        )
        descriptors: dict[str, ProviderDescriptor] = {}
        component_keys: dict[str, str] = {}
        context_providers = list(self._context_transform_registry.providers)
        tool_input_bindings = list(self._tool_input_handler_registry.bindings)
        before_agent_start_registry = BeforeAgentStartRegistry(
            bindings=self._before_agent_start_registry.bindings,
            declarations=self._before_agent_start_registry.declarations.values(),
        )
        for component in frozen.binding.components:
            if component.slot == "provider":
                descriptor = active.admitted_value(component.key)
                if not isinstance(descriptor, ProviderDescriptor) or descriptor.provider_name != component.name:
                    raise ValueError("activated provider component does not match its frozen declaration")
                descriptors[component.name] = descriptor
                component_keys[component.name] = component.key
            elif component.slot == "command":
                continue
            elif component.distribution != "voidcode":
                instance = active.construct(component.key, context=composition_ref)
                if component.slot == "tool":
                    if not callable(getattr(instance, "invoke", None)):
                        raise ValueError(f"activated tool component does not implement invoke: {component.name}")
                elif component.slot == "context":
                    if getattr(instance, "provider_id", None) != component.name or not callable(getattr(instance, "build_result", None)):
                        raise ValueError(f"activated context component does not match declaration: {component.name}")
                    context_providers.append(cast(RuntimeContextTransformProvider, instance))
                elif component.slot == "typed-input":
                    if not isinstance(instance, ToolInputHandlerBinding) or instance.declaration.name != component.name:
                        raise ValueError(f"activated typed-input component must be a ToolInputHandlerBinding for {component.name}")
                    tool_input_bindings.append(instance)
                elif component.slot == "before-agent-start":
                    if not isinstance(instance, BeforeAgentStartBinding) or instance.declaration.name != component.name:
                        raise ValueError(f"activated before-agent-start component must bind {component.name}")
                    before_agent_start_registry.bind_handler(instance)
        self._activated_context_transform_registries[composition_ref] = RuntimeContextTransformRegistry(
            providers=context_providers,
            declarations=self._context_transform_registry.declarations.values(),
        )
        self._activated_tool_input_registries[composition_ref] = ToolInputHandlerRegistry(
            bindings=tool_input_bindings,
            declarations=self._tool_input_handler_registry.declarations.values(),
        )
        self._activated_before_agent_start_registries[composition_ref] = before_agent_start_registry
        self._tool_materialization = self._tool_materializer.base()
        resolved = self._resolved_provider_from_composition(frozen, descriptors)

        def _materialize_provider(descriptor: ProviderDescriptor) -> ModelTurnProvider:
            if descriptor.provider_name in self._model_provider_registry.providers:
                return self._model_provider_registry.providers[descriptor.provider_name]
            return cast(ModelTurnProvider, active.construct(component_keys[descriptor.provider_name], context=composition_ref))

        bound = self._model_provider_registry.bind(
            resolved,
            materialize=_materialize_provider,
        )
        self._activated_provider_bindings[composition_ref] = (resolved, bound)
        self._active_compositions[composition_ref] = active
        return replace(config, resolved_provider=resolved, bound_provider=bound)

    def _activate_existing_session_composition(self, session_id: str) -> RunConfig:
        stored = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        metadata = json_wire_object(stored.session.metadata)
        composition_ref = composition_ref_from_session_metadata(metadata)
        frozen = self._repositories.recovery.load_execution_composition(ref=composition_ref)
        return self._activate_frozen_composition(
            frozen,
            composition_ref=composition_ref,
            config=self.run_config_from_metadata(metadata),
        )

    def active_composition(self, *, session_id: str) -> ActiveComposition:
        if not session_id:
            raise ValueError("active composition requires a non-empty session identity")
        stored = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        ref = composition_ref_from_session_metadata(stored.session.metadata)
        if ref.workspace != str(self._workspace):
            raise ValueError("composition reference names a different workspace")
        if isinstance(ref.owner, SessionCompositionOwner):
            if ref.owner.session_id != session_id:
                raise ValueError("composition owner does not match the active session")
        else:
            task = self._repositories.tasks.load_background_task(workspace=self._workspace, task_id=ref.owner.task_id)
            if task.session_id != session_id:
                raise ValueError("composition task owner does not match the active session")
        if ref in self._active_compositions:
            return self._active_compositions[ref]
        frozen = self._repositories.recovery.load_execution_composition(ref=ref)
        active = self._composition_owner.activate(
            frozen,
            read_persisted=lambda: self._repositories.recovery.load_execution_composition(ref=ref),
        )
        self._active_compositions[ref] = active
        return active

    def run_composition_phase(
        self,
        name: str,
        value: object,
        *,
        scope: LifecycleScope,
        scope_id: str,
        session_id: str,
        context: PhaseContext,
        recovering: bool = False,
        restore_only: bool = False,
    ) -> tuple[PhaseOutcome, ...]:
        if not session_id or not scope_id:
            raise ValueError("run_composition_phase requires non-empty session_id and scope_id")
        if (
            context.workspace != self._workspace
            or context.session_id != session_id
            or context.scope != scope
            or context.scope_id != scope_id
            or not context.run_id
        ):
            raise ValueError("phase context does not match its authentic runtime scope")
        active = self.active_composition(session_id=session_id)

        def read_record(key: str) -> PhaseRecord | None:
            records = self.phase_records(session_id)
            if key not in records:
                return None
            return PhaseRecord.model_validate(records[key])

        def write_record(record: PhaseRecord) -> None:
            self._repositories.sessions.save_composition_phase_record(
                workspace=self._workspace,
                session_id=session_id,
                record=record,
            )

        return active.run_phase(
            name,
            value,
            scope=scope,
            scope_id=scope_id,
            context=context,
            read_record=read_record,
            write_record=write_record,
            recovering=recovering,
            restore_only=restore_only,
        )

    def phase_records(self, session_id: str, metadata: Mapping[str, object] | None = None) -> dict[str, dict[str, object]]:
        if not session_id:
            raise ValueError("phase records require a non-empty session identity")
        records_source = metadata.get("phase_records") if metadata is not None else None
        if records_source is None:
            if metadata is not None:
                return {}
            stored = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
            if "phase_records" not in stored.session.metadata:
                return {}
            records_source = stored.session.metadata["phase_records"]
        if not isinstance(records_source, dict):
            raise ValueError("persisted phase_records mapping is malformed")
        frozen = self.active_composition(session_id=session_id).frozen
        phases = {phase.key: phase.declaration for phase in frozen.plan.phases}
        validated: dict[str, dict[str, object]] = {}
        for key, raw in records_source.items():
            if not isinstance(key, str) or not isinstance(raw, dict):
                raise ValueError("persisted phase_records entry is malformed")
            record = PhaseRecord.model_validate(raw)
            declaration = phases.get(record.phase_key)
            if (
                declaration is None
                or record.binding_id != frozen.binding.binding_id
                or record.version != declaration.version
                or record.scope != declaration.scope
            ):
                raise ValueError("persisted phase record does not match its frozen declaration")
            if key != f"{record.phase_key}:{record.scope}:{record.scope_id}":
                raise ValueError("persisted phase record key does not match its identity")
            validated[key] = record.model_dump(mode="json")
        return validated

    def composition_session_metadata(self, session_id: str, metadata: dict[str, object]) -> dict[str, object]:
        """Merge durable phase state and context projection into an older run/checkpoint snapshot."""
        stored = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        stored_meta = stored.session.metadata
        state: dict[str, object] = dict(parse_runtime_state_metadata(stored_meta["runtime_state"])) if "runtime_state" in stored_meta else {}
        continuity_and_phase_keys = frozenset(
            {
                "composition_turn_occurrence",
                "composition_turn",
                "context_projection",
                "context_projection_summary",
                "context_compacted",
                "reminders",
            }
        )
        persisted_updates = {key: state[key] for key in continuity_and_phase_keys if key in state}
        phase_keys = frozenset({"composition_turn_occurrence", "composition_turn"})
        merged = session_metadata_with_runtime_state_updates(metadata, updates=persisted_updates, removed=phase_keys - state.keys())
        if "context_window" in stored_meta:
            merged["context_window"] = stored_meta["context_window"]
        if "collaboration_mode" in stored_meta:
            merged["collaboration_mode"] = stored_meta["collaboration_mode"]
        stored_goal = stored_meta.get("thread_goal")
        incoming_goal = merged.get("thread_goal")
        if isinstance(stored_goal, dict) and isinstance(incoming_goal, dict):
            stored_ts = cast(int, stored_goal.get("updated_at", 0))
            incoming_ts = cast(int, incoming_goal.get("updated_at", 0))
            merged["thread_goal"] = stored_goal if stored_ts > incoming_ts else incoming_goal
        elif isinstance(stored_goal, dict):
            merged["thread_goal"] = stored_goal
        return {**merged, "phase_records": self.phase_records(session_id, metadata=stored_meta)}

    def _background_task_composition_ref(self, *, task_id: str, session_id: str) -> CompositionRef:
        task = self._repositories.tasks.load_background_task(workspace=self._workspace, task_id=task_id)
        if task.session_id != session_id:
            raise ValueError("background task composition owner does not name the dispatched session")
        ref = CompositionRef.model_validate(task.request.metadata.get("composition_ref"))
        if ref.workspace != str(self._workspace):
            raise ValueError("background task composition reference names a different workspace")
        return ref

    def _admit_existing_session_composition(self, session_id: str) -> FrozenComposition:
        stored = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        stored_metadata = json_wire_object(stored.session.metadata)
        checkpoint = self._repositories.recovery.load_resume_checkpoint(
            workspace=self._workspace,
            session_id=session_id,
        )
        if checkpoint is not None:
            kind = checkpoint.get("kind")
            if not isinstance(kind, str) or kind not in {
                "approval_wait",
                "interrupted",
                "provider_failure_retryable",
                "question_wait",
                "terminal",
            }:
                raise ValueError("persisted resume checkpoint kind is unsupported")
            validated_resume_checkpoint_envelope(checkpoint=checkpoint, expected_kind=kind)
        elif stored.session.status in {"interrupted", "waiting"}:
            raise ValueError("persisted resume checkpoint is required")
        if not isinstance(stored_metadata.get("agent_capability_snapshot"), dict):
            raise RuntimeRequestError(
                "session cannot be resumed: its checkpoint records no agent capability snapshot, "
                "so the runtime cannot replay the capabilities (provider, model, tools, skills) that run was bound to"
            )
        ref = composition_ref_from_session_metadata(stored_metadata)
        raw_task_id = stored_metadata.get("background_task_id")
        if raw_task_id is not None:
            if (
                not isinstance(raw_task_id, str)
                or self._background_task_composition_ref(
                    task_id=raw_task_id,
                    session_id=session_id,
                )
                != ref
            ):
                raise ValueError("persisted session composition reference does not match its background task")
        if ref.workspace != str(self._workspace):
            raise ValueError("persisted session composition reference names a different workspace")
        frozen = self._repositories.recovery.load_execution_composition(ref=ref)
        owner_payload = stored_metadata.get("execution_composition")
        if owner_payload is not None and owner_payload != frozen.to_payload():
            raise ValueError("session metadata does not match its canonical composition owner")
        if checkpoint is not None:
            checkpoint_metadata = checkpoint.get("session_metadata")
            if (
                not isinstance(checkpoint_metadata, dict)
                or verified_checkpoint_session_metadata(
                    checkpoint_metadata=checkpoint_metadata,
                    stored_metadata={**stored_metadata, "execution_composition": frozen.to_payload()},
                )
                is None
            ):
                raise ValueError("persisted session metadata does not match its resume checkpoint")
        phases = {phase.key: phase.declaration for phase in frozen.plan.phases}
        records = self.phase_records(session_id)
        for record_payload in records.values():
            record = PhaseRecord.model_validate(record_payload)
            if record.state == "started":
                declaration = phases.get(record.phase_key)
                if declaration is not None and declaration.replay in ("once", "refuse"):
                    raise ValueError(f"package lifecycle phase was interrupted in-flight; replay={declaration.replay!r} refuses duplicate execution")
        self._composition_owner.refresh(frozen)
        return frozen

    @staticmethod
    def _resolved_provider_from_composition(
        frozen: FrozenComposition,
        descriptors: Mapping[str, ProviderDescriptor],
    ) -> ResolvedProviderConfig:
        raw_snapshot = frozen.binding.intent.get("resolved_provider")
        if raw_snapshot is None:
            return ResolvedProviderConfig()
        if not isinstance(raw_snapshot, Mapping):
            raise ValueError("frozen execution composition has an invalid provider snapshot")
        raw_targets = raw_snapshot.get("targets")
        raw_active = raw_snapshot.get("active_target")
        metadata_items = frozen.binding.intent.get("provider_target_metadata")
        if not isinstance(raw_targets, (list, tuple)) or not isinstance(raw_active, Mapping) or not isinstance(metadata_items, (list, tuple)):
            raise ValueError("frozen execution composition is missing provider targets")
        if len(metadata_items) != len(raw_targets) or not raw_targets:
            raise ValueError("frozen execution composition provider metadata does not match its targets")
        targets: list[ResolvedProviderModel] = []
        for raw_target, raw_metadata in zip(raw_targets, metadata_items, strict=True):
            if not isinstance(raw_target, Mapping):
                raise ValueError("frozen provider target must be an object")
            raw_model, provider, model = (raw_target.get(key) for key in ("raw_model", "provider", "model"))
            if not all(isinstance(value, str) and value for value in (raw_model, provider, model)):
                raise ValueError("frozen provider target is incomplete")
            if split_provider_model_reference(raw_model) != (provider, model):
                raise ValueError("frozen provider target identity is inconsistent")
            descriptor = descriptors.get(provider)
            if descriptor is None:
                raise ValueError("frozen provider target has no activated owner")
            metadata = ProviderModelMetadata(**cast(dict[str, Any], json_wire_object(raw_metadata))) if isinstance(raw_metadata, Mapping) else None
            targets.append(
                ResolvedProviderModel(
                    selection=ProviderModelSelection(raw_model=raw_model, provider=provider, model=model),
                    descriptor=descriptor,
                    metadata=metadata,
                )
            )
        active_raw = raw_active.get("raw_model")
        active_index = next((index for index, target in enumerate(targets) if target.selection.raw_model == active_raw), None)
        if active_index is None:
            raise ValueError("frozen active provider target is outside its target chain")
        fallback = (
            ProviderFallbackConfig(
                preferred_model=cast(str, targets[0].selection.raw_model),
                fallback_models=tuple(cast(str, target.selection.raw_model) for target in targets[1:]),
            )
            if len(targets) > 1
            else None
        )
        return ResolvedProviderConfig(
            model=targets[0].selection.raw_model,
            provider_fallback=fallback,
            active_target=targets[active_index],
            target_chain=ResolvedProviderChain(preferred=targets[0], all_targets=tuple(targets)),
        )

    def skill_registry_for_config(
        self,
        config: RunConfig,
    ) -> SkillRegistry:
        return self._stream_prep_coordinator.skill_registry_for_config(config)

    def _list_mcp_descriptors(self) -> tuple[McpToolDescriptor, ...]:
        return self._stream_prep_coordinator.list_mcp_descriptors()

    def mcp_cached_surface(self) -> McpCachedToolSurface:
        return self._stream_prep_coordinator.mcp_cached_surface()

    def mcp_tools_available_for_run(
        self,
        *,
        request_metadata: Mapping[str, object],
        config: RunConfig,
    ) -> bool:
        return not (
            self.should_skip_mcp_startup_for_request()
            or StreamPrepCoordinator.is_background_child_mcp_deferred(
                request_metadata=request_metadata,
                config=config,
            )
        )

    def _refresh_mcp_tools(self) -> None:
        if self._mcp_manager.current_state().mode != "managed":
            self._tool_materialization = self._tool_materializer.base()
            return
        self._tool_materialization = self._tool_materializer.materialize_mcp_descriptors(self._list_mcp_descriptors())

    def materialize_mcp_tools_for_run(
        self,
        *,
        session: SessionState,
        sequence: int,
        request_metadata: Mapping[str, object],
        config: RunConfig,
        failure_kind: str,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int, RuntimeStreamChunk | None, tuple[McpToolDescriptor, ...]]:
        """Materialize the actual descriptor surface at run start."""
        if not self.mcp_tools_available_for_run(request_metadata=request_metadata, config=config):
            self._tool_materialization = self._tool_materializer.base()
            return (), session, sequence, None, ()
        if self._mcp_manager_is_injected:
            return self.refresh_mcp_tools_for_session(
                session=session,
                sequence=sequence,
                failure_kind=failure_kind,
            )
        surface = self.mcp_cached_surface()
        configured_servers = self._mcp_manager.current_state().configuration.servers
        if not surface.covers(configured_servers):
            return self.refresh_mcp_tools_for_session(
                session=session,
                sequence=sequence,
                failure_kind=failure_kind,
            )
        descriptors = self._materialize_cached_mcp_tools(surface)
        return (), session, sequence, None, descriptors

    def _materialize_cached_mcp_tools(self, surface: McpCachedToolSurface) -> tuple[McpToolDescriptor, ...]:
        """Advertise captured descriptors without connecting or binding adapters."""
        descriptors = surface.descriptors()
        self._tool_materialization = self._tool_materializer.materialize_mcp_descriptors(descriptors)
        return descriptors

    def tool_registry_for_run(
        self,
        *,
        session: SessionState,
        config: RunConfig,
        mcp_descriptors: tuple[McpToolDescriptor, ...],
        tool_materialization: RuntimeToolMaterialization | None = None,
        local_manifests: tuple[LocalCustomToolManifest, ...] | None = None,
    ) -> ToolRegistry:
        """Bind only descriptors captured for this activated run."""
        if local_manifests is None:
            local_config = config.tools.local if config.tools is not None else None
            local_manifests = workspace_local_tool_manifests_provider(self._workspace)(local_config)
        if tool_materialization is None:
            base = self._tool_materializer.materialize_mcp_descriptors(mcp_descriptors)
            tool_materialization = self._tool_materialization_for_config(
                config,
                session.metadata,
                materialization=base,
                local_manifests=local_manifests,
            )
        mcp_by_name = {mcp_tool_definition(item).name: item for item in mcp_descriptors}
        local_by_name = {local_custom_tool_definition(item).name: item for item in local_manifests}
        active_tools: dict[str, Tool] = {}
        if "composition_ref" in session.metadata or "agent_capability_snapshot" in session.metadata:
            try:
                ref = composition_ref_from_session_metadata(session.metadata)
                active = self._active_compositions.get(ref)
                if active is not None:
                    for comp in active.frozen.binding.components:
                        if comp.slot == "tool" and comp.key in active._instances:
                            active_tools[comp.name] = cast(Tool, active._instances[comp.key])
            except Exception:
                pass

        def materialize_definition(definition: ToolDefinition) -> Tool:
            if definition.name in BUILTIN_TOOL_NAMES:
                return materialize_builtin_tool(definition.name, hooks_config=self._config.hooks)
            descriptor = mcp_by_name.get(definition.name)
            if descriptor is not None:
                return McpTool(descriptor)
            manifest = local_by_name.get(definition.name)
            if manifest is not None:
                return LocalCustomTool(manifest)
            if definition.name in active_tools:
                return active_tools[definition.name]
            if definition.name in self._base_tool_registry.tools:
                return self._base_tool_registry.tools[definition.name]
            raise ValueError(f"no activated tool factory for declaration: {definition.name}")

        return tool_materialization.registry.bind(materialize_definition)

    def refresh_mcp_tools_for_session(
        self,
        *,
        session: SessionState,
        sequence: int,
        failure_kind: str,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int, RuntimeStreamChunk | None, tuple[McpToolDescriptor, ...]]:
        try:
            if self._mcp_manager.current_state().mode != "managed":
                self._tool_materialization = self._tool_materializer.base()
                return (), session, sequence, None, ()
            descriptors = self._list_mcp_descriptors()
            self._tool_materialization = self._tool_materializer.materialize_mcp_descriptors(descriptors)
        except Exception:
            logger.info(
                "continuing session %s after MCP tool refresh failure",
                session.session.id,
                extra={"failure_kind": failure_kind},
                exc_info=True,
            )
            emitted_events = envelopes_for_mcp_events(
                session_id=session.session.id,
                start_sequence=sequence + 1,
                mcp_events=self._mcp_manager.drain_events(),
            )
            emitted = tuple(RuntimeStreamChunk(kind="event", session=session, event=event) for event in emitted_events)
            last_sequence = emitted_events[-1].sequence if emitted_events else sequence
            return emitted, session, last_sequence, None, ()
        return (), session, sequence, None, descriptors

    def should_skip_mcp_startup_for_request(self) -> bool:
        return self._stream_prep_coordinator.should_skip_mcp_startup_for_request()

    def tool_registry_for_config(
        self,
        config: RunConfig,
        metadata: dict[str, object] | None = None,
    ) -> ToolRegistry:
        return self._tool_materialization_for_config(
            config,
            metadata,
        ).registry

    def provider_tool_definitions(
        self,
        tool_registry: ToolRegistry,
        config: RunConfig,
    ) -> tuple[ToolDefinition, ...]:
        """Provider-visible tool definitions for a request.

        With ``tools.essential_only`` enabled, only the essential tool set plus
        agent-allowlist-required tools are exposed top-level; discoverable
        tools stay registered and reachable via ``invoke_tool`` dispatch.
        Disabled (default) keeps the historical all-tools-top-level behavior.
        """
        if config.tools is None or config.tools.essential_only is not True:
            return tool_registry.definitions()
        return tool_registry.provider_definitions(
            allowlist_patterns=agent_required_tool_patterns(config.agent),
        )

    @staticmethod
    def _builtin_mcp_tool_names_for_materialization(materialization: RuntimeToolMaterialization) -> tuple[str, ...]:
        builtin_server_prefixes = tuple(f"mcp/{descriptor.name}/" for descriptor in list_builtin_mcp_descriptors())
        return tuple(
            item.tool_name for item in materialization.provenance if item.source_kind == "mcp" and item.tool_name.startswith(builtin_server_prefixes)
        )

    def _tool_materialization_for_config(
        self,
        config: RunConfig,
        metadata: dict[str, object] | None = None,
        *,
        materialization: RuntimeToolMaterialization | None = None,
        local_manifests: tuple[LocalCustomToolManifest, ...] | None = None,
        builtin_mcp_tool_names: tuple[str, ...] | None = None,
    ) -> RuntimeToolMaterialization:
        local_config = config.tools.local if config.tools is not None else None
        captured_manifests = workspace_local_tool_manifests_provider(self._workspace)(local_config) if local_manifests is None else local_manifests
        source_materialization = self._tool_materialization if materialization is None else materialization
        captured_builtin_mcp_names = (
            self._builtin_mcp_tool_names_for_materialization(source_materialization) if builtin_mcp_tool_names is None else builtin_mcp_tool_names
        )
        return materialize(
            config,
            materialization=source_materialization,
            materializer=self._tool_materializer,
            local_tool_manifests_provider=lambda _: captured_manifests,
            scope_resolver=self._tool_scope_resolver,
            metadata=metadata,
            builtin_mcp_tool_names=captured_builtin_mcp_names,
        )

    def tool_policy_denial(
        self,
        *,
        session: SessionState,
        tool_name: str,
    ) -> ToolPolicyDecision | None:
        config = self.run_config_from_metadata(session.metadata)
        materialization = self._tool_materialization_with_effective_local_tools(config)
        return self._tool_scope_resolver.denial(
            materialization.registry,
            agent=config.agent,
            metadata=session.metadata,
            tool_name=tool_name,
            builtin_mcp_tool_names=self._builtin_mcp_tool_names_for_materialization(materialization),
        )

    def delegation_tool_policy_error(
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
        config = self.run_config_from_metadata(session.metadata)
        agent = config.agent
        return self._tool_scope_resolver.delegation_policy_error(
            delegated_child=True,
            agent=agent,
            base_registry=self._base_tool_registry,
            tool_name=tool_name,
        )

    def current_lsp_state(self) -> LspManagerState:
        return self._inspection_coordinator.current_lsp_state()

    @property
    def provider_auth_resolver(self) -> ProviderAuthResolver:
        return self._inspection_coordinator.provider_auth_resolver

    def request_lsp(
        self,
        *,
        server_name: str | None,
        method: str,
        params: dict[str, object],
        workspace: Path,
    ) -> LspRequestResult:
        return self._inspection_coordinator.request_lsp(
            server_name=server_name,
            method=method,
            params=params,
            workspace=workspace,
        )

    def request_diagnostics(
        self,
        *,
        file_path: str,
        workspace: str,
    ) -> dict[str, object]:
        return self._inspection_coordinator.request_diagnostics(
            file_path=file_path,
            workspace=workspace,
        )

    def request_mcp_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
    ):
        return self._inspection_coordinator.request_mcp_tool(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            workspace=workspace,
        )

    def shutdown_mcp(self) -> tuple[EventEnvelope, ...]:
        return self._inspection_coordinator.shutdown_mcp()

    def shutdown_lsp(self) -> tuple[EventEnvelope, ...]:
        return self._inspection_coordinator.shutdown_lsp()

    def current_acp_state(self):
        return self._inspection_coordinator.current_acp_state()

    def disconnect_acp(self) -> tuple[EventEnvelope, ...]:
        return self._inspection_coordinator.disconnect_acp()

    def _runtime_agent_registry(self, installed_manifests: Iterable[AgentManifest] = ()) -> AgentManifestRegistry:
        registry = load_agent_manifest_registry(self._workspace, installed_manifests=installed_manifests)
        builtin = dict(registry.builtin)
        for agent_id in tuple(builtin):
            manifest = get_builtin_agent_manifest(agent_id)
            if manifest is not None:
                builtin[agent_id] = manifest
        return AgentManifestRegistry(builtin=builtin, custom=registry.custom)

    def run_with_persistence(
        self,
        request: RuntimeRequest,
        *,
        allow_internal_metadata: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        request = self._validated_request(
            request,
            allow_internal_metadata=allow_internal_metadata,
        )
        session_id = resolve_runtime_session_routing(request).session_id
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
            except GeneratorExit:
                if final_session is not None and final_session.status == "waiting":
                    response = RuntimeResponse(session=final_session, events=tuple(events), output=output)
                    self.persist_response(request=request, response=response)
                else:
                    self._persist_interrupted_terminal_on_generator_close(
                        request=request,
                        abort_signal=abort_signal,
                        final_session=final_session,
                        events=events,
                        run_id=run_id,
                    )
                raise
            except Exception:
                if final_session is not None and final_session.status == "failed":
                    final_session = disconnect_acp_for_session_state(self._acp_adapter, final_session)
                    response = RuntimeResponse(session=final_session, events=tuple(events), output=output)
                    self.persist_response(request=request, response=response)
                    raise
                if self._interrupt_requested_but_not_emitted(
                    abort_signal=abort_signal,
                    final_session=final_session,
                    events=events,
                ):
                    # A client disconnect / user abort terminated the run before
                    # it reached an abort checkpoint and emitted its terminal
                    # cancelled event. Synthesize the terminal event, seal
                    # ``interrupted``, and swallow the underlying exception — the
                    # run was interrupted, not errored.
                    # ``_interrupt_requested_but_not_emitted`` guarantees
                    # ``final_session`` is non-None here.
                    assert final_session is not None
                    final_session = yield from self._synthesize_interrupted_terminal(
                        session=final_session,
                        events=events,
                        run_id=run_id,
                        reason=abort_signal.reason,
                    )
                    if final_session.status == "waiting":
                        final_session = disconnect_acp_for_session_state(self._acp_adapter, final_session)
                    response = RuntimeResponse(session=final_session, events=tuple(events), output=output)
                    self.persist_response(request=request, response=response)
                    return
                raise

            if final_session is None:
                raise ValueError("runtime stream emitted no chunks")

            if self._interrupt_requested_but_not_emitted(
                abort_signal=abort_signal,
                final_session=final_session,
                events=events,
            ):
                # Abnormal early termination on the normal path (e.g. the run
                # loop ended without producing a terminal chunk while the abort
                # signal was armed). Synthesize the interrupted terminal event so
                # the cancel is always observed and durably persisted.
                final_session = yield from self._synthesize_interrupted_terminal(
                    session=final_session,
                    events=events,
                    run_id=run_id,
                    reason=abort_signal.reason,
                )

            if final_session.status == "waiting":
                final_session = disconnect_acp_for_session_state(self._acp_adapter, final_session)

            final_session = self._session_with_loaded_skill_metadata(
                final_session,
                events=tuple(events),
            )

            response = RuntimeResponse(session=final_session, events=tuple(events), output=output)
            self.persist_response(request=request, response=response)

        finally:
            self._unregister_active_session_id(session_id, run_id=run_id)

    def _interrupt_requested_but_not_emitted(
        self,
        *,
        abort_signal: ProviderAbortSignal | None,
        final_session: SessionState | None,
        events: list[EventEnvelope],
    ) -> bool:
        return FinalizeCoordinator.interrupt_requested_but_not_emitted(
            abort_signal=abort_signal,
            final_session=final_session,
            events=events,
        )

    def _persist_interrupted_terminal_on_generator_close(
        self,
        *,
        request: RuntimeRequest,
        abort_signal: ProviderAbortSignal | None,
        final_session: SessionState | None,
        events: list[EventEnvelope],
        run_id: str,
    ) -> None:
        self._finalize_coordinator.persist_interrupted_terminal_on_generator_close(
            request=request,
            abort_signal=abort_signal,
            final_session=final_session,
            events=events,
            run_id=run_id,
        )

    def _synthesize_interrupted_terminal(
        self,
        *,
        session: SessionState,
        events: list[EventEnvelope],
        run_id: str,
        reason: str | None,
    ) -> Generator[RuntimeStreamChunk, None, SessionState]:
        return (
            yield from self._finalize_coordinator.synthesize_interrupted_terminal(
                session=session,
                events=events,
                run_id=run_id,
                reason=reason,
            )
        )

    def run(self, request: RuntimeRequest) -> RuntimeResponse:
        events: list[EventEnvelope] = []
        output: str | None = None
        final_session: SessionState | None = None

        for chunk in self.run_with_persistence(request):
            final_session = chunk.session
            if chunk.event is not None:
                events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output

        if final_session is None:
            raise ValueError("runtime stream emitted no chunks")

        if final_session.status == "waiting":
            final_session = reload_persisted_session(self._repositories.sessions, self._workspace, session_id=final_session.session.id)

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
        loaded_payloads = [event.payload for event in events if event.event_type == "runtime.skill_loaded"]
        implicit_force_loaded: tuple[dict[str, object], ...] = ()
        raw_snapshot = session.metadata.get("skill_snapshot")
        if isinstance(raw_snapshot, dict):
            applied_payloads = raw_snapshot.get("applied_skill_payloads")
            if isinstance(applied_payloads, list):
                normalized: list[dict[str, object]] = []
                for item in applied_payloads:
                    if not isinstance(item, dict):
                        continue
                    normalized.append(
                        {
                            "name": item.get("name"),
                            "source": "force_load",
                            "source_path": item.get("source_path"),
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
        return self._finalize_coordinator.persist_emitted_event(
            session_id=session_id,
            event_type=event_type,
            source=source,
            payload=payload,
            dedupe_key=dedupe_key,
        )

    def _persist_emitted_chunk(self, chunk: RuntimeStreamChunk) -> RuntimeStreamChunk:
        return self._finalize_coordinator.persist_emitted_chunk(chunk)

    def _persist_emitted_chunks(
        self,
        chunks: Iterable[RuntimeStreamChunk],
        *,
        fallback_sequence: int,
    ) -> Generator[RuntimeStreamChunk, None, int]:
        return (
            yield from self._finalize_coordinator.persist_emitted_chunks(
                chunks,
                fallback_sequence=fallback_sequence,
            )
        )

    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        if "provider_stream" in request.metadata:
            return self.run_with_persistence(request)

        stream_metadata = {**request.metadata, "provider_stream": True}
        validated_stream_metadata = validate_runtime_request_metadata(stream_metadata)
        request_with_stream = RuntimeRequest(
            prompt=request.prompt,
            session_id=request.session_id,
            parent_session_id=request.parent_session_id,
            metadata=validated_stream_metadata,
            allocate_session_id=request.allocate_session_id,
        )
        return self.run_with_persistence(request_with_stream)

    def _stream_chunks(
        self,
        request: RuntimeRequest,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        prepared = self._prepare_stream_request_and_rehydration(
            request,
            session_id=session_id,
            run_id=run_id,
        )
        session = self._assemble_stream_session_metadata(
            prepared,
            run_id=run_id,
        )
        prepared = replace(
            prepared,
            config=self._activate_execution_composition(prepared),
        )
        session, sequence = yield from self._emit_stream_prelude_events(
            prepared,
            session,
            0,
        )
        before_agent_start = yield from self._prepare_before_agent_start(prepared, session, sequence)
        if before_agent_start is None:
            return
        prepared, session, sequence = before_agent_start
        tooling = yield from self._start_stream_mcp_and_tooling(
            prepared,
            session,
            sequence,
        )
        if tooling is None:
            return
        session, sequence, tool_registry, skill_registry, start_hook_guidance = tooling
        startup = yield from self._start_stream_acp_and_skills(
            prepared,
            session,
            sequence,
            tool_registry,
            skill_registry,
            abort_signal=abort_signal,
            hook_guidance=start_hook_guidance,
        )
        if startup is None:
            return
        session, sequence, producer, turn_request, tool_results = startup
        # The run-start checkpoint was written before this run materialized the
        # capability binding and skill snapshot a resume replays from, so an
        # interruption before the first safe boundary used to leave a session
        # that cannot be resumed. Refresh it now that both records exist — this
        # is the last point before any tool call, so every execution window is
        # covered. See docs/contracts/runtime-execution.md.
        self._refresh_run_checkpoint(
            session=session,
            prompt=prepared.provider_prompt if prepared.provider_prompt is not None else prepared.request.prompt,
            sequence=sequence,
        )
        loop = yield from self._run_stream_turn_engine(
            producer=producer,
            tool_registry=tool_registry,
            session=session,
            sequence=sequence,
            turn_request=turn_request,
            tool_results=tool_results,
        )
        yield from self._finalize_stream_run(*loop)

    def _prepare_stream_request_and_rehydration(
        self, request: RuntimeRequest, *, session_id: str | None, run_id: str | None
    ) -> _PreparedStreamSession:
        resolved_session_id = session_id or resolve_runtime_session_routing(request).session_id
        config = self.run_config_for_request(request)
        existing_session = self._load_existing_session_if_present(session_id=resolved_session_id)
        raw_composition_ref = request.metadata.get("composition_ref")
        raw_task_id = request.metadata.get("background_task_id")
        background_dispatch = request.metadata.get("background_run") is True or raw_task_id is not None
        task_composition_ref: CompositionRef | None = None
        if background_dispatch:
            if not isinstance(raw_task_id, str) or raw_composition_ref is None:
                raise ValueError("background task dispatch requires its persisted composition reference")
            task_composition_ref = self._background_task_composition_ref(
                task_id=raw_task_id,
                session_id=resolved_session_id,
            )
            if CompositionRef.model_validate(raw_composition_ref) != task_composition_ref:
                raise ValueError("background task request composition reference does not match its owner")
        elif raw_composition_ref is not None:
            raise ValueError("only a background task may supply a request composition reference")
        if existing_session is not None:
            frozen_composition = self._admit_existing_session_composition(existing_session.session.session.id)
            composition_ref = composition_ref_from_session_metadata(existing_session.session.metadata)
            if task_composition_ref is not None and task_composition_ref != composition_ref:
                raise ValueError("background task and session composition references disagree")
        elif task_composition_ref is not None:
            composition_ref = task_composition_ref
            frozen_composition = self._repositories.recovery.load_execution_composition(ref=composition_ref)
            self._composition_owner.refresh(frozen_composition)
        else:
            frozen_composition = self._prepare_execution_composition(config)
            composition_ref = frozen_composition.reference(
                workspace=str(self._workspace),
                owner=SessionCompositionOwner(kind="session", session_id=resolved_session_id),
            )
        request_metadata = self._fresh_request_metadata(request.metadata)
        if existing_session is not None and "agent" not in request_metadata:
            session_config = self.run_config_from_metadata(existing_session.session.metadata)
            if session_config.agent is not None:
                config = replace(config, agent=session_config.agent)
        if existing_session is not None:
            queued_steering = self._repositories.sessions.drain_session_messages(
                workspace=self._workspace,
                session_id=resolved_session_id,
                kind="steering",
                remember_dedupe=True,
            )
            queued_followups = self._repositories.sessions.drain_session_messages(
                workspace=self._workspace,
                session_id=resolved_session_id,
                kind="follow_up",
            )
            if queued_followups:
                request = replace(
                    request,
                    prompt="\n\n".join(message.content for message in queued_followups),
                )
            if queued_steering:
                steering_text = "\n\n".join(message.content for message in queued_steering)
                request = replace(
                    request,
                    prompt=(f"{request.prompt}\n\nRuntime steering messages:\n{steering_text}" if request.prompt.strip() else steering_text),
                )
        rehydrated_conversation_segments = self.replayed_conversation_segments_for_existing_session(
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
        resolved_hook_plan = hook_plan_from_session_metadata(existing_session.session.metadata) if existing_session is not None else None
        if resolved_hook_plan is None:
            resolved_hook_plan = materialize_hook_plan(
                self._config.hooks,
                agent_hook_refs=hook_preset_refs_for_agent(config.agent),
                agent_source=(config.agent.preset if config.agent is not None else "default"),
            )
        resolved_hook_presets = self._build_hook_preset_snapshot(
            config.agent,
        )
        return _PreparedStreamSession(
            request=request,
            resolved_session_id=resolved_session_id,
            config=config,
            frozen_composition=frozen_composition,
            composition_ref=composition_ref,
            request_metadata=request_metadata,
            existing_session=existing_session,
            rehydrated_conversation_segments=rehydrated_conversation_segments,
            rehydrated_tool_results=rehydrated_tool_results,
            resolved_hook_presets=resolved_hook_presets,
            resolved_hook_plan=resolved_hook_plan,
        )

    def _assemble_stream_session_metadata(self, prep: _PreparedStreamSession, *, run_id: str | None) -> SessionState:
        request = prep.request
        resolved_session_id = prep.resolved_session_id
        config = prep.config
        request_metadata = prep.request_metadata
        existing_session = prep.existing_session
        resolved_hook_presets = prep.resolved_hook_presets
        resolved_hook_plan = prep.resolved_hook_plan
        session_request_metadata = dict(request_metadata)
        session_request_metadata.pop("background_rate_limit_retry", None)
        session_request_metadata.pop("show_thinking", None)
        parent_runtime_policy: dict[str, object] | None = None
        if request.parent_session_id is not None:
            parent_policy_metadata = self._parent_policy_metadata(request.parent_session_id)
            if parent_policy_metadata is not None:
                raw_parent_runtime_policy = parent_policy_metadata.get("runtime_policy")
                if raw_parent_runtime_policy is not None:
                    if not isinstance(raw_parent_runtime_policy, dict):
                        raise ValueError("persisted parent runtime_policy must be an object")
                    parent_runtime_policy = raw_parent_runtime_policy
        persisted_runtime_policy: dict[str, object] | None = None
        if existing_session is not None and existing_session.session.session.parent_id == request.parent_session_id:
            raw_persisted_runtime_policy = existing_session.session.metadata.get("runtime_policy")
            if raw_persisted_runtime_policy is not None:
                if not isinstance(raw_persisted_runtime_policy, dict):
                    raise ValueError("persisted runtime_policy must be an object")
                persisted_runtime_policy = raw_persisted_runtime_policy
        policy_snapshot = materialize_runtime_policy_snapshot(
            persisted_session_policy=persisted_runtime_policy,
            agent_preset=config.agent.preset if config.agent is not None else "default",
            agent_manifest_id=config.agent.preset if config.agent is not None else "default",
            callable_subagent_presets=self._agent_registry.executable_subagent_ids(),
            runtime_config={
                **self._runtime_config_metadata(
                    config,
                ),
            },
            request_metadata=request_metadata,
            parent_snapshot=parent_runtime_policy,
        )
        if policy_snapshot.read_only:
            session_request_metadata["read_only"] = True
        session = SessionState(
            session=SessionRef(id=resolved_session_id, parent_id=request.parent_session_id),
            status="running",
            turn=1,
            metadata={
                **session_request_metadata,
                "workspace": str(self._workspace),
                "runtime_config": self._runtime_config_metadata(
                    config,
                ),
                "runtime_policy": policy_snapshot.as_payload(),
                **({"resolved_hook_presets": resolved_hook_presets.to_payload()} if resolved_hook_presets.presets else {}),
                "resolved_hook_plan": resolved_hook_plan.to_payload(),
                "runtime_state": runtime_state_metadata_payload(
                    run_id=run_id,
                    acp_state=self._acp_adapter.current_state(),
                ),
            },
        )
        composition_ref = prep.composition_ref
        composition_ref_payload = composition_ref.model_dump(mode="json")
        session = self._session_with_agent_capability_snapshot(
            session=session,
            config=config,
            request_metadata=request_metadata,
            resolved_hook_presets=resolved_hook_presets,
            tool_materialization=self._tool_materialization,
            composition_ref=composition_ref_payload,
        )
        session = replace(session, metadata={**session.metadata, "composition_ref": composition_ref_payload})
        if existing_session is not None:
            session = replace(
                session,
                metadata=self.composition_session_metadata(resolved_session_id, session.metadata),
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
        self._repositories.recovery.save_interrupted_checkpoint(
            workspace=self._workspace,
            session_id=resolved_session_id,
            prompt=request.prompt,
            session_metadata=session.metadata,
            tool_results=(),
            last_event_sequence=0,
            composition_ref=composition_ref,
            composition=(
                prep.frozen_composition
                if existing_session is None
                and isinstance(composition_ref.owner, SessionCompositionOwner)
                and composition_ref.owner.session_id == resolved_session_id
                else None
            ),
            output=None,
            create_if_missing=True,
            turn=session.turn,
            parent_session_id=request.parent_session_id,
        )
        return session

    def _emit_stream_prelude_events(
        self, prep: _PreparedStreamSession, session: SessionState, sequence: int
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, int]]:
        request = prep.request
        config = prep.config
        runtime_policy_snapshot = session.metadata.get("runtime_policy")
        request_received_payload: dict[str, object] = {
            "prompt": request.prompt,
            **({"agent_preset": active_agent.preset} if (active_agent := config.agent) is not None else {}),
        }
        if isinstance(runtime_policy_snapshot, dict):
            request_received_payload["runtime_policy"] = runtime_policy_observability_payload(runtime_policy_snapshot)
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

        reasoning_diagnostic = self._reasoning_controls_diagnostic_for_config(config)
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

        return session, sequence

    def _prepare_before_agent_start(
        self,
        prep: _PreparedStreamSession,
        session: SessionState,
        sequence: int,
    ) -> Generator[RuntimeStreamChunk, None, tuple[_PreparedStreamSession, SessionState, int] | None]:
        registry = self._activated_before_agent_start_registries[prep.composition_ref]
        if not registry.declarations:
            return prep, session, sequence
        outcome = registry.apply(
            BeforeAgentStartRequest(
                original_prompt=prep.request.prompt,
                effective_prompt=prep.request.prompt,
            )
        )
        trace = outcome.trace_payload()
        if outcome.blocked_reason is None and outcome.failed_handler is None:
            metadata = dict(session.metadata)
            metadata["before_agent_start"] = {"effective_prompt": outcome.prompt, "trace": trace}
            session = replace(session, metadata=metadata)
            prep = replace(prep, provider_prompt=outcome.prompt)
        event = self._persist_emitted_event(
            session_id=session.session.id,
            event_type=RUNTIME_BEFORE_AGENT_START,
            source="runtime",
            payload=trace,
        )
        sequence = event.sequence
        if outcome.blocked_reason is not None or outcome.failed_handler is not None:
            blocked = outcome.blocked_reason is not None
            failed = self._persist_emitted_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=(
                        f"request blocked by before-agent-start handler: {outcome.blocked_reason}"
                        if blocked
                        else f"before-agent-start handler failed: {outcome.failed_handler}"
                    ),
                    payload={"kind": "before_agent_start_blocked" if blocked else "before_agent_start_failed"},
                )
            )
            failed_event = failed.require_event()
            self._finalize_coordinator.persist_response(
                request=prep.request,
                response=RuntimeResponse(
                    session=failed.session,
                    events=(event, failed_event),
                ),
            )
            yield RuntimeStreamChunk(kind="event", session=session, event=event)
            yield failed
            return None
        self._refresh_run_checkpoint(session=session, prompt=outcome.prompt, sequence=sequence)
        yield RuntimeStreamChunk(kind="event", session=session, event=event)
        return prep, session, sequence

    def _start_stream_mcp_and_tooling(
        self, prep: _PreparedStreamSession, session: SessionState, sequence: int
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, int, ToolRegistry, SkillRegistry, tuple[str, ...]] | None]:
        request = prep.request
        config = prep.config
        request_metadata = prep.request_metadata
        resolved_hook_presets = prep.resolved_hook_presets
        (
            mcp_startup_chunks,
            session,
            sequence,
            mcp_failed_chunk,
            mcp_descriptors,
        ) = self.materialize_mcp_tools_for_run(
            session=session,
            sequence=sequence,
            request_metadata=request_metadata,
            config=config,
            failure_kind="mcp_startup_failed",
        )
        for chunk in mcp_startup_chunks:
            persisted_chunk = self._persist_emitted_chunk(chunk)
            sequence = persisted_chunk.require_event().sequence
            yield persisted_chunk
        if mcp_failed_chunk is not None:
            yield self._persist_emitted_chunk(mcp_failed_chunk)
            return None

        local_config = config.tools.local if config.tools is not None else None
        local_manifests = workspace_local_tool_manifests_provider(self._workspace)(local_config)
        mcp_materialization = self._tool_materializer.materialize_mcp_descriptors(mcp_descriptors)
        builtin_mcp_servers = {descriptor.name for descriptor in list_builtin_mcp_descriptors()}
        builtin_mcp_tool_names = tuple(
            mcp_tool_definition(descriptor).name for descriptor in mcp_descriptors if descriptor.server_name in builtin_mcp_servers
        )
        tool_materialization = self._tool_materialization_for_config(
            config,
            session.metadata,
            materialization=mcp_materialization,
            local_manifests=local_manifests,
            builtin_mcp_tool_names=builtin_mcp_tool_names,
        )
        session = self._session_with_agent_capability_snapshot(
            session=session,
            config=config,
            request_metadata=request_metadata,
            resolved_hook_presets=resolved_hook_presets,
            tool_materialization=tool_materialization,
            composition_ref=prep.composition_ref.model_dump(mode="json"),
        )
        tool_registry = self.tool_registry_for_run(
            session=session,
            config=config,
            tool_materialization=tool_materialization,
            mcp_descriptors=mcp_descriptors,
            local_manifests=local_manifests,
        )
        skill_registry = self.skill_registry_for_config(config)

        start_hook_outcome = run_lifecycle_hooks_for_session(
            hooks=self._config.hooks,
            workspace=self._workspace,
            session=session,
            surface="session_start",
            recursion_env_var=HOOK_RECURSION_ENV_VAR,
            sequence=sequence,
            payload={"prompt": request.prompt},
            policy=hook_execution_policy_from_metadata(session.metadata),
        )
        sequence = yield from self._persist_emitted_chunks(
            start_hook_outcome.chunks,
            fallback_sequence=start_hook_outcome.last_sequence,
        )
        if start_hook_outcome.failed_error is not None:
            failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                session=session,
                sequence=sequence,
                surface="session_start",
                error=start_hook_outcome.failed_error,
                hooks=self._config.hooks,
            )
            if failed_chunk is not None:
                yield self._persist_emitted_chunk(failed_chunk)
                return None
        return session, sequence, tool_registry, skill_registry, start_hook_outcome.guidance

    def _refresh_run_checkpoint(self, *, session: SessionState, prompt: str, sequence: int) -> None:
        """Record the run's replayable bindings in its resumable checkpoint.

        A resume replays what the recorded run was bound to (capability binding +
        skill snapshot), but the run-start checkpoint is written before either is
        materialized. This refresh happens once they exist and before any tool
        call, so an interruption at any later point is resumable.

        ``save_interrupted_checkpoint`` writes the row only (no events), so this is
        a cheap metadata refresh at the last durable event of this run's prefix.
        """
        ref = composition_ref_from_session_metadata(session.metadata)
        self._repositories.recovery.save_interrupted_checkpoint(
            workspace=self._workspace,
            session_id=session.session.id,
            prompt=prompt,
            session_metadata=session.metadata,
            tool_results=(),
            last_event_sequence=sequence,
            composition_ref=ref,
            output=None,
            create_if_missing=False,
            turn=session.turn,
            parent_session_id=session.session.parent_id,
        )

    def _start_stream_acp_and_skills(
        self,
        prep: _PreparedStreamSession,
        session: SessionState,
        sequence: int,
        tool_registry: ToolRegistry,
        skill_registry: SkillRegistry,
        *,
        abort_signal: ProviderAbortSignal | None,
        hook_guidance: tuple[str, ...] = (),
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, int, TurnProducer, TurnRequest, list[ReportedCall]] | None]:
        request = prep.request
        config = prep.config
        request_metadata = prep.request_metadata
        provider_prompt = prep.provider_prompt if prep.provider_prompt is not None else request.prompt
        rehydrated_conversation_segments = prep.rehydrated_conversation_segments
        rehydrated_tool_results = prep.rehydrated_tool_results
        loaded_skill_names = skills.loaded_skill_names(skill_registry)

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

        skill_snapshot = self.build_skill_snapshot(
            skill_registry,
            metadata=session.metadata,
            agent=config.agent,
            source="run",
        )
        catalog_skill_context = skills.catalog_skill_context(
            skill_registry,
            available_skill_names=tuple(loaded_skill_names),
            selected_skill_names=skill_snapshot.selected_skill_names,
        )
        skill_prompt_context = skill_prompt_context_for_assembly(
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
        raw_rulebook_snapshot = session.metadata.get("rulebook_snapshot")
        if raw_rulebook_snapshot is None:
            rulebook_snapshot = build_rule_catalog(self._workspace).snapshot
        else:
            rulebook_snapshot = rulebook_snapshot_from_payload(raw_rulebook_snapshot)
        session = SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata={
                **session.metadata,
                "rulebook_snapshot": rulebook_snapshot_payload(rulebook_snapshot),
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

        assembled_context = self.assemble_provider_context(
            prompt=provider_prompt,
            tool_results=rehydrated_tool_results,
            session_metadata=session.metadata,
            skill_prompt_context=skill_prompt_context,
            replayed_conversation_segments=rehydrated_conversation_segments,
            tool_registry=tool_registry,
            hook_guidance=hook_guidance or None,
        )
        session = session_with_context_window_payload_metadata(
            session,
            dict(assembled_context.metadata),
        )
        turn_request = turn_request_for_session(
            TurnRequest(
                session=turn_session_snapshot(session),
                prompt=provider_prompt,
                available_tools=self.provider_tool_definitions(tool_registry, config),
                context_window=assembled_context.context_window
                or self.prepare_provider_context_window(
                    prompt=provider_prompt,
                    tool_results=rehydrated_tool_results,
                    session_metadata=session.metadata,
                    abort_signal=abort_signal,
                ),
                assembled_context=assembled_context,
                metadata={
                    **request_metadata,
                    "agent_preset": serialize_runtime_agent_config(self.run_config_from_metadata(session.metadata).agent),
                    "provider_attempt": 0,
                    "provider_stream": request_metadata.get("provider_stream", False),
                    **(
                        {"reasoning_effort": config.reasoning_effort}
                        if config.reasoning_effort is not None and "reasoning_effort" not in request_metadata
                        else {}
                    ),
                },
                abort_signal=abort_signal,
                run_step=1,
            ),
            session,
        )
        tool_results: list[ReportedCall] = list(rehydrated_tool_results)
        producer = self.turn_producer_for_session_metadata(session.metadata)
        return session, sequence, producer, turn_request, tool_results

    def _run_stream_turn_engine(
        self,
        *,
        producer: TurnProducer,
        tool_registry: ToolRegistry,
        session: SessionState,
        sequence: int,
        turn_request: TurnRequest,
        tool_results: list[ReportedCall],
    ) -> Generator[RuntimeStreamChunk, None, tuple[RuntimeStreamChunk | None, int, RuntimeStreamChunk | None, Exception | None]]:
        last_chunk: RuntimeStreamChunk | None = None
        last_sequence = sequence
        deferred_failed_chunk: RuntimeStreamChunk | None = None
        turn_error: Exception | None = None
        try:
            for chunk in self._run_loop_coordinator.execute_turn_engine(
                producer=producer,
                tool_registry=tool_registry,
                session=session,
                sequence=sequence,
                turn_request=turn_request,
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
            turn_error = exc

        return last_chunk, last_sequence, deferred_failed_chunk, turn_error

    def _finalize_stream_run(
        self,
        last_chunk: RuntimeStreamChunk | None,
        last_sequence: int,
        deferred_failed_chunk: RuntimeStreamChunk | None,
        turn_error: Exception | None,
    ) -> Generator[RuntimeStreamChunk]:
        yield from self._finalize_coordinator.finalize_stream_run(
            last_chunk=last_chunk,
            last_sequence=last_sequence,
            deferred_failed_chunk=deferred_failed_chunk,
            turn_error=turn_error,
        )

    def persist_response(self, *, request: RuntimeRequest, response: RuntimeResponse) -> None:
        self._finalize_coordinator.persist_response(request=request, response=response)

    def save_interrupted_checkpoint(
        self,
        *,
        session_id: str,
        prompt: str,
        session_metadata: dict[str, object],
        tool_results: tuple[dict[str, object], ...] | list[dict[str, object]],
        last_event_sequence: int,
        output: str | None,
        create_if_missing: bool = False,
        turn: int | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        self._finalize_coordinator.save_interrupted_checkpoint(
            session_id=session_id,
            prompt=prompt,
            session_metadata=session_metadata,
            tool_results=tool_results,
            last_event_sequence=last_event_sequence,
            output=output,
            create_if_missing=create_if_missing,
            turn=turn,
            parent_session_id=parent_session_id,
        )

    def _approval_requested_hook_chunks(
        self,
        *,
        session: SessionState,
        pending: PendingApproval,
        sequence: int,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], int, str | None]:
        """Run the ``approval_requested`` foreground observer (fail-open, cancel blocks)."""
        try:
            outcome = run_lifecycle_hooks_for_session(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session=session,
                surface="approval_requested",
                recursion_env_var=HOOK_RECURSION_ENV_VAR,
                sequence=sequence,
                payload=_approval_requested_hook_payload(pending),
                policy=hook_execution_policy_from_metadata(session.metadata),
            )
        except Exception as exc:
            logging.getLogger(__name__).warning("approval_requested hook failed for %s: %s", pending.tool_name, exc)
            return (), sequence, None
        if outcome.failed_error is not None:
            # Fail-open observer: keep the error event, never block on executor failure.
            return outcome.chunks, outcome.last_sequence, None
        if outcome.action != "cancel":
            return outcome.chunks, outcome.last_sequence, None
        return outcome.chunks, outcome.last_sequence, hook_cancel_reason(outcome, default="hook blocked approval")

    def resolve_permission(
        self,
        *,
        session: SessionState,
        tool: ToolDefinition,
        tool_instance: Tool,
        tool_call: ToolCall,
        sequence: int,
        permission_policy: PermissionPolicy,
    ) -> PermissionOutcome:
        effective_permission = self.run_config_from_metadata(session.metadata).permission
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

        raw_background_task_id = session.metadata.get("background_task_id")
        permission = resolve_permission(
            tool,
            tool_call,
            policy=self._permission_policy if self._permission_policy is not None else permission_policy,
            path_scope=path_scope,
            operation_class=operation_class,
            canonical_path=canonical_path,
            matched_rule=matched_rule,
            policy_surface=policy_surface,
            external_decision=external_decision,
            rule_decision=rule_decision,
            owner_session_id=session.session.id,
            owner_parent_session_id=session.session.parent_id,
            delegated_task_id=(raw_background_task_id if isinstance(raw_background_task_id, str) else None),
            read_only=runtime_effective_read_only_from_metadata(session.metadata),
        )

        if path_scope == "workspace" and operation_class == "read":
            return PermissionOutcome(
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

        if permission.decision != "ask":
            pending = permission.pending_approval
            if pending is None:
                raise ValueError("non-read-only permission decisions require pending approval data")
            return self.approval_resolution_outcome(
                session=session,
                pending=pending,
                decision=permission.decision,
                sequence=sequence,
            )

        pending = permission.pending_approval
        if pending is None:
            raise ValueError("non-read-only permission decisions require pending approval data")

        waiting_session = session_with_plan_state(
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
            "tool_call_id": tool_call.tool_call_id,
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
        approval_chunk = RuntimeStreamChunk(kind="event", session=waiting_session, event=request_event)
        hook_chunks, hook_last_sequence, hook_cancel_reason = self._approval_requested_hook_chunks(
            session=waiting_session,
            pending=pending,
            sequence=sequence,
        )
        if hook_cancel_reason is not None:
            blocked_pending = replace(pending, reason=hook_cancel_reason)
            deny_outcome = self.approval_resolution_outcome(
                session=session,
                pending=blocked_pending,
                decision="deny",
                sequence=hook_last_sequence + 1,
            )
            deny_chunks = _permission_chunks_with_hook_reason(deny_outcome.chunks, hook_cancel_reason)
            return PermissionOutcome(
                chunks=(approval_chunk, *hook_chunks, *deny_chunks),
                last_sequence=deny_outcome.last_sequence,
                denied=True,
                denied_approval=blocked_pending,
            )
        if hook_chunks:
            return PermissionOutcome(
                chunks=(approval_chunk, *hook_chunks),
                last_sequence=hook_last_sequence,
                pending_approval=pending,
            )
        return PermissionOutcome(
            chunks=(approval_chunk,),
            last_sequence=sequence,
            pending_approval=pending,
        )

    def approval_resolution_outcome(
        self,
        *,
        session: SessionState,
        pending: PendingApproval,
        decision: PermissionResolution,
        sequence: int,
    ) -> PermissionOutcome:
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
            return PermissionOutcome(
                chunks=(
                    RuntimeStreamChunk(
                        kind="event",
                        session=session_with_plan_state(session, status="in_progress"),
                        event=resolution_event,
                    ),
                ),
                last_sequence=sequence,
                denied=True,
                denied_approval=pending,
            )
        return PermissionOutcome(
            chunks=(
                RuntimeStreamChunk(
                    kind="event",
                    session=session_with_plan_state(session, status="in_progress"),
                    event=resolution_event,
                ),
            ),
            last_sequence=sequence,
        )

    def list_sessions(self) -> tuple[StoredSessionSummary, ...]:
        return self._inspection_coordinator.list_sessions()

    def rename_session(self, *, session_id: str, title: str) -> StoredSessionSummary:
        """Set a user-settable session title; returns the updated summary.

        Titles are client-facing labels, not runtime metadata, so they live in
        their own column/surface rather than in the session metadata blob.
        """
        return self._inspection_coordinator.rename_session(session_id=session_id, title=title)

    def fork_session(self, *, session_id: str, at_sequence: int | None = None) -> StoredSessionSummary:
        """Fork a session: copy its event prefix ``1..N`` into a new session.

        The source is never modified. ``at_sequence`` defaults to the source's
        watermark; a boundary that splits a tool call from its result is
        refused with ``RuntimeSessionForkBoundaryError``. VoidCode keeps one
        linear event log per session and has no leaf pointer, so "branch" is
        exactly this plus continuing the returned session.
        """
        if ACTIVE_SESSION_REGISTRY.active_run_count(workspace=self._workspace, session_id=session_id):
            raise SessionBusyConflictError("cannot fork a session while it is running")
        parent_response = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        parent_session = parent_response.session
        if parent_session.status in {"running", "waiting"}:
            raise SessionBusyConflictError(f"cannot fork a session with status {parent_session.status}")
        self._admit_existing_session_composition(session_id)
        return self._inspection_coordinator.fork_session(
            session_id=session_id,
            at_sequence=at_sequence,
        )

    def create_session(self, *, title: str | None = None) -> StoredSessionSummary:
        """Create a new empty session with an optional user-settable title."""
        from uuid import uuid4

        from .contracts import validate_session_title

        session_id = f"session-{uuid4().hex}"
        validated_title = validate_session_title(title) if title is not None else None
        return self._repositories.sessions.create_session(
            workspace=self._workspace,
            session_id=session_id,
            title=validated_title,
        )

    def session_lineage(self, *, session_id: str | None = None) -> tuple[StoredSessionLineageEntry, ...]:
        """Read-only fork ancestry, oldest ancestor first.

        With ``session_id`` the walk follows ``forked_from_session_id`` upward
        from that session; without it every workspace session's provenance row
        is returned so a client can lay out the whole forest. Delegated
        background-task children (``parent_session_id`` set) are excluded from
        the whole-workspace read: they are not fork nodes.
        """
        return self._inspection_coordinator.session_lineage(session_id=session_id)

    def session_forest(self) -> tuple[StoredSessionForestEntry, ...]:
        """Read-only workspace fork forest in display order, parents first.

        The single forest projection every client renders: each entry carries
        its topological ``depth``, roots and siblings have one data-determined
        order (provenance sequence, then session id), and a malformed cycle is
        refused with ``SessionLineageCycleError``.
        """
        return self._inspection_coordinator.session_forest()

    def session_entries(self, session_id: str) -> tuple[SessionEntrySummary, ...]:
        """Every stored entry of one session, ascending ``sequence``.

        The read-only listing a user picks a checkout target from: each row
        carries its ancestor edge and whether it is on the current leaf path,
        so the abandoned branches a checkout left behind are visible.
        """
        return self._inspection_coordinator.session_entries(session_id=session_id)

    def checkout_session(self, session_id: str, sequence: int) -> int:
        """Move the session's leaf to ``sequence`` and return the new leaf.

        An explicit "continue from here": the events after ``sequence`` stay
        stored as an off-path branch that a later checkout can restore, and the
        next run or resume replays the root→leaf path to this position. The row
        is left continuable (never terminal at the chosen position), so
        ``sessions resume`` continues instead of replaying a stored response.
        A target that splits a tool/approval pair is refused with
        ``RuntimeSessionCheckoutBoundaryError``.
        """
        self._admit_existing_session_composition(session_id)
        return self._inspection_coordinator.checkout_session(
            session_id=session_id,
            sequence=sequence,
        )

    def revert_session(self, *, session_id: str, sequence: int) -> int:
        """Continue from the entry just before ``sequence``; returns the new leaf."""
        return self._inspection_coordinator.revert_session(
            session_id=session_id,
            sequence=sequence,
        )

    def undo_session(self, *, session_id: str) -> int:
        """Continue from just before the latest user turn; returns the new leaf."""
        return self._inspection_coordinator.undo_session(session_id=session_id)

    def tool_effectiveness_report(self) -> ToolEffectivenessReport:
        return self._inspection_coordinator.tool_effectiveness_report()

    @property
    def task_substrate(self) -> TaskSubstrate:
        """Runtime-owned task substrate providing minimal async task lifecycle."""
        return self._background_task_supervisor

    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState:
        validated_request = self._validated_request(request)
        composition = self._prepare_execution_composition(self.run_config_for_request(validated_request))
        return self._background_task_supervisor.start_background_task(
            validated_request,
            composition=composition,
        )

    def authorize_background_task_owner(self, task_id: str, *, parent_session_id: str | None) -> None:
        """Authorize a parent session before a tool reads or cancels its task."""
        self._background_task_supervisor.authorize_background_task_owner(
            task_id,
            parent_session_id=parent_session_id,
        )

    def load_background_task(self, task_id: str) -> BackgroundTaskState:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        self._background_task_supervisor.drain_queued_background_tasks()
        validate_id(task_id, field_name="task_id")
        task = self._repositories.tasks.load_background_task(workspace=self._workspace, task_id=task_id)
        return self._background_task_supervisor.task_with_observability(task)

    def wait_for_background_task(self, task_id: str, *, timeout_seconds: float) -> BackgroundTaskState:
        """Wait for a terminal child transition using runtime lifecycle delivery."""
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        self._background_task_supervisor.drain_queued_background_tasks()
        return self._background_task_supervisor.wait_for_background_task(
            task_id,
            timeout_seconds=timeout_seconds,
        )

    def load_background_task_result(
        self,
        task_id: str,
        *,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskResult:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        self._background_task_supervisor.drain_queued_background_tasks()
        return self._background_task_supervisor.load_background_task_result(
            task_id,
            emit_result_read_hook=emit_result_read_hook,
        )

    def load_background_task_group_result(
        self,
        *,
        task_ids: tuple[str, ...] = (),
        parallel_group_id: str | None = None,
        parent_session_id: str | None = None,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskGroupResult:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        self._background_task_supervisor.drain_queued_background_tasks()
        return self._background_task_supervisor.load_background_task_group_result(
            task_ids=task_ids,
            parallel_group_id=parallel_group_id,
            parent_session_id=parent_session_id,
            emit_result_read_hook=emit_result_read_hook,
        )

    def wait_for_background_task_group(
        self,
        *,
        task_ids: tuple[str, ...] = (),
        parallel_group_id: str | None = None,
        parent_session_id: str | None = None,
        timeout_seconds: float,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskGroupResult:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        self._background_task_supervisor.drain_queued_background_tasks()
        return self._background_task_supervisor.wait_for_background_task_group(
            task_ids=task_ids,
            parallel_group_id=parallel_group_id,
            parent_session_id=parent_session_id,
            timeout_seconds=timeout_seconds,
            emit_result_read_hook=emit_result_read_hook,
        )

    def load_background_task_result_by_child_session(
        self,
        *,
        child_session_id: str,
        emit_result_read_hook: bool = True,
    ) -> BackgroundTaskResult | None:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        self._background_task_supervisor.drain_queued_background_tasks()
        validated_child_session_id = validate_id(child_session_id, field_name="child_session_id")
        task = self._repositories.tasks.load_background_task_by_child_session(
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
        self._background_task_supervisor.drain_queued_background_tasks()
        return self._background_task_supervisor.summaries_with_observability(
            self._repositories.tasks.list_background_tasks(workspace=self._workspace)
        )

    def has_pending_background_tasks(self, *, parent_session_id: str) -> bool:
        """Whether a non-terminal background task will re-wake this parent session.

        Read-only store projection: a queued/running/awaiting child completes
        into a parent notification, so its completion already continues the
        parent loop and a reminder would only add noise. Unreconciled rows count
        as pending, which errs toward staying silent.
        """
        validated_parent_session_id = validate_id(parent_session_id, field_name="parent_session_id")
        summaries = self._repositories.tasks.list_background_tasks_by_parent_session(
            workspace=self._workspace,
            parent_session_id=validated_parent_session_id,
        )
        return any(not is_background_task_terminal(summary.status) for summary in summaries)

    def list_background_tasks_by_parent_session(self, *, parent_session_id: str) -> tuple[StoredBackgroundTaskSummary, ...]:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        self._background_task_supervisor.drain_queued_background_tasks()
        validated_parent_session_id = validate_id(parent_session_id, field_name="parent_session_id")
        return self._background_task_supervisor.summaries_with_observability(
            self._repositories.tasks.list_background_tasks_by_parent_session(
                workspace=self._workspace,
                parent_session_id=validated_parent_session_id,
            )
        )

    def background_task_roster(self, *, parent_session_id: str) -> dict[str, object]:
        """Return a bounded, prompt/transcript-free task projection for one parent."""
        validated_parent_session_id = validate_id(parent_session_id, field_name="parent_session_id")
        summaries = self.list_background_tasks_by_parent_session(
            parent_session_id=validated_parent_session_id,
        )
        limit = 100
        selected = summaries[:limit]
        tasks: list[dict[str, object]] = []
        for summary in selected:
            state = self._repositories.tasks.load_background_task(
                workspace=self._workspace,
                task_id=summary.task.id,
            )
            state = self._background_task_supervisor.task_with_observability(state)
            delegation = state.request.metadata.get("delegation")
            metadata = delegation if isinstance(delegation, dict) else state.request.metadata
            group_id = metadata.get("parallel_group_id")
            group_size = metadata.get("parallel_group_size")
            if not isinstance(group_id, str) or not group_id.strip():
                group_id = None
            else:
                group_id = group_id[:256]
            if isinstance(group_size, str) and group_size.isdecimal():
                group_size = int(group_size)
            if not isinstance(group_size, int) or isinstance(group_size, bool):
                group_size = None
            duration_seconds = None
            if state.started_at_unix_ms is not None and state.finished_at_unix_ms is not None:
                duration_seconds = round(
                    max(state.finished_at_unix_ms - state.started_at_unix_ms, 0) / 1000,
                    3,
                )
            approval_blocked = state.approval_request_id is not None
            task_id = state.task.id[:256]
            child_session_id = state.session_id[:256] if state.session_id is not None else None
            next_steps: dict[str, object] = {
                "task": f'task(operation="output", task_id="{task_id}")',
            }
            if child_session_id is not None:
                next_steps["child_session"] = f"session:{child_session_id}"
            item: dict[str, object] = {
                "task_id": task_id,
                "status": state.status,
                "lifecycle_status": "waiting_approval" if approval_blocked else state.status,
                "parent_session_id": validated_parent_session_id[:256],
                "child_session_id": child_session_id,
                "session_id": child_session_id,
                "parallel_group_id": group_id,
                "parallel_group_size": group_size,
                "created_at": state.created_at,
                "updated_at": state.updated_at,
                "created_at_unix_ms": state.created_at_unix_ms,
                "started_at_unix_ms": state.started_at_unix_ms,
                "finished_at_unix_ms": state.finished_at_unix_ms,
                "duration_seconds": duration_seconds,
                "approval_blocked": approval_blocked,
                "approval_request_id": state.approval_request_id[:256] if state.approval_request_id is not None else None,
                "question_request_id": state.question_request_id[:256] if state.question_request_id is not None else None,
                "result_available": state.result_available,
                "terminal": is_background_task_terminal(state.status),
                "next_steps": next_steps,
            }
            if state.observability is not None:
                item["waiting_reason"] = state.observability.waiting_reason
            tasks.append(item)
        return {
            "parent_session_id": validated_parent_session_id[:256],
            "tasks": tasks,
            "task_count": len(tasks),
            "total_task_count": len(summaries),
            "truncated": len(summaries) > limit,
            "limit": limit,
        }

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        return self._background_task_supervisor.cancel_background_task(task_id)

    def retry_background_task(self, task_id: str) -> BackgroundTaskState:
        task = self._repositories.tasks.load_background_task(workspace=self._workspace, task_id=task_id)
        if "bundle_import_provenance" in task.request.metadata:
            raise ValueError("imported background task observations cannot be retried")
        composition_ref = CompositionRef.model_validate(task.request.metadata.get("composition_ref"))
        if composition_ref.workspace != str(self._workspace):
            raise ValueError("background task composition reference names a different workspace")
        frozen = self._repositories.recovery.load_execution_composition(ref=composition_ref)
        self._composition_owner.refresh(frozen)
        return self._background_task_supervisor.retry_background_task(task_id)

    def steer_background_task(self, task_id: str, content: str) -> BackgroundTaskState:
        """Steer a keep-alive background task with a new instruction.

        Delegates to the background-task supervisor; the task must be
        keep-alive and currently ``idle`` (or ``interrupted``, treated as a
        resumable breakpoint) and the content must be non-empty.
        """
        return self._background_task_supervisor.steer_background_task(task_id, content)

    def session_result(self, *, session_id: str) -> RuntimeSessionResult:
        return self._inspection_coordinator.session_result(session_id=session_id)

    def replay_session(self, *, session_id: str) -> RuntimeResponse:
        """Read the persisted session transcript without resume semantics."""
        return self._inspection_coordinator.replay_session(session_id=session_id)

    def session_events_after(self, *, session_id: str, after_sequence: int) -> SessionEventBatch:
        """Read only the persisted events after ``after_sequence`` plus the row status.

        Follow-stream poll used after an initial ``replay_session``: it must be
        cheap for sessions that are merely waiting, so it reads one bounded
        transcript slice (never the whole log, the todo state, or the session
        row snapshot). The returned events carry the same runtime policy
        projection as a full replay, and the projection is only computed when
        the batch actually contains a policy-annotated event.
        """
        return self._inspection_coordinator.session_events_after(
            session_id=session_id,
            after_sequence=after_sequence,
        )

    def _load_session_result(self, *, session_id: str) -> RuntimeSessionResult:
        return self._inspection_coordinator._load_session_result(session_id=session_id)

    def read_tool_transcript(
        self,
        *,
        caller_session_id: str,
        session_id: str,
        limit: int | None = None,
    ) -> TranscriptRead:
        return self._inspection_coordinator.read_tool_transcript(caller_session_id=caller_session_id, session_id=session_id, limit=limit)

    def resolve_tool_output_artifact(
        self,
        *,
        session_id: str,
        artifact_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, object]:
        """Resolve spilled tool output artifact metadata for a session."""
        return self._inspection_coordinator.resolve_tool_output_artifact(
            session_id=session_id,
            artifact_id=artifact_id,
            tool_call_id=tool_call_id,
        )

    def read_tool_output_artifact(
        self,
        *,
        caller_session_id: str | None = None,
        session_id: str,
        artifact_id: str | None = None,
        tool_call_id: str | None = None,
        offset: int = 0,
        limit: int = 2000,
        byte_offset: int | None = None,
        byte_limit: int | None = None,
    ) -> ArtifactRead:
        """Read a bounded slice from a spilled tool output artifact."""
        return self._inspection_coordinator.read_tool_output_artifact(
            caller_session_id=caller_session_id,
            session_id=session_id,
            artifact_id=artifact_id,
            tool_call_id=tool_call_id,
            offset=offset,
            limit=limit,
            byte_offset=byte_offset,
            byte_limit=byte_limit,
        )

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
        return self._inspection_coordinator.search_tool_output_artifact(
            session_id=session_id,
            pattern=pattern,
            artifact_id=artifact_id,
            tool_call_id=tool_call_id,
            case_sensitive=case_sensitive,
            limit=limit,
        )

    def session_debug_snapshot(self, *, session_id: str) -> RuntimeSessionDebugSnapshot:
        return self._inspection_coordinator.session_debug_snapshot(session_id=session_id)

    def storage_diagnostics(self) -> dict[str, object]:
        return self._inspection_coordinator.storage_diagnostics()

    def export_session_bundle(
        self,
        *,
        session_id: str,
        options: SessionBundleOptions | None = None,
    ) -> SessionBundle:
        return self._inspection_coordinator.export_session_bundle(
            session_id=session_id,
            options=options,
        )

    def import_session_bundle_file(
        self,
        *,
        bundle_path: Path,
        dry_run: bool = False,
    ) -> SessionBundleImportResult:
        return self._inspection_coordinator.import_session_bundle_file(
            bundle_path=bundle_path,
            dry_run=dry_run,
        )

    def prune_runtime_storage(
        self,
        *,
        keep_sessions: int | None = None,
        keep_background_tasks: int | None = None,
        older_than: int | None = None,
    ) -> dict[str, int]:
        return self._inspection_coordinator.prune_runtime_storage(
            keep_sessions=keep_sessions,
            keep_background_tasks=keep_background_tasks,
            older_than=older_than,
        )

    def reset_runtime_storage(self) -> dict[str, object]:
        return self._inspection_coordinator.reset_runtime_storage()

    def run_config(self, *, session_id: str | None = None) -> RunConfig:
        return self._inspection_coordinator.run_config(session_id=session_id)

    def effective_agent_model_config(self, *, session_id: str | None = None) -> dict[str, object]:
        return self._inspection_coordinator.effective_agent_model_config(session_id=session_id)

    def _canonical_known_provider_name(self, provider_name: str) -> str:
        """Canonical id for a provider this runtime knows, or a loud error.

        ``provider_name`` is user input from the CLI, HTTP, or config. An id that
        is neither a built-in nor a declared ``providers.custom`` provider must
        not degrade into the generic endpoint provider, so the registry answers
        this question once and raises otherwise.
        """
        return self._inspection_coordinator._canonical_known_provider_name(provider_name)

    def refresh_provider_models(self, provider_name: str) -> tuple[str, ...]:
        return self._inspection_coordinator.refresh_provider_models(provider_name)

    def _hydrate_provider_model_catalog_cache(self) -> None:
        self._provider_catalog_cache.hydrate()

    def reasoning_effort_capability(self, config: RunConfig) -> ReasoningEffortCapability:
        """Resolve the reasoning-effort capability of a config's active provider/model target.

        Reasoning effort belongs to the model, so the model's own catalog metadata
        decides; the provider-level allowlist is only the fallback for models whose
        metadata is silent (see `resolve_reasoning_effort_capability`).
        """
        return self._inspection_coordinator.reasoning_effort_capability(config)

    def list_provider_summaries(self) -> tuple[ProviderSummary, ...]:
        return self._inspection_coordinator.list_provider_summaries()

    def provider_models_result(self, provider_name: str) -> ProviderModelsResult:
        return self._inspection_coordinator.provider_models_result(provider_name)

    def provider_readiness(self, *, session_id: str | None = None) -> ProviderReadinessResult:
        return self._inspection_coordinator.provider_readiness(session_id=session_id)

    def _reasoning_controls_diagnostic_for_config(
        self,
        config: RunConfig,
    ) -> dict[str, object] | None:
        return self._inspection_coordinator._reasoning_controls_diagnostic_for_config(config)

    def inspect_provider(self, provider_name: str) -> ProviderInspectResult:
        return self._inspection_coordinator.inspect_provider(provider_name)

    def validate_provider_credentials(self, provider_name: str) -> ProviderValidationResult:
        return self._inspection_coordinator.validate_provider_credentials(provider_name)

    def list_agent_summaries(self) -> tuple[AgentSummary, ...]:
        return self._inspection_coordinator.list_agent_summaries()

    def list_skill_summaries(self) -> tuple[SkillSummary, ...]:
        return self._inspection_coordinator.list_skill_summaries()

    def _resolve_skill(self, name: str) -> SkillMetadata:
        with self._skill_lock:
            return self._skill_registry.resolve(name)

    def set_skill_enabled(
        self,
        *,
        name: str,
        enabled: bool,
        scope: Literal["workspace", "user"] = "workspace",
    ) -> bool:
        if not name or not isinstance(name, str):
            raise ValueError("skill name must be a non-empty string")
        from .config.loading import load_runtime_config, save_skill_enablement_config

        save_skill_enablement_config(name.strip(), enabled, workspace=self._workspace, scope=scope)
        refreshed_config = load_runtime_config(self._workspace)
        if not self._skill_registry_is_injected:
            new_registry = self._stream_prep_coordinator.build_skill_registry(refreshed_config.skills)
            with self._skill_lock:
                self._config = refreshed_config
                self._skill_registry = new_registry
                self._inspection_coordinator.update_skill_registry(new_registry)
        else:
            with self._skill_lock:
                self._config = refreshed_config
                if self._config.skills and self._config.skills.config:
                    self._skill_registry.apply_config_rules(self._config.skills.config)
                elif name in self._skill_registry.skills:
                    from dataclasses import replace

                    self._skill_registry.skills[name] = replace(self._skill_registry.skills[name], enabled=enabled)
        return enabled

    def list_command_summaries(self) -> tuple[CommandSummary, ...]:
        return self._inspection_coordinator.list_command_summaries()

    def current_status(self) -> RuntimeStatusSnapshot:
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        self._background_task_supervisor.drain_queued_background_tasks()
        return self._inspection_coordinator.status_snapshot()

    @staticmethod
    def _acp_status_snapshot(acp_state: AcpAdapterState) -> CapabilityStatusSnapshot:
        return project_acp_status(acp_state)

    def retry_mcp_connections(self) -> RuntimeStatusSnapshot:
        self._mcp_manager.retry_connections(workspace=self._workspace)
        try:
            self._refresh_mcp_tools()
        except Exception:
            logger.debug("failed to refresh MCP tools after retry", exc_info=True)
        return self.current_status()

    def review_snapshot(self) -> WorkspaceReviewSnapshot:
        return self._inspection_coordinator.review_snapshot()

    def review_diff(self, path: str) -> ReviewFileDiff:
        return self._inspection_coordinator.review_diff(path)

    def review_diff_all(self) -> ReviewFileDiff:
        return self._inspection_coordinator.review_diff_all()

    def review_diff_base_branch(self, branch: str) -> ReviewFileDiff:
        return self._inspection_coordinator.review_diff_base_branch(branch)

    def review_diff_commit(self, sha: str) -> ReviewFileDiff:
        return self._inspection_coordinator.review_diff_commit(sha)

    def review_branches(self) -> tuple[str, ...]:
        return self._inspection_coordinator.review_branches()

    def review_current_branch(self) -> str | None:
        return self._inspection_coordinator.review_current_branch()

    def review_recent_commits(self, limit: int = 100) -> tuple[ReviewCommitLogEntry, ...]:
        return self._inspection_coordinator.review_recent_commits(limit=limit)

    def _fallback_reasoning_effort(self, review_model: str, parent_effort: str) -> str:
        provider_name, _, model_name = review_model.partition("/")
        if not model_name:
            model_name = provider_name
            provider_name = ""
        model_metadata = None
        if provider_name:
            model_metadata = self._inspection_coordinator._metadata_for_provider_model(provider_name, model_name)
        if model_metadata is None:
            for p in self._model_provider_registry.descriptors:
                meta = self._inspection_coordinator._metadata_for_provider_model(p, model_name)
                if meta is not None:
                    model_metadata = meta
                    break
        if model_metadata is not None:
            supported = model_metadata.supported_effort_levels
            if supported and parent_effort not in supported:
                middle_idx = max(0, len(supported) - 1) // 2
                return supported[middle_idx]
            if model_metadata.supports_reasoning_effort is False:
                return ""
        return parent_effort

    def review_session(
        self,
        *,
        session_id: str,
        focus: str | None = None,
        target: ReviewTarget | dict[str, object] | None = None,
    ) -> RuntimeResponse:
        """Run the read-only review agent over the specified review target."""
        validate_id(session_id)
        if ACTIVE_SESSION_REGISTRY.active_run_count(workspace=self._workspace, session_id=session_id):
            raise SessionBusyConflictError("cannot review a session while it is running")
        parent_response = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        parent_session = parent_response.session
        if parent_session.status in {"running", "waiting"}:
            raise SessionBusyConflictError(f"cannot review a session with status {parent_session.status}")

        resolved_target: ReviewTarget
        if target is None:
            if focus and focus.strip():
                resolved_target = ReviewTarget(type="custom", instructions=focus.strip())
            else:
                resolved_target = ReviewTarget(type="uncommitted")
        elif isinstance(target, ReviewTarget):
            resolved_target = target
        elif isinstance(target, dict):
            raw_type = target.get("type", "uncommitted")
            if raw_type in ("uncommittedChanges", "uncommitted"):
                resolved_target = ReviewTarget(type="uncommitted")
            elif raw_type in ("baseBranch", "base_branch"):
                resolved_target = ReviewTarget(type="base_branch", branch=str(target.get("branch") or ""))
            elif raw_type == "commit":
                resolved_target = ReviewTarget(type="commit", sha=str(target.get("sha") or ""), title=str(target.get("title") or "") or None)
            elif raw_type == "custom":
                resolved_target = ReviewTarget(type="custom", instructions=str(target.get("instructions") or ""))
            else:
                raise ValueError(f"unknown review target type '{raw_type}'")
        else:
            raise ValueError("review target must be a ReviewTarget or object")

        if resolved_target.type == "custom" and not resolved_target.instructions and focus and focus.strip():
            resolved_target = ReviewTarget(type="custom", instructions=focus.strip())

        diff: ReviewFileDiff
        prompt_parts: list[str]
        if resolved_target.type == "uncommitted":
            diff = self.review_diff_all()
            if diff.state == "not_git_repo":
                raise ValueError("review requires a Git repository")
            if diff.state == "clean" or not diff.diff:
                raise ValueError("review found no workspace changes")
            diff_bytes = len(diff.diff.encode("utf-8"))
            if diff_bytes > MAX_REVIEW_DIFF_BYTES:
                raise ValueError(
                    f"workspace diff is too large ({diff_bytes} bytes) for inline review; reduce working tree changes or review changes incrementally"
                )
            prompt_parts = [
                "Review the current workspace changes (staged, unstaged, and untracked files) and provide prioritized findings.",
                f"Aggregate Git diff:\n{diff.diff}",
                "Use the yield tool to return the final review findings.",
            ]
        elif resolved_target.type == "base_branch":
            if not resolved_target.branch:
                raise ValueError("base branch target requires a branch name")
            diff = self.review_diff_base_branch(resolved_target.branch)
            if diff.state == "not_git_repo":
                raise ValueError("review requires a Git repository")
            if diff.state == "clean" or not diff.diff:
                raise ValueError(f"review found no changes against base branch '{resolved_target.branch}'")
            diff_bytes = len(diff.diff.encode("utf-8"))
            if diff_bytes > MAX_REVIEW_DIFF_BYTES:
                raise ValueError(f"base branch diff is too large ({diff_bytes} bytes) for inline review; review changes incrementally")
            merge_base = WorkspaceReviewService(workspace=self._workspace).merge_base_sha(
                resolved_target.branch, git=self._inspection_coordinator._git_status_snapshot()
            )
            merge_base_text = f" The merge base commit for this comparison is {merge_base}." if merge_base else ""
            prompt_parts = [
                f"Review the code changes against the base branch '{resolved_target.branch}'.{merge_base_text}",
                f"Git diff against base branch '{resolved_target.branch}':\n{diff.diff}",
                "Provide prioritized, actionable findings. Use the yield tool to return the final review findings.",
            ]
        elif resolved_target.type == "commit":
            if not resolved_target.sha:
                raise ValueError("commit target requires a commit sha")
            diff = self.review_diff_commit(resolved_target.sha)
            if diff.state == "not_git_repo":
                raise ValueError("review requires a Git repository")
            if diff.state == "clean" or not diff.diff:
                raise ValueError(f"review found no changes in commit {resolved_target.sha}")
            diff_bytes = len(diff.diff.encode("utf-8"))
            if diff_bytes > MAX_REVIEW_DIFF_BYTES:
                raise ValueError(f"commit diff is too large ({diff_bytes} bytes) for inline review; review changes incrementally")
            title_text = f' ("{resolved_target.title}")' if resolved_target.title else ""
            prompt_parts = [
                f"Review the code changes introduced by commit {resolved_target.sha}{title_text}.",
                f"Git diff for commit {resolved_target.sha}:\n{diff.diff}",
                "Provide prioritized, actionable findings. Use the yield tool to return the final review findings.",
            ]
        elif resolved_target.type == "custom":
            instructions = (resolved_target.instructions or focus or "").strip()
            if not instructions:
                raise ValueError("custom review instructions must not be empty")
            diff = self.review_diff_all()
            if diff.state == "not_git_repo":
                raise ValueError("review requires a Git repository")
            if diff.state == "clean" or not diff.diff:
                raise ValueError("review found no workspace changes")
            diff_bytes = len(diff.diff.encode("utf-8"))
            if diff_bytes > MAX_REVIEW_DIFF_BYTES:
                raise ValueError(
                    f"workspace diff is too large ({diff_bytes} bytes) for inline review; reduce working tree changes or review changes incrementally"
                )
            prompt_parts = [
                "Review the current workspace changes and report concrete correctness, regression, and safety findings.",
                f"Reviewer focus: {instructions}",
                f"Aggregate Git diff:\n{diff.diff}",
                "Use the yield tool to return the final review findings.",
            ]
        else:
            raise ValueError(f"unsupported review target: {resolved_target.type}")

        review_prompt = "\n\n".join(prompt_parts)

        parent_model: str | None = None
        parent_effort: str | None = None
        raw_cap = parent_session.metadata.get("agent_capability_snapshot")
        if isinstance(raw_cap, dict):
            exec_meta = raw_cap.get("execution")
            if isinstance(exec_meta, dict):
                pm = exec_meta.get("model")
                if isinstance(pm, str):
                    parent_model = pm
                pe = exec_meta.get("reasoning_effort")
                if isinstance(pe, str):
                    parent_effort = pe
        raw_agent = parent_session.metadata.get("agent")
        if not parent_model and isinstance(raw_agent, dict):
            pm = raw_agent.get("model")
            if isinstance(pm, str):
                parent_model = pm
        if not parent_model:
            parent_model = self._config.model
        raw_effort = parent_session.metadata.get("reasoning_effort")
        if not parent_effort and isinstance(raw_effort, str):
            parent_effort = raw_effort
        if not parent_effort:
            parent_effort = self._config.reasoning_effort

        review_model = self._config.review_model or parent_model
        resolved_effort = parent_effort
        if review_model and parent_effort and review_model != parent_model:
            resolved_effort = self._fallback_reasoning_effort(review_model, parent_effort)

        agent_dict: dict[str, object] = {"preset": "review"}
        if review_model:
            agent_dict["model"] = review_model

        review_meta: dict[str, object] = {
            "agent": agent_dict,
            "provider_stream": True,
            "read_only": True,
            "review_child": True,
            "delegation": {
                "mode": "sync",
                "subagent_type": "review",
                "description": "Review the current workspace diff.",
            },
        }
        if resolved_effort:
            review_meta["reasoning_effort"] = resolved_effort

        return self.run(
            RuntimeRequest(
                prompt=review_prompt,
                parent_session_id=session_id,
                allocate_session_id=True,
                metadata=review_meta,
            )
        )

    def ask_side_question(self, *, session_id: str, question: str) -> dict[str, object]:
        """Answer a native ``/btw`` side question without touching the session transcript.

        Fork semantics (Claude ``sideQuestion.ts`` parity): the fork reuses the
        session's own assembled provider-context prefix (same model, same
        ``assemble_provider_context`` projection incl. replayed conversation)
        through the session's own turn producer, offers zero tools, and runs
        exactly one producer turn. No event is persisted, no metadata is
        written, and there is intentionally no busy gate: this works mid-turn.
        """
        validate_id(session_id)
        cleaned = question.strip()
        if not cleaned:
            raise ValueError("side question must not be empty")
        stored = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        session = stored.session
        # Same activation compact_session performs: a fresh runtime (e.g. the
        # HTTP server process) has no bound provider until the session's
        # frozen composition is activated.
        config = self._activate_existing_session_composition(session_id)
        replayed = self.replayed_conversation_segments_for_existing_session(stored=stored, parent_session_id=session.session.parent_id)
        tool_results = self._rehydrated_tool_results_for_existing_session(stored=stored, parent_session_id=session.session.parent_id)
        wrapped = (
            "This is a side question from the user. Answer it directly in a single response.\n"
            "You have NO tools available and there will be no follow-up turns; answer only from the conversation context.\n"
            "If you do not know the answer, say so — do not offer to look it up.\n\n"
            f"{cleaned}"
        )
        assembled = self.assemble_provider_context(
            prompt=wrapped,
            tool_results=tool_results,
            session_metadata=dict(session.metadata),
            replayed_conversation_segments=replayed,
        )
        turn_request = turn_request_for_session(
            TurnRequest(
                session=turn_session_snapshot(session),
                prompt=wrapped,
                available_tools=(),
                context_window=assembled.context_window,
                assembled_context=assembled,
                metadata={
                    "provider_attempt": 0,
                    "provider_stream": False,
                    **({"reasoning_effort": config.reasoning_effort} if config.reasoning_effort is not None else {}),
                },
                run_step=1,
            ),
            session,
        )
        producer = self.turn_producer_for_session_metadata(dict(session.metadata))
        plan = producer.produce(
            turn_request,
            tool_results=tuple(project_report(report) for report in tool_results if isinstance(report, ReportedCall)),
            session=turn_request.session,
        )
        if isinstance(plan, ToolTurn):
            attempted = plan.calls[0].tool_name if plan.calls else "a tool"
            answer = f"(The model tried to call {attempted} instead of answering directly. Try rephrasing or ask in the main conversation.)"
        elif isinstance(plan, FinalTurn):
            answer = plan.output
        else:
            raise ValueError(f"side question producer returned an unsupported turn plan: {type(plan).__name__}")
        if not answer.strip():
            raise ValueError("side question received an empty response")
        return {"session_id": session_id, "question": cleaned, "answer": answer}

    # -- native read-only info commands (/status /export /copy /context) --------

    def session_status_panel(self, *, session_id: str):
        """Project the read-only ``/status`` panel; safe mid-stream (no gate)."""
        return self._inspection_coordinator.session_status_panel(session_id=session_id)

    def export_session_markdown(self, *, session_id: str):
        """Render the full ``/export`` Markdown transcript; idle-only like Codex."""
        validate_id(session_id)
        if ACTIVE_SESSION_REGISTRY.active_run_count(workspace=self._workspace, session_id=session_id):
            raise SessionBusyConflictError("cannot export a session while it is running")
        response = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        if response.session.status in {"running", "waiting"}:
            raise SessionBusyConflictError(f"cannot export a session with status {response.session.status}")
        return self._inspection_coordinator.export_session_markdown(session_id=session_id)

    def write_session_export(self, *, session_id: str, path: str) -> dict[str, object]:
        """Write the ``/export`` transcript through the workspace fs (explicit user action, no approval gate)."""
        export = self.export_session_markdown(session_id=session_id)
        candidate = (self._workspace / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if not candidate.is_relative_to(self._workspace.resolve()):
            raise ValueError("export path must stay inside the workspace")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(export.markdown, encoding="utf-8")
        return {"session_id": session_id, "path": candidate.relative_to(self._workspace.resolve()).as_posix(), "filename": candidate.name}

    def copy_targets(self, *, session_id: str):
        """``/copy`` choices from the last assistant response; safe mid-stream (no gate)."""
        return self._inspection_coordinator.copy_targets(session_id=session_id)

    def session_context_usage(self, *, session_id: str, expand_items: bool = False):
        """``/context`` usage from the real assembly projection; safe mid-stream (no gate)."""
        return self._inspection_coordinator.session_context_usage(session_id=session_id, expand_items=expand_items)

    def session_mcp_inventory(self, *, session_id: str, verbose: bool = False):
        """``/mcp`` inventory; read-only projection, safe mid-stream (no gate)."""
        return self._inspection_coordinator.session_mcp_inventory(session_id=session_id, verbose=verbose)

    def session_hooks_listing(self, *, session_id: str):
        """``/hooks`` listing; read-only projection, safe mid-stream (no gate)."""
        return self._inspection_coordinator.session_hooks_listing(session_id=session_id)

    def session_skills_listing(self, *, session_id: str):
        """``/skills`` listing; read-only projection, safe mid-stream (no gate)."""
        return self._inspection_coordinator.session_skills_listing(session_id=session_id)

    def session_agents_overview(self, *, session_id: str):
        """``/agents`` overview; read-only projection, safe mid-stream (no gate)."""
        return self._inspection_coordinator.session_agents_overview(session_id=session_id)

    def session_token_usage(self, *, session_id: str, scope: str = "session"):
        """``/usage`` activity; read-only projection, safe mid-stream (no gate)."""
        return self._inspection_coordinator.session_token_usage(session_id=session_id, scope=scope)

    def run_user_shell(self, *, session_id: str | None, command: str, timeout_seconds: int | None = None) -> dict[str, object]:
        """Execute a native ``!`` shell command and record it in session history.

        Intentional consent bypass (see ``runtime/user_shell.py``): the typed
        ``!`` command IS explicit user consent, so there is no approval check,
        no permission gate, and no sandbox — not even under read-only/plan
        mode. Works idle AND mid-turn (no turn lifecycle is started, so there
        is no duplicate turn event); the output only lands in context for a
        later provider turn.
        """
        from .user_shell import (
            USER_SHELL_TIMEOUT_SECONDS,
            execute_user_shell_command,
            format_user_shell_record,
        )

        cleaned = command.strip()
        if not cleaned:
            raise ValueError("shell escape command must not be empty")
        if session_id is not None:
            validate_id(session_id)
        resolved_session_id = session_id or f"shell-{os.urandom(8).hex()}"
        started_sequence: int | None = None
        if self._repositories.sessions.has_session(workspace=self._workspace, session_id=resolved_session_id):
            stored = self._repositories.sessions.load_session(workspace=self._workspace, session_id=resolved_session_id)
            started_sequence = stored.events[-1].sequence if stored.events else 0
        else:
            self._admit_sessionless_user_shell(resolved_session_id)
        timeout = timeout_seconds if timeout_seconds is not None and timeout_seconds > 0 else USER_SHELL_TIMEOUT_SECONDS
        result = execute_user_shell_command(cleaned, cwd=self._workspace, timeout_seconds=timeout)
        record = format_user_shell_record(cleaned, result)
        # `completed`/`failed` rows seal against late events, exactly like a
        # follow-up re-entry: unseal through the interrupted checkpoint first
        # (same call `_assemble_stream_session_metadata` makes), preserving
        # the stored metadata and checkpoint identity.
        stored_now = self._repositories.sessions.load_session(workspace=self._workspace, session_id=resolved_session_id)
        if stored_now.session.status in {"completed", "failed"}:
            unseal_metadata = {k: v for k, v in stored_now.session.metadata.items() if k != "execution_composition"}
            self._finalize_coordinator.save_interrupted_checkpoint(
                session_id=resolved_session_id,
                prompt=f"!{cleaned}",
                session_metadata=unseal_metadata,
                tool_results=(),
                last_event_sequence=stored_now.events[-1].sequence if stored_now.events else 0,
                output=None,
                create_if_missing=False,
                turn=stored_now.session.turn,
                parent_session_id=stored_now.session.session.parent_id,
            )
        self._finalize_coordinator.persist_emitted_event(
            session_id=resolved_session_id,
            event_type="runtime.user_shell_executed",
            source="runtime",
            payload={
                "command": cleaned,
                "exit_code": result.exit_code,
                "status": result.status,
                "duration_seconds": result.duration_seconds,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "output": result.output,
                "truncated": result.truncated,
                "timed_out": result.timed_out,
                "record": record,
            },
        )
        reloaded = self._repositories.sessions.load_session(workspace=self._workspace, session_id=resolved_session_id)
        stored_event = reloaded.events[-1]
        return {
            "session_id": resolved_session_id,
            "command": cleaned,
            "exit_code": result.exit_code,
            "status": result.status,
            "duration_seconds": result.duration_seconds,
            "output": result.output,
            "truncated": result.truncated,
            "timed_out": result.timed_out,
            "event_sequence": stored_event.sequence,
            "started_sequence": started_sequence,
        }

    def _admit_sessionless_user_shell(self, session_id: str) -> None:
        """Create a minimal session row so a ``!`` escape can record history."""
        config = self.run_config_for_request(RuntimeRequest(prompt="", session_id=session_id, metadata={"provider_stream": True}))
        frozen_composition = self._prepare_execution_composition(config)
        composition_ref = frozen_composition.reference(
            workspace=str(self._workspace),
            owner=SessionCompositionOwner(kind="session", session_id=session_id),
        )
        metadata: dict[str, object] = {
            "workspace": str(self._workspace),
            "runtime_config": self._runtime_config_metadata(config),
            "runtime_state": runtime_state_metadata_payload(run_id=None, acp_state=self._acp_adapter.current_state()),
        }
        self._repositories.recovery.save_interrupted_checkpoint(
            workspace=self._workspace,
            session_id=session_id,
            prompt="",
            session_metadata=metadata,
            tool_results=(),
            last_event_sequence=0,
            composition_ref=composition_ref,
            composition=frozen_composition,
            output=None,
            create_if_missing=True,
            turn=0,
            parent_session_id=None,
        )

    def compact_session(self, *, session_id: str) -> dict[str, object]:
        """Compact one settled session through the existing context projection."""
        validate_id(session_id)
        if ACTIVE_SESSION_REGISTRY.active_run_count(workspace=self._workspace, session_id=session_id):
            raise SessionBusyConflictError("cannot compact a session while it is running")
        response = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        session = response.session
        if session.status in {"running", "waiting"}:
            raise SessionBusyConflictError(f"cannot compact a session with status {session.status}")
        self._activate_existing_session_composition(session_id)
        response = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        session = response.session
        replayed = self.replayed_conversation_segments_for_existing_session(stored=response, parent_session_id=None)
        tool_results = self._rehydrated_tool_results_for_existing_session(stored=response, parent_session_id=None)
        sequence = response.events[-1].sequence if response.events else 0
        previous_projection = continuity_state_from_session_metadata(session.metadata)
        source_parts = []
        if previous_projection is not None and previous_projection.summary_text:
            source_parts.append(f"Previous runtime summary:\n{previous_projection.summary_text}")
        source_parts.extend(f"{segment.role}: {segment.content or ''}" for segment in replayed if (segment.content or "").strip())
        source_text = "\n\n".join(source_parts)
        if source_text:
            source_text = (
                "Summarize this completed coding-agent conversation for future continuation. "
                "Preserve the objective, user constraints, decisions, files, verification, and open blockers. "
                "Do not answer the last request; output only the factual summary.\n\n" + source_text
            )
        summary = (
            self.summarize_continuity(
                tool_results=tool_results,
                session_metadata=dict(session.metadata),
                source_text=source_text,
            )
            if source_text
            else None
        )
        if summary:
            anchor = hashlib.sha256(f"{sequence}:{summary}".encode()).hexdigest()[:16]
            projection = ContextProjection(
                projection_id=f"native-{session_id}-{sequence}",
                source_event_sequence=sequence,
                summary_text=summary,
                recent_tail=tuple((segment.content or "")[:500] for segment in replayed[-4:]),
                source="native_manual",
            )
            window = RuntimeContextWindow(
                prompt="",
                compacted=True,
                compaction_reason="manual",
                original_tool_result_count=len(tool_results),
                retained_tool_result_count=0,
                continuity_state=projection,
                summary_anchor=anchor,
                summary_kind="model",
            )
        else:
            if not tool_results:
                return {"session_id": session_id, "compacted": False, "reason": "summary_unavailable"}
            config = self.run_config_from_metadata(dict(session.metadata))
            budget = compaction_budget_from_config(
                config.context_window,
                context_window=self._stream_prep_coordinator.context_budget_for_config(config),
                anchor_tokens=provider_usage_anchor_tokens(session.metadata),
            )
            window = self.assemble_provider_context(
                prompt="",
                tool_results=tool_results,
                session_metadata=dict(session.metadata),
                replayed_conversation_segments=replayed,
                compaction_budget=replace(budget, fit_payload=True),
            ).context_window
            if window is None or not window.compacted:
                return {"session_id": session_id, "compacted": False, "reason": "no_prunable_content"}
        updated = session_with_context_window_payload_metadata(session, window.metadata_payload())
        updated = session_with_context_compacted_state(
            updated,
            summary_anchor=window.summary_anchor,
            original_tool_result_count=window.original_tool_result_count,
            retained_tool_result_count=window.retained_tool_result_count,
        )
        event = self._repositories.sessions.append_native_context_compaction(
            workspace=self._workspace,
            session_id=session_id,
            metadata=dict(updated.metadata),
            payload=window.metadata_payload(),
            expected_sequence=sequence,
            expected_status=session.status,
        )
        return {
            "session_id": session_id,
            "compacted": True,
            "event_sequence": event.sequence,
            "original_tool_result_count": window.original_tool_result_count,
            "retained_tool_result_count": window.retained_tool_result_count,
        }

    def set_collaboration_mode(self, *, session_id: str, mode: str) -> dict[str, object]:
        """Set the native collaboration mode ('default' or 'plan') for an idle session."""
        validate_id(session_id)
        valid_mode = validate_collaboration_mode(mode)
        if ACTIVE_SESSION_REGISTRY.active_run_count(workspace=self._workspace, session_id=session_id):
            raise SessionBusyConflictError("cannot change collaboration mode while a session is running")
        response = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        session = response.session
        if session.status in {"running", "waiting"}:
            raise SessionBusyConflictError(f"cannot change collaboration mode for a session with status {session.status}")
        raw_previous_mode = session.metadata.get("collaboration_mode", "default")
        previous_mode = validate_collaboration_mode(raw_previous_mode)
        if previous_mode == valid_mode:
            return {
                "session_id": session_id,
                "collaboration_mode": valid_mode,
                "previous_mode": previous_mode,
                "unchanged": True,
            }
        sequence = response.events[-1].sequence if response.events else 0
        updated_metadata = dict(session.metadata)
        updated_metadata["collaboration_mode"] = valid_mode
        event = self._repositories.sessions.append_collaboration_mode_change(
            workspace=self._workspace,
            session_id=session_id,
            metadata=updated_metadata,
            mode=valid_mode,
            previous_mode=previous_mode,
            expected_sequence=sequence,
            expected_status=session.status,
        )
        return {
            "session_id": session_id,
            "collaboration_mode": valid_mode,
            "previous_mode": previous_mode,
            "event_sequence": event.sequence,
        }

    def session_model(self, *, session_id: str) -> dict[str, object]:
        """Return the current model and reasoning effort for a session."""
        validate_id(session_id)
        response = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        model, provider = session_model_identity(response.session.metadata)
        runtime_config = response.session.metadata.get("runtime_config")
        effort = None
        if isinstance(runtime_config, Mapping):
            raw_effort = runtime_config.get("reasoning_effort")
            if isinstance(raw_effort, str):
                effort = raw_effort
        if effort is None:
            effort = self._config.reasoning_effort
        return {
            "session_id": session_id,
            "model": model or self._config.model or "default",
            "provider": provider,
            "reasoning_effort": effort,
        }

    def set_session_model(
        self,
        *,
        session_id: str,
        model: str,
        reasoning_effort: str | None = None,
    ) -> dict[str, object]:
        """Switch session model (+ reasoning effort) immediately. Safe mid-stream."""
        validate_id(session_id)
        if not model or not model.strip():
            raise ValueError("model must be a non-empty string")
        model = model.strip()
        if reasoning_effort is not None:
            reasoning_effort = normalize_reasoning_effort(reasoning_effort)

        response = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        metadata = dict(response.session.metadata)
        raw_rc = metadata.get("runtime_config")
        runtime_config: dict[str, object] = dict(raw_rc) if isinstance(raw_rc, Mapping) else {}
        runtime_config["model"] = model
        if reasoning_effort is not None:
            runtime_config["reasoning_effort"] = reasoning_effort
        elif "reasoning_effort" in runtime_config:
            reasoning_effort = cast(str, runtime_config["reasoning_effort"])
        metadata["runtime_config"] = runtime_config
        self._repositories.sessions.update_session_metadata(
            workspace=self._workspace,
            session_id=session_id,
            metadata=metadata,
        )
        return {
            "session_id": session_id,
            "model": model,
            "reasoning_effort": reasoning_effort,
        }

    def list_available_models(self) -> tuple[dict[str, object], ...]:
        """List available models across configured providers from catalog query."""
        results: list[dict[str, object]] = []
        try:
            summaries = self.list_provider_summaries()
            for summary in summaries:
                provider_name = summary.name
                models_res = self.provider_models_result(provider_name)
                for model_name in models_res.models:
                    meta = models_res.model_metadata.get(model_name)
                    desc = f"{provider_name} model"
                    efforts = list(meta.supported_effort_levels) if meta and meta.supported_effort_levels else []
                    default_effort = meta.default_reasoning_effort if meta else None
                    supports_reasoning = meta.supports_reasoning if meta else None
                    supports_reasoning_effort = meta.supports_reasoning_effort if meta else None
                    results.append(
                        {
                            "model": f"{provider_name}/{model_name}",
                            "raw_model": model_name,
                            "provider": provider_name,
                            "description": desc,
                            "supports_reasoning": supports_reasoning,
                            "supports_reasoning_effort": supports_reasoning_effort,
                            "default_reasoning_effort": default_effort,
                            "supported_effort_levels": efforts,
                        }
                    )
        except Exception:
            pass
        return tuple(results)

    def session_permissions(self, *, session_id: str) -> dict[str, object]:
        """Return the current permission mode and approval policy for a session."""
        validate_id(session_id)
        response = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        runtime_config = response.session.metadata.get("runtime_config")
        mode = None
        if isinstance(runtime_config, Mapping):
            raw_mode = runtime_config.get("approval_mode")
            if isinstance(raw_mode, str):
                mode = raw_mode
        if mode is None:
            mode = self._permission_policy.mode
        read_only = runtime_effective_read_only_from_metadata(response.session.metadata)
        preset = "read-only" if mode == "ask" else ("auto" if mode == "write" else "full-access")
        return {
            "session_id": session_id,
            "approval_mode": mode,
            "preset": preset,
            "read_only": read_only,
        }

    def set_session_permissions(self, *, session_id: str, mode: str) -> dict[str, object]:
        """Set approval mode/permissions preset immediately. Safe mid-stream."""
        validate_id(session_id)
        normalized = mode.strip().lower()
        preset_map = {
            "read-only": "ask",
            "readonly": "ask",
            "ask": "ask",
            "auto": "write",
            "default": "write",
            "write": "write",
            "full-access": "yolo",
            "full_access": "yolo",
            "full": "yolo",
            "yolo": "yolo",
        }
        if normalized not in preset_map:
            raise ValueError(f"invalid permissions mode or preset: {mode!r}; expected one of: read-only, auto, full-access, ask, write, yolo")
        target_mode = preset_map[normalized]
        preset_id = "read-only" if target_mode == "ask" else ("auto" if target_mode == "write" else "full-access")

        self._permission_policy = replace(self._permission_policy, mode=target_mode)
        if hasattr(self, "_run_loop_coordinator") and self._run_loop_coordinator is not None:
            self._run_loop_coordinator._permission_policy = self._permission_policy

        response = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        metadata = dict(response.session.metadata)
        raw_rc = metadata.get("runtime_config")
        runtime_config: dict[str, object] = dict(raw_rc) if isinstance(raw_rc, Mapping) else {}
        runtime_config["approval_mode"] = target_mode
        metadata["runtime_config"] = runtime_config
        self._repositories.sessions.update_session_metadata(
            workspace=self._workspace,
            session_id=session_id,
            metadata=metadata,
        )
        return {
            "session_id": session_id,
            "approval_mode": target_mode,
            "preset": preset_id,
        }

    def list_pending_approvals(self, *, session_id: str) -> tuple[dict[str, object], ...]:
        """List pending approval requests waiting for user decision."""
        validate_id(session_id)
        approvals: list[dict[str, object]] = []
        pending = self._repositories.recovery.load_pending_approval(
            workspace=self._workspace,
            session_id=session_id,
        )
        if pending is not None:
            approvals.append(
                {
                    "request_id": pending.request_id,
                    "tool_name": pending.tool_name,
                    "arguments": pending.arguments,
                    "target_summary": pending.target_summary,
                    "reason": pending.reason,
                    "policy_mode": pending.policy_mode,
                    "session_id": session_id,
                }
            )
        try:
            tasks = self.list_background_tasks_by_parent_session(parent_session_id=session_id)
            for task in tasks:
                child_session_id = task.session_id
                if child_session_id:
                    child_pending = self._repositories.recovery.load_pending_approval(
                        workspace=self._workspace,
                        session_id=child_session_id,
                    )
                    if child_pending is not None:
                        approvals.append(
                            {
                                "request_id": child_pending.request_id,
                                "tool_name": child_pending.tool_name,
                                "arguments": child_pending.arguments,
                                "target_summary": child_pending.target_summary,
                                "reason": child_pending.reason,
                                "policy_mode": child_pending.policy_mode,
                                "session_id": child_session_id,
                            }
                        )
        except Exception:
            pass
        return tuple(approvals)

    def approve_pending_action(self, *, session_id: str, request_id: str | None = None) -> dict[str, object]:
        """Approve one pending or auto-denied action retry.

        Empty queue -> returns approved=False.
        Non-approvable entries ignored with warning.
        """
        validate_id(session_id)
        approvals = self.list_pending_approvals(session_id=session_id)
        if not approvals:
            return {
                "approved": False,
                "reason": "no_pending_approvals",
                "message": "No pending approvals in this session.",
            }

        target = None
        if request_id is not None and request_id.strip():
            req_id = request_id.strip()
            for app in approvals:
                if app["request_id"] == req_id:
                    target = app
                    break
            if target is None:
                return {
                    "approved": False,
                    "reason": "not_found",
                    "message": f"Approval request '{req_id}' not found or non-approvable.",
                }
        else:
            target = approvals[0]

        target_request_id = cast(str, target["request_id"])
        target_session_id = cast(str, target["session_id"])
        tool_name = cast(str, target.get("tool_name", "action"))
        target_summary = cast(str, target.get("target_summary", ""))

        try:
            self._claim_pending_approval(session_id=target_session_id, approval_request_id=target_request_id)
        except Exception:
            pass

        return {
            "approved": True,
            "session_id": target_session_id,
            "request_id": target_request_id,
            "tool_name": tool_name,
            "target_summary": target_summary,
            "message": f"Approved {tool_name}",
        }

    def get_goal(self, *, session_id: str) -> dict[str, object] | None:
        """Return the current thread goal for this session if one exists."""
        validate_id(session_id)
        response = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
        raw_goal = response.session.metadata.get("thread_goal")
        return dict(raw_goal) if isinstance(raw_goal, dict) else None

    def set_goal(
        self,
        *,
        session_id: str,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None = None,
        mode: str | None = None,
    ) -> dict[str, object]:
        """Set, update, or replace the thread goal for a session."""
        import time

        validate_id(session_id)
        from .goal import (
            VALID_GOAL_STATUSES,
            ThreadGoal,
            spill_goal_objective_if_needed,
        )

        with ACTIVE_SESSION_REGISTRY.goal_lock(workspace=self._workspace, session_id=session_id):
            response = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
            existing_goal = response.session.metadata.get("thread_goal")
            now = int(time.time() * 1000)

            if mode == "replace" or existing_goal is None:
                if objective is None or not objective.strip():
                    raise ValueError("Goal objective must not be empty.")
                clean_objective = objective.strip()
                spilled_obj, _ = spill_goal_objective_if_needed(self._workspace, clean_objective)
                goal_status = status if status in VALID_GOAL_STATUSES else "active"
                if token_budget is not None and token_budget <= 0:
                    raise ValueError("goal budgets must be positive when provided")
                new_goal = ThreadGoal(
                    thread_id=session_id,
                    objective=spilled_obj,
                    status=goal_status,  # type: ignore[arg-type]
                    token_budget=token_budget,
                    tokens_used=0,
                    time_used_seconds=0,
                    created_at=now,
                    updated_at=now,
                )
            else:
                assert isinstance(existing_goal, dict)
                current_goal = ThreadGoal.from_payload(existing_goal)
                if objective is not None:
                    clean_objective = objective.strip()
                    if not clean_objective:
                        raise ValueError("Goal objective must not be empty.")
                    spilled_obj, _ = spill_goal_objective_if_needed(self._workspace, clean_objective)
                else:
                    spilled_obj = current_goal.objective

                goal_status = status if status in VALID_GOAL_STATUSES else current_goal.status
                if token_budget is not None:
                    if token_budget <= 0:
                        raise ValueError("goal budgets must be positive when provided")
                    budget = token_budget
                else:
                    budget = current_goal.token_budget

                new_goal = ThreadGoal(
                    thread_id=session_id,
                    objective=spilled_obj,
                    status=goal_status,  # type: ignore[arg-type]
                    token_budget=budget,
                    tokens_used=current_goal.tokens_used,
                    time_used_seconds=current_goal.time_used_seconds,
                    created_at=current_goal.created_at,
                    updated_at=now,
                )

            goal_payload = new_goal.to_payload()
            self._repositories.sessions.append_thread_goal_change(
                workspace=self._workspace,
                session_id=session_id,
                goal=goal_payload,
                event_type="runtime.thread_goal_updated",
            )
            return goal_payload

    def clear_goal(self, *, session_id: str) -> bool:
        """Clear the thread goal for a session."""
        validate_id(session_id)
        with ACTIVE_SESSION_REGISTRY.goal_lock(workspace=self._workspace, session_id=session_id):
            response = self._repositories.sessions.load_session(workspace=self._workspace, session_id=session_id)
            if "thread_goal" not in response.session.metadata:
                return False
            self._repositories.sessions.append_thread_goal_change(
                workspace=self._workspace,
                session_id=session_id,
                goal=None,
                event_type="runtime.thread_goal_cleared",
            )
            return True

    @staticmethod
    def _decode_subprocess_text_output(value: object) -> str:
        from .coordinators.inspection import _decode_subprocess_text_output as _shared_decode

        return _shared_decode(value)

    def web_settings(self) -> dict[str, object]:
        return self._inspection_coordinator.web_settings()

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
            provider_name, model_name = split_provider_model_reference(model)
            _ = self._canonical_known_provider_name(provider_name)
            config_path = self._workspace / ".voidcode.json"
            payload = self._read_json_object(config_path)
            # Store the canonical provider id; the model id is the vendor's own.
            payload["model"] = f"{provider_name}/{model_name}"
            config_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self._reload_runtime_config_state()
        return self.web_settings()

    @staticmethod
    def _read_json_object(config_path: Path) -> dict[str, object]:
        if not config_path.exists():
            return {}
        raw_payload: object = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw_payload, dict):
            raise ValueError(f"runtime config file must contain a JSON object: {config_path}")
        return raw_payload

    def _reload_runtime_config_state(self) -> None:
        """Atomically refresh the provider/model state used by web settings.

        ``update_web_settings`` is a deliberately narrow hot-reload surface:
        it changes only global provider credentials/selection and the workspace
        model. Agent, permission, hook, capability, and background-task config
        remain runtime-lifetime state; reloading those collaborators while a
        run is active would leave a partially updated control plane. Stage every
        provider-derived object first, then publish the complete provider state
        in one commit.
        """
        refreshed_config = load_runtime_config(self._workspace)
        model = refreshed_config.model
        if self._config.agent is not None and self._config.agent.model is not None:
            model = self._config.agent.model
        provider_registry = ModelProviderRegistry.with_defaults(provider_configs=refreshed_config.providers)
        provider_catalog_cache = RuntimeProviderCatalogCache(
            registry=provider_registry,
            path=provider_catalog_cache_path(),
        )
        provider_catalog_query = RuntimeProviderCatalogQuery(registry=provider_registry)
        provider_catalog_cache.hydrate()
        provider_auth_resolver = ProviderAuthResolver(
            providers=refreshed_config.providers,
            env=os.environ,
        )
        provider_auth_inspector = RuntimeProviderAuthInspector(
            providers=refreshed_config.providers,
            resolver=provider_auth_resolver,
            env=os.environ,
        )
        provider_fallback = self._initial_config.provider_fallback
        resolved_provider_config = resolve_provider_config(
            model,
            provider_fallback,
            registry=provider_registry,
        )
        config = replace(
            self._initial_config,
            model=model,
            providers=refreshed_config.providers,
            resolved_provider=resolved_provider_config,
        )

        # Commit only after all parsing, provider resolution, and turn-producer creation
        # succeeded. No collaborator that owns local governance is replaced by
        # this provider/model-only reload.
        self._config = replace(self._config, model=model, providers=refreshed_config.providers)
        self._model_provider_registry = provider_registry
        self._provider_catalog_cache = provider_catalog_cache
        self._provider_catalog_query = provider_catalog_query
        self._provider_auth_resolver = provider_auth_resolver
        self._provider_auth_inspector = provider_auth_inspector
        self._run_loop_coordinator.update_provider_catalog_query(provider_catalog_query)
        self._resolved_provider_config = resolved_provider_config
        self._provider_model = resolved_provider_config.active_target
        self._provider_chain = resolved_provider_config.target_chain
        self._initial_config = config
        self._inspection_coordinator.update_provider_state(
            config=self._config,
            model_provider_registry=provider_registry,
            provider_catalog_cache=provider_catalog_cache,
            provider_catalog_query=provider_catalog_query,
            provider_auth_inspector=provider_auth_inspector,
            provider_auth_resolver=provider_auth_resolver,
            resolved_provider_config=resolved_provider_config,
            provider_model=resolved_provider_config.active_target,
        )

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
    def _last_tool_summary(result: RuntimeSessionResult) -> RuntimeSessionDebugToolSummary | None:
        return last_tool_summary(result)

    def _provider_context_snapshot_for_assembled_context(
        self,
        *,
        assembled_context: RuntimeAssembledContext,
        config: RunConfig,
    ) -> RuntimeProviderContextSnapshot:
        active_target = config.resolved_provider.active_target
        provider = active_target.selection.provider or "unknown"
        model = active_target.selection.model or active_target.selection.raw_model or "unknown"
        tool_registry = self.tool_registry_for_config(config)
        context_window_config = config.context_window or RuntimeContextWindowConfig()
        return inspect_provider_context(
            assembled_context=assembled_context,
            provider=provider,
            model=model,
            available_tool_count=len(self.provider_tool_definitions(tool_registry, config)),
            tool_feedback_mode=self._tool_feedback_mode_for_config(config),
            oversized_tool_feedback_chars=(context_window_config.provider_context_oversized_feedback_chars),
            diagnostic_policy_mode=context_window_config.provider_context_diagnostics,
        )

    @staticmethod
    def _tool_feedback_mode_for_config(
        config: RunConfig,
    ) -> ToolFeedbackMode:
        active_target = config.resolved_provider.active_target
        if active_target.metadata is not None and active_target.metadata.tool_feedback_mode is not None:
            return active_target.metadata.tool_feedback_mode
        return "standard"

    def provider_context_policy_decision_for_turn_request(
        self,
        *,
        turn_request: TurnRequest,
        config: RunConfig,
    ) -> RuntimeProviderContextPolicyDecision | None:
        return self._inspection_coordinator.provider_context_policy_decision_for_turn_request(
            turn_request=turn_request,
            config=config,
        )

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

    @staticmethod
    def _should_prefer_active_debug_snapshot(
        *,
        result: RuntimeSessionResult,
        active_metadata: dict[str, object] | None,
    ) -> bool:
        if active_metadata is None:
            return False
        active_run_id = active_metadata.get("run_id")
        persisted_run_id = run_id_from_session_metadata(result.session.metadata)
        if isinstance(active_run_id, str) and active_run_id != persisted_run_id:
            return True
        request_metadata = active_metadata.get("request_metadata")
        if not isinstance(request_metadata, dict):
            return False
        active_request_metadata = VoidCodeRuntime._fresh_request_metadata(request_metadata)
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
            "provider_stream",
            "reasoning_effort",
            "skills",
            "background_run",
            "background_task_id",
        }
        request_metadata = {key: value for key, value in metadata.items() if key in request_metadata_keys}
        return VoidCodeRuntime._fresh_request_metadata(request_metadata)

    def queue_steering(
        self,
        session_id: str,
        content: str,
        *,
        dedupe_key: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Persist a message to deliver before the next provider turn."""
        return self._queue_runtime_message(
            session_id,
            content=content,
            kind="steering",
            dedupe_key=dedupe_key,
        )

    def queue_follow_up(self, session_id: str, content: str) -> tuple[dict[str, object], ...]:
        """Persist a message to deliver after the current run reaches idle."""
        return self._queue_runtime_message(session_id, content=content, kind="follow_up")

    def queue_completion_interaction(
        self,
        session_id: str,
        content: str,
        *,
        dedupe_key: str,
    ) -> tuple[dict[str, object], ...]:
        """Persist one bounded child-completion interaction for parent delivery."""
        return self._queue_runtime_message(
            session_id,
            content=content,
            kind="steering",
            dedupe_key=dedupe_key,
            allow_terminal_completion=True,
        )

    def queue_progress_interaction(
        self,
        session_id: str,
        content: str,
        *,
        dedupe_key: str,
    ) -> tuple[dict[str, object], ...]:
        """Persist one bounded nonterminal child-progress message for parent delivery."""
        return self._queue_runtime_message(
            session_id,
            content=content,
            kind="steering",
            dedupe_key=dedupe_key,
            allow_terminal_completion=True,
        )

    def _queue_runtime_message(
        self,
        session_id: str,
        *,
        content: str,
        kind: Literal["steering", "follow_up"],
        dedupe_key: str | None = None,
        allow_terminal_completion: bool = False,
    ) -> tuple[dict[str, object], ...]:
        validate_id(session_id)
        self._admit_existing_session_composition(session_id)
        # Steering/follow-up is rejected once the session is sealed. A
        # completion interaction is a runtime outbox record created only after
        # its parent lifecycle event is durable, so it is allowed for backfill.
        sealed_status = self._sealed_session_status(session_id=session_id)
        if sealed_status is not None and not allow_terminal_completion:
            raise SessionSealedError(f"session {session_id!r} is {sealed_status}: refusing to queue {kind} message on a terminal session")
        return self._repositories.sessions.enqueue_session_message(
            workspace=self._workspace,
            session_id=session_id,
            content=content,
            kind=kind,
            dedupe_key=dedupe_key,
        )

    def drain_queued_messages(
        self,
        session_id: str,
        *,
        kind: Literal["steering", "follow_up"],
    ) -> tuple[str, ...]:
        validate_id(session_id)
        if not self._repositories.sessions.has_session(workspace=self._workspace, session_id=session_id):
            return ()
        messages = self._repositories.sessions.drain_session_messages(
            workspace=self._workspace,
            session_id=session_id,
            kind=kind,
            remember_dedupe=True,
        )
        return tuple(message.content for message in messages)

    def pending_tool_intent(self, session_id: str) -> dict[str, object] | None:
        """Return an unsettled tool intent left by an interrupted process."""
        validate_id(session_id)
        response = self._load_stored_response(session_id=session_id)
        intent = runtime_state_pending_tool_intent(response.session.metadata)
        return intent if intent is not None else None

    @contextmanager
    def _active_resume_registration(
        self,
        session_id: str,
        *,
        resume_kind: str,
        extra_metadata: Mapping[str, object] | None = None,
    ) -> Generator[tuple[str, ProviderAbortSignal]]:
        run_id = os.urandom(8).hex()
        abort_signal = self._register_active_session_id(
            session_id,
            run_id=run_id,
            metadata={
                "resume": True,
                "resume_kind": resume_kind,
                "run_id": run_id,
                **(dict(extra_metadata) if extra_metadata is not None else {}),
            },
        )
        try:
            yield run_id, abort_signal
        finally:
            self._unregister_active_session_id(session_id, run_id=run_id)

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> RuntimeResponse:
        validate_id(session_id)
        self._admit_existing_session_composition(session_id)
        if approval_request_id is None and approval_decision is None:
            checkpoint = self._resume_coordinator.load_resume_checkpoint(session_id=session_id)
            if checkpoint is not None and checkpoint.get("kind") in {"provider_failure_retryable", "interrupted"}:
                self._activate_existing_session_composition(session_id)
            _ = self._reconcile_resolved_approval(session_id=session_id)
            checkpoint = self._resume_coordinator.load_resume_checkpoint(session_id=session_id)
            if checkpoint is not None and checkpoint.get("kind") == "provider_failure_retryable":
                self._background_task_supervisor.reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
                with self._active_resume_registration(session_id, resume_kind="provider_failure") as (run_id, abort_signal):
                    return self._resume_coordinator.resume_provider_failure_response(
                        session_id=session_id,
                        checkpoint=checkpoint,
                        finalize_background_task=True,
                        run_id=run_id,
                        abort_signal=abort_signal,
                    )
            if checkpoint is not None and checkpoint.get("kind") == "interrupted":
                self._background_task_supervisor.reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
                with self._active_resume_registration(session_id, resume_kind="interrupted") as (run_id, abort_signal):
                    return self._resume_coordinator.resume_interrupted_response(
                        session_id=session_id,
                        checkpoint=checkpoint,
                        finalize_background_task=True,
                        run_id=run_id,
                        abort_signal=abort_signal,
                    )
            self._background_task_supervisor.reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
            return self._load_replay_response(session_id=session_id)
        if approval_request_id is None or approval_decision is None:
            raise ValueError("approval resume requires request id and decision")
        self._validate_resume_targets_owned_request(
            session_id=session_id,
            approval_request_id=approval_request_id,
        )
        self._activate_existing_session_composition(session_id)
        with ACTIVE_SESSION_REGISTRY.approval_resolution_lock(
            workspace=self._workspace,
            session_id=session_id,
        ):
            self._claim_pending_approval(
                session_id=session_id,
                approval_request_id=approval_request_id,
            )
            with self._active_resume_registration(
                session_id,
                resume_kind="approval",
                extra_metadata={"approval_request_id": approval_request_id},
            ) as (run_id, abort_signal):
                _, response = self._resume_coordinator.resume_pending_approval_response(
                    session_id=session_id,
                    approval_request_id=approval_request_id,
                    approval_decision=approval_decision,
                    run_id=run_id,
                    abort_signal=abort_signal,
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
        validate_id(session_id)
        self._admit_existing_session_composition(session_id)
        if approval_request_id is None and approval_decision is None:
            checkpoint = self._resume_coordinator.load_resume_checkpoint(session_id=session_id)
            if checkpoint is not None and checkpoint.get("kind") in {"provider_failure_retryable", "interrupted"}:
                self._activate_existing_session_composition(session_id)
            if checkpoint is not None and checkpoint.get("kind") == "provider_failure_retryable":
                self._background_task_supervisor.reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
                with self._active_resume_registration(
                    session_id,
                    resume_kind="provider_failure_retryable",
                ) as (run_id, abort_signal):
                    yield from self._resume_coordinator.resume_provider_failure_stream(
                        session_id=session_id,
                        checkpoint=checkpoint,
                        run_id=run_id,
                        abort_signal=abort_signal,
                        finalize_background_task=True,
                    )
                return
            if checkpoint is not None and checkpoint.get("kind") == "interrupted":
                self._background_task_supervisor.reconcile_parent_background_task_events_for_session(parent_session_id=session_id)
                with self._active_resume_registration(
                    session_id,
                    resume_kind="interrupted",
                ) as (run_id, abort_signal):
                    yield from self._resume_coordinator.resume_interrupted_stream(
                        session_id=session_id,
                        checkpoint=checkpoint,
                        run_id=run_id,
                        abort_signal=abort_signal,
                        finalize_background_task=True,
                    )
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
        self._activate_existing_session_composition(session_id)
        with ACTIVE_SESSION_REGISTRY.approval_resolution_lock(
            workspace=self._workspace,
            session_id=session_id,
        ):
            self._claim_pending_approval(
                session_id=session_id,
                approval_request_id=approval_request_id,
            )
            with self._active_resume_registration(
                session_id,
                resume_kind="approval",
                extra_metadata={"approval_request_id": approval_request_id},
            ) as (run_id, abort_signal):
                yield from self._resume_coordinator.resume_pending_approval_stream(
                    session_id=session_id,
                    approval_request_id=approval_request_id,
                    approval_decision=approval_decision,
                    run_id=run_id,
                    abort_signal=abort_signal,
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
        pending = self._repositories.recovery.load_pending_approval(
            workspace=self._workspace,
            session_id=session_id,
        )
        if pending is None:
            return
        if pending.owner_session_id is not None and pending.owner_session_id != session_id:
            raise ValueError("approval resume must target the child session that owns the approval request")
        if pending.request_id != approval_request_id:
            return

    def _reconcile_resolved_approval(self, *, session_id: str) -> bool:
        pending = self._repositories.recovery.load_pending_approval(
            workspace=self._workspace,
            session_id=session_id,
        )
        if pending is None:
            return False
        return bool(
            self._repositories.recovery.reconcile_resolved_approval(
                workspace=self._workspace,
                session_id=session_id,
                request_id=pending.request_id,
            )
        )

    def _claim_pending_approval(self, *, session_id: str, approval_request_id: str) -> None:
        self._resume_coordinator.claim_pending_approval(
            session_id=session_id,
            approval_request_id=approval_request_id,
        )

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
        for task_summary in self._repositories.tasks.list_background_tasks_by_parent_session(
            workspace=self._workspace,
            parent_session_id=session_id,
        ):
            task = self._repositories.tasks.load_background_task(
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
        responses: QuestionAnswer,
    ) -> RuntimeResponse:
        validate_id(session_id)
        self._admit_existing_session_composition(session_id)
        self._validate_question_targets_owned_request(
            session_id=session_id,
            question_request_id=question_request_id,
        )
        self._activate_existing_session_composition(session_id)
        with self._active_resume_registration(
            session_id,
            resume_kind="question",
            extra_metadata={"question_request_id": question_request_id},
        ) as (run_id, abort_signal):
            _, response = self._resume_coordinator.answer_pending_question_response(
                session_id=session_id,
                question_request_id=question_request_id,
                responses=responses,
                run_id=run_id,
                abort_signal=abort_signal,
            )
            self._background_task_supervisor.finalize_background_task_from_session_response(session_response=response)
            return response

    def answer_question_stream(
        self,
        session_id: str,
        *,
        question_request_id: str,
        responses: QuestionAnswer,
    ) -> Iterator[RuntimeStreamChunk]:
        validate_id(session_id)
        self._admit_existing_session_composition(session_id)
        self._validate_question_targets_owned_request(
            session_id=session_id,
            question_request_id=question_request_id,
        )
        self._activate_existing_session_composition(session_id)
        with self._active_resume_registration(
            session_id,
            resume_kind="question",
            extra_metadata={"question_request_id": question_request_id},
        ) as (run_id, abort_signal):
            yield from self._resume_coordinator.answer_pending_question_stream(
                session_id=session_id,
                question_request_id=question_request_id,
                responses=responses,
                run_id=run_id,
                abort_signal=abort_signal,
                finalize_background_task=True,
            )

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
            event.event_type == "core.response_ready"
            or (event.event_type == "core.loop_step" and event.payload.get("phase") == "finalize")
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
            session_id = validate_id(session_id)

        parent_session_id = request.parent_session_id
        if parent_session_id is not None:
            parent_session_id = validate_id(parent_session_id, field_name="parent_session_id")
        if session_id is not None and parent_session_id == session_id:
            raise RuntimeRequestError("parent_session_id must not match session_id")

        existing_session = self._load_existing_session_if_present(session_id=session_id) if session_id is not None else None
        governance_parent_session_id = parent_session_id
        if governance_parent_session_id is None and existing_session is not None:
            governance_parent_session_id = existing_session.session.session.parent_id
        raw_metadata = {key: value for key, value in request.metadata.items()}
        if existing_session is not None and existing_session.session.session.parent_id is not None and request.parent_session_id is None:
            persisted = existing_session.session.metadata
            if "delegation" not in raw_metadata and isinstance(persisted.get("delegation"), dict):
                raw_delegation = cast(Mapping[str, object], persisted["delegation"])
                raw_metadata["delegation"] = dict(raw_delegation)
            runtime_config = persisted.get("runtime_config")
            if "agent" not in raw_metadata and isinstance(runtime_config, dict) and isinstance(runtime_config.get("agent"), dict):
                persisted_agent = parse_runtime_agent_payload(
                    runtime_config["agent"],
                    source="persisted child runtime_config.agent",
                    hooks=self._config.hooks,
                    agent_registry=self._session_agent_registry(persisted),
                    allow_runtime_internal=True,
                )
                raw_metadata["agent"] = serialize_runtime_agent_config(persisted_agent)
        if governance_parent_session_id is not None:
            parent_metadata = self._parent_policy_metadata(governance_parent_session_id)
            if parent_metadata is not None:
                raw_metadata = self._metadata_with_inherited_child_policy(
                    child_metadata=raw_metadata,
                    parent_metadata=parent_metadata,
                )
        metadata = validate_runtime_request_metadata(
            raw_metadata,
            allow_internal_fields=allow_internal_metadata,
        )
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

        _validate_prompt_slash_command(request.prompt)

        return RuntimeRequest(
            prompt=request.prompt,
            session_id=session_id,
            parent_session_id=resolved_parent_session_id,
            metadata=metadata,
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
        # A read-only parent stance is the shared gate both layers consume,
        # and the more restrictive parent stance propagates to the child
        # through the single read_only key.
        parent_read_only = runtime_effective_read_only_from_metadata(parent_metadata)
        child_read_only = runtime_read_only_from_metadata(child_metadata)
        if parent_read_only or child_read_only or "read_only" in child_metadata:
            inherited["read_only"] = parent_read_only or child_read_only
        return inherited

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

        normalized: dict[str, object] = dict(metadata)
        delegation: dict[str, object] = dict(raw_delegation)
        parent_depth = 0
        remaining_spawn_budget = _DELEGATION_GOVERNANCE.spawn_budget

        if parent_session_id is not None:
            parent_response = self._load_existing_session_if_present(session_id=parent_session_id)
            if parent_response is not None:
                parent_depth = delegation_depth_from_metadata(parent_response.session.metadata)
                remaining_spawn_budget = remaining_spawn_budget_from_metadata(parent_response.session.metadata)
            elif (active_parent_metadata := self._active_session_metadata(parent_session_id)) is not None:
                parent_depth = delegation_depth_from_metadata(active_parent_metadata)
                remaining_spawn_budget = remaining_spawn_budget_from_metadata(active_parent_metadata)

        request_depth = parent_depth + 1
        if request_depth > _DELEGATION_GOVERNANCE.max_depth:
            raise RuntimeRequestError(
                f"delegation depth limit exceeded: requested depth {request_depth} exceeds max {_DELEGATION_GOVERNANCE.max_depth}"
            )

        if existing_session_id is None:
            if remaining_spawn_budget < 1:
                raise RuntimeRequestError("delegation spawn budget exhausted for parent session")
            remaining_spawn_budget -= 1
        # Spawn-budget invariant: the budget is debited exactly once per *new*
        # child session (``existing_session_id is None``). Keep-alive re-entry
        # reuses the same child session (``task.session_id``), so every steer
        # turn is an existing-session run and never re-debits the budget;
        # depth is recomputed from the parent on each turn and does not drift.

        delegation["depth"] = request_depth
        delegation["remaining_spawn_budget"] = remaining_spawn_budget
        normalized["delegation"] = parse_delegation_metadata(delegation)
        validated = validate_runtime_request_metadata(
            normalized,
            allow_internal_fields=(
                "background_run" in normalized
                or "background_rate_limit_retry" in normalized
                or "background_task_id" in normalized
                or "keep_alive_turn" in normalized
            ),
        )
        return validated

    def prepare_provider_context_window(
        self,
        *,
        prompt: str,
        tool_results: tuple[ReportedCall | ToolResultView, ...],
        session_metadata: dict[str, object],
        policy: ContextWindowPolicy | None = None,
        abort_signal: ProviderAbortSignal | None = None,  # noqa: ARG002 — retained by RuntimeSurface protocol for abort-aware callers.
        before_compact: BeforeCompactInput | None = None,
    ) -> RuntimeContextWindow:
        return self._stream_prep_coordinator.prepare_provider_context_window(
            prompt=prompt,
            tool_results=tool_results,
            session_metadata=session_metadata,
            policy=policy,
            abort_signal=abort_signal,
            before_compact=before_compact,
        )

    def _rehydrated_tool_results_for_existing_session(
        self,
        *,
        stored: RuntimeResponse | None = None,
        session_id: str | None = None,
        parent_session_id: str | None,
    ) -> tuple[ReportedCall, ...]:
        if stored is None and session_id is not None:
            stored = self._load_existing_session_if_present(session_id=session_id)
        if stored is None:
            return ()
        stored_parent_session_id = stored.session.session.parent_id
        if parent_session_id is not None and stored_parent_session_id != parent_session_id:
            return ()
        # The replay source is the session's root→leaf *path*, never the flat
        # event log: under a tree the flat log also holds the abandoned
        # branches, so rehydrating from it would leak a checked-out-away tool
        # result into the provider context. ``stored.events`` cannot be reused
        # for the walk (``load_session`` returns every row without the
        # ``parent_sequence`` edges), so the one authoritative path walk in
        # storage is called again — one extra read per rehydration,
        # deliberately, rather than a second implementation of the walk here.
        path_events = self._repositories.events.session_path(
            workspace=self._workspace,
            session_id=stored.session.session.id,
        )
        path_events = events_after_manual_compaction(path_events)
        _prompt, tool_results = prompt_and_tool_results_from_debug_events(path_events)
        return tuple(tool_results)

    def replayed_conversation_segments_for_existing_session(
        self,
        *,
        stored: RuntimeResponse | None = None,
        session_id: str | None = None,
        parent_session_id: str | None,
        include_current_request: bool = False,
    ) -> tuple[ContextSegment, ...]:
        if stored is None and session_id is not None:
            stored = self._load_existing_session_if_present(session_id=session_id)
        if stored is None:
            return ()
        stored_parent_session_id = stored.session.session.parent_id
        if parent_session_id is not None and stored_parent_session_id != parent_session_id:
            return ()
        # The replay source is the session's root→leaf *path*, never the flat
        # event log: under a tree the flat log also holds the abandoned
        # branches, so replaying it would leak a checked-out-away prompt or
        # tool result into the provider context. ``stored.events`` cannot be
        # reused for the walk (``load_session`` returns every row without the
        # ``parent_sequence`` edges), so the one authoritative path walk in
        # storage is called again — one extra read per replay, deliberately,
        # rather than a second implementation of the walk here.
        replay_events = self._repositories.events.session_path(
            workspace=self._workspace,
            session_id=stored.session.session.id,
        )
        replay_events = events_after_manual_compaction(replay_events)
        current_request_event_id = next(
            (
                (event.session_id, event.sequence)
                for event in reversed(replay_events)
                if include_current_request and event.event_type == "runtime.request_received" and event.session_id == stored.session.session.id
            ),
            None,
        )

        def report_from_event(event: EventEnvelope) -> ReportedCall | None:
            return reported_call_from_event(event)

        return replayed_conversation_segments_from_events(
            replay_events,
            reported_call_from_event=report_from_event,
            current_request_event_id=current_request_event_id,
        )

    def assemble_provider_context(
        self,
        *,
        prompt: str,
        tool_results: tuple[ReportedCall | ToolResultView, ...],
        session_metadata: dict[str, object],
        skill_prompt_context: str = "",
        replayed_conversation_segments: tuple[ContextSegment, ...] = (),
        tool_registry: ToolRegistry | None = None,
        hook_guidance: Iterable[str] | None = None,
        reminder_segment: ContextSegment | None = None,
        compaction_budget: CompactionBudget | None = None,
        before_compact: BeforeCompactInput | None = None,
        continuity_summary_override: str | None = None,
        continuity_summary_kind: ContinuitySummaryKind | None = None,
    ) -> RuntimeAssembledContext:
        config = self.run_config_from_metadata(session_metadata)
        if tool_registry is None:
            tool_registry = self.tool_registry_for_config(
                config,
                metadata=session_metadata,
            )
        tool_catalog_context = tool_registry.capability_catalog_prompt(
            essential_only=(config.tools is not None and config.tools.essential_only is True),
            allowlist_patterns=agent_required_tool_patterns(config.agent),
        )
        policy = context_window_policy_from_config(config.context_window)
        raw_loaded = session_metadata.get("loaded_skills", [])
        loaded_skills: tuple[dict[str, object], ...] = ()
        if isinstance(raw_loaded, list):
            typed: list[dict[str, object]] = []
            for item in raw_loaded:
                if isinstance(item, dict):
                    entry: dict[str, object] = {}
                    item_entries: Mapping[str, object] = item
                    for k, v in item_entries.items():
                        if isinstance(k, str):
                            entry[k] = v
                    typed.append(entry)
            loaded_skills = tuple(typed)
        agent = config.agent
        agent_prompt_materialization: AgentPromptMaterialization | None = None
        if agent is not None:
            internal = agent.runtime_internal
            if internal is not None and internal.prompt_materialization is not None:
                agent_prompt_materialization = internal.prompt_materialization
            else:
                manifest = self._agent_registry.get(agent.preset)
                if manifest is not None:
                    agent_prompt_materialization = manifest.prompt_materialization
        tool_feedback_mode = self._tool_feedback_mode_for_config(config)
        agent_prompt_context = render_agent_prompt(agent_prompt_materialization) or ""
        hook_preset_context = self._hook_preset_context_for_agent(agent=config.agent)
        comp_ref = None
        if session_metadata is not None and ("composition_ref" in session_metadata or "agent_capability_snapshot" in session_metadata):
            try:
                comp_ref = composition_ref_from_session_metadata(session_metadata)
            except Exception:
                comp_ref = None
        context_transform_result = build_provider_context_transform_result(
            workspace=self._workspace,
            tool_results=tool_results,
            hook_preset_context=hook_preset_context,
            failure_policy=config.context_window.context_transform_failure_policy if config.context_window is not None else "warn",
            registry=self._context_transform_registry_for_agent(
                config.agent,
                composition_ref=comp_ref,
            ),
        )
        if compaction_budget is None:
            # The catalog window and the compaction knobs are runtime-owned
            # truth; a caller that does not supply a budget still gets the
            # sized one so pruning (or its explicit "unsized" reason) happens.
            compaction_budget = compaction_budget_from_config(
                config.context_window,
                context_window=self._stream_prep_coordinator.context_budget_for_config(config),
                anchor_tokens=provider_usage_anchor_tokens(session_metadata),
            )
        assembled_context = assemble_provider_context(
            prompt=prompt,
            tool_results=tool_results,
            session_metadata=session_metadata,
            policy=policy or self._default_context_window_policy,
            compaction_budget=compaction_budget,
            before_compact=before_compact,
            agent_prompt_context=agent_prompt_context,
            hook_preset_context=hook_preset_context,
            context_transform_result=context_transform_result,
            skill_prompt_context=skill_prompt_context,
            loaded_skills=loaded_skills,
            preserved_continuity_state=continuity_state_from_session_metadata(session_metadata),
            workspace=self._workspace,
            replay_retained_tool_messages=tool_feedback_mode != "synthetic_user_message",
            replayed_conversation_segments=replayed_conversation_segments,
            tool_catalog_context=tool_catalog_context,
            hook_guidance=hook_guidance if hook_guidance else None,
            reminder_segment=reminder_segment,
            continuity_summary_override=continuity_summary_override,
            continuity_summary_kind=continuity_summary_kind,
        )
        raw_delegation = session_metadata.get("delegation")
        if raw_delegation is None:
            return assembled_context
        delegation = parse_delegation_metadata(raw_delegation)
        return RuntimeAssembledContext(
            prompt=assembled_context.prompt,
            tool_results=assembled_context.tool_results,
            continuity_state=assembled_context.continuity_state,
            segments=assembled_context.segments,
            metadata={**assembled_context.metadata, "delegation": dict(delegation)},
            loaded_skills=assembled_context.loaded_skills,
            context_window=assembled_context.context_window,
        )

    def summarize_continuity(
        self,
        *,
        tool_results: tuple[ReportedCall | ToolResultView, ...],
        session_metadata: dict[str, object],
        source_text: str | None = None,
    ) -> str | None:
        """One-shot, runtime-owned summary of the conversation pruning is discarding.

        Owned spirit: :mod:`voidcode.runtime.context` stays transport-free, so the
        provider call lives here. One provider request through the same
        ``turn_provider()`` seam as normal turns; provider/model/credential
        resolution and the timeout are settled below. Any failure, a blank answer
        or a timeout returns ``None`` -- this never raises into the run, and the
        caller falls back to the deterministic projection.
        """
        prompt = source_text if source_text is not None else _continuity_summary_input(tool_results)
        if not prompt:
            return None
        try:
            config = self.run_config_from_metadata(session_metadata)
            bound_target = config.bound_provider.active_target if config.bound_provider is not None else None
            if bound_target is None:
                return None
            provider = bound_target.provider
            selection = bound_target.selection
            request = ProviderTurnRequest(
                assembled_context=_PlainTextProviderContext(prompt),
                raw_model=selection.raw_model,
                provider_name=selection.provider,
                model_name=selection.model,
                model_metadata=bound_target.metadata,
                session_id=None,
                attempt=0,
            )
            outcome: list[ProviderTurnResult] = []
            failures: list[BaseException] = []

            def _call() -> None:
                # The provider exception must not escape this thread: it would
                # print as an unhandled thread exception while the run carries on
                # with the deterministic projection.
                try:
                    outcome.append(provider.turn_provider().propose_turn(request))
                except BaseException as exc:  # noqa: BLE001 - any failure degrades, never raises.
                    failures.append(exc)

            thread = threading.Thread(target=_call, name="runtime-continuity-summary", daemon=True)
            thread.start()
            thread.join(timeout=_CONTINUITY_SUMMARY_TIMEOUT_SECONDS)
            if thread.is_alive():
                logger.warning("continuity summary provider call timed out after %.1fs", _CONTINUITY_SUMMARY_TIMEOUT_SECONDS)
                return None
            if failures:
                logger.warning("continuity summary provider call failed: %s", failures[0])
                return None
            if not outcome:
                return None
            text = outcome[0].output
        except Exception as exc:  # noqa: BLE001 - the run must survive a summary failure.
            logger.warning("continuity summary setup failed: %s", exc)
            return None
        if not isinstance(text, str):
            return None
        stripped = text.strip()
        return stripped[:_CONTINUITY_SUMMARY_OUTPUT_CHARS] or None

    def reassemble_provider_context_for_overflow(
        self,
        *,
        prompt: str,
        tool_results: tuple[ReportedCall | ToolResultView, ...],
        session_metadata: dict[str, object],
        replayed_conversation_segments: tuple[ContextSegment, ...],
    ) -> RuntimeAssembledContext:
        """Rebuild the provider view for a context-limit retry with a recovery budget.

        The recovery target is harsher than the ordinary trigger: the whole view
        must fit the catalog window minus reserve, so the tool results may only
        keep what the instructions and prompt leave under that threshold. The
        ordinary policy knobs (``context_window.compaction.*``) are reused
        unchanged -- recovery adds a target, not a second configuration.
        """
        config = self.run_config_from_metadata(session_metadata)
        budget = compaction_budget_from_config(
            config.context_window,
            context_window=self._stream_prep_coordinator.context_budget_for_config(config),
            anchor_tokens=provider_usage_anchor_tokens(session_metadata),
        )
        return self.assemble_provider_context(
            prompt=prompt,
            tool_results=tool_results,
            session_metadata=session_metadata,
            replayed_conversation_segments=replayed_conversation_segments,
            compaction_budget=replace(budget, fit_payload=True),
        )

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
            if skill_name not in deduped_force_load_skill_names and skill_registry.is_enabled(skill_name):
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

    def build_skill_snapshot(
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
        if effective_selected_skill_names is not None:
            effective_selected_skill_names = tuple(name for name in effective_selected_skill_names if skill_registry.is_enabled(name))
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
                validate_agent_capability_snapshot(raw_capability_snapshot)
                return self._skill_binding_snapshot_from_agent_capability_snapshot(raw_capability_snapshot)
        if require_capability:
            raise ValueError("persisted session requires agent_capability_snapshot")
        source_runtime_config: dict[str, object] | None = None
        if metadata is not None:
            raw_runtime_config = metadata.get("runtime_config")
            if isinstance(raw_runtime_config, dict):
                source_runtime_config = raw_runtime_config
        if source_runtime_config is None:
            source_runtime_config = self._runtime_config_metadata(self.run_config_from_metadata(metadata))
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
        config: RunConfig,
        tool_materialization: RuntimeToolMaterialization,
        metadata: dict[str, object],
        request_metadata: dict[str, object],
        resolved_hook_presets: ResolvedHookPresetSnapshot,
        composition_ref: Mapping[str, object],
        parent_capability_snapshot: dict[str, object] | None = None,
    ) -> dict[str, object]:
        runtime_config = metadata.get("runtime_config")
        runtime_config_payload: dict[str, object] = runtime_config if isinstance(runtime_config, dict) else {}
        agent = config.agent
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
        internal = agent.runtime_internal if agent is not None else None
        skill_snapshot = {
            "manifest_refs": list(internal.manifest_skill_refs) if internal is not None else [],
            "selected_names": list(selected_skills or ()),
            "force_loaded_names": list(dict.fromkeys(force_load_skills or ()).keys() if force_load_skills is not None else ()),
            "scope": "target_session",
        }
        hook_snapshot = {
            "manifest_refs": list(internal.manifest_hook_refs) if internal is not None else [],
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
            "governance": "runtime_config_gated",
        }
        delegation_snapshot = agent_capability_delegation_snapshot(
            metadata=metadata,
            parent_capability_snapshot=parent_capability_snapshot,
        )
        return {
            "composition_ref": dict(composition_ref),
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
                    "mcp": ("agent MCP binding is declarative intent; runtime-scoped MCP lifecycle and tool allowlists remain runtime-governed"),
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
            "runtime": {
                "approval_mode": config.approval_mode,
                "tool_timeout_seconds": config.tool_timeout_seconds,
                "permission": runtime_config_payload.get("permission"),
            },
            "execution": {
                "model": config.model,
                "fallback_models": (list(config.provider_fallback.fallback_models) if config.provider_fallback is not None else []),
                "resolved_provider": runtime_config_payload.get("resolved_provider"),
                "reasoning_effort": config.reasoning_effort,
            },
        }

    def _session_with_agent_capability_snapshot(
        self,
        *,
        session: SessionState,
        config: RunConfig,
        request_metadata: dict[str, object],
        resolved_hook_presets: ResolvedHookPresetSnapshot,
        tool_materialization: RuntimeToolMaterialization,
        composition_ref: Mapping[str, object],
    ) -> SessionState:
        parent_capability_snapshot = self._parent_capability_snapshot_for_session(session)
        return SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata={
                **session.metadata,
                "agent_capability_snapshot": self._agent_capability_snapshot(
                    config=config,
                    tool_materialization=tool_materialization,
                    metadata=session.metadata,
                    request_metadata=request_metadata,
                    resolved_hook_presets=resolved_hook_presets,
                    composition_ref=composition_ref,
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
        return validate_agent_capability_snapshot(raw_snapshot)

    @staticmethod
    def _snapshot_to_session_metadata(snapshot: SkillExecutionSnapshot) -> dict[str, object]:
        return snapshot_to_session_metadata(snapshot)

    def _build_hook_preset_snapshot(
        self,
        agent: RuntimeAgentConfig | None,
    ) -> ResolvedHookPresetSnapshot:
        refs = hook_preset_refs_for_agent(agent)
        return resolve_hook_preset_refs(refs)

    def _hook_preset_context_for_agent(
        self,
        *,
        agent: RuntimeAgentConfig | None,
    ) -> str:
        return self._build_hook_preset_snapshot(agent).guidance_context()

    def _context_transform_registry_for_agent(
        self,
        agent: RuntimeAgentConfig | None,
        *,
        composition_ref: CompositionRef | None = None,
    ) -> RuntimeContextTransformRegistry:
        refs = agent.context_transform_refs if agent is not None else ()
        registry = (
            self._activated_context_transform_registries.get(composition_ref, self._context_transform_registry)
            if composition_ref is not None
            else self._context_transform_registry
        )
        return registry.filtered(refs)

    def tool_input_handler_registry_for_session(self, session: SessionState) -> ToolInputHandlerRegistry:
        try:
            ref = composition_ref_from_session_metadata(session.metadata)
            return self._activated_tool_input_registries.get(ref, self._tool_input_handler_registry)
        except Exception:
            return self._tool_input_handler_registry

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
    def _fresh_request_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
        return fresh_request_metadata(metadata)

    @staticmethod
    def _persisted_selected_skill_names(
        metadata: dict[str, object],
    ) -> tuple[str, ...] | None:
        return persisted_selected_skill_names(metadata)

    def _runtime_config_metadata(
        self,
        config: RunConfig | None = None,
    ) -> dict[str, object]:
        config = config or self.run_config_from_metadata(None)
        runtime_config_metadata = serialize_runtime_config_core(config)
        runtime_config_metadata["resolved_provider"] = resolved_provider_snapshot(config.resolved_provider)
        resolved_hook_presets = self._build_hook_preset_snapshot(
            config.agent,
        )
        if resolved_hook_presets.presets:
            runtime_config_metadata["resolved_hook_presets"] = resolved_hook_presets.to_payload()
        serialized_agents = serialize_runtime_agents_config(self._config.agents, include_runtime_internal=True)
        if serialized_agents is not None:
            runtime_config_metadata["agents"] = serialized_agents
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
        config: RunConfig,
        raw_agent: object,
        *,
        allow_subagent_presets: bool = False,
    ) -> RunConfig:
        raw_agent_payload = raw_agent if isinstance(raw_agent, dict) else {}
        raw_agent_prompt_profile = raw_agent_payload.get("prompt_profile")
        raw_agent_prompt = raw_agent_payload.get("prompt")
        raw_agent_prompt_append = raw_agent_payload.get("prompt_append")
        agent_payload_for_parse: object = raw_agent
        if (
            raw_agent_prompt is None
            and raw_agent_prompt_append is not None
            and raw_agent_prompt_profile is None
            and config.agent is not None
            and isinstance(config.agent.runtime_internal, RuntimeAgentInternalState)
            and config.agent.preset == raw_agent_payload.get("preset")
        ):
            inherited_materialization = config.agent.runtime_internal.prompt_materialization
            inherited_body = inherited_materialization.body if inherited_materialization is not None else None
            if inherited_body is not None:
                agent_payload_for_parse = {**raw_agent_payload, "prompt": inherited_body}
        agent = parse_runtime_agent_payload(
            agent_payload_for_parse,
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
        model = agent.model if agent.model is not None else config.model
        provider_fallback = agent.provider_fallback if agent.provider_fallback is not None else config.provider_fallback
        request_runtime_internal = agent.runtime_internal
        if (
            request_runtime_internal is not None
            and request_runtime_internal.prompt_materialization is None
            and raw_agent_prompt is None
            and raw_agent_prompt_append is None
            and raw_agent_prompt_profile is None
            and (config.agent is None or agent.preset == config.agent.preset)
        ):
            request_runtime_internal = config.agent.runtime_internal if config.agent is not None else None
        merged_agent = RuntimeAgentConfig(
            preset=agent.preset,
            prompt_profile=(
                agent.prompt_profile if agent.prompt_profile is not None else config.agent.prompt_profile if config.agent is not None else None
            ),
            prompt=(agent.prompt if agent.prompt is not None else config.agent.prompt if config.agent is not None else None),
            prompt_append=(
                agent.prompt_append if agent.prompt_append is not None else config.agent.prompt_append if config.agent is not None else None
            ),
            runtime_internal=request_runtime_internal,
            hook_refs=(agent.hook_refs if agent.hook_refs else config.agent.hook_refs if config.agent is not None else ()),
            context_transform_refs=(
                agent.context_transform_refs
                if agent.context_transform_refs
                else config.agent.context_transform_refs
                if config.agent is not None
                else ()
            ),
            model=model,
            tools=(agent.tools if agent.tools is not None else config.agent.tools if config.agent is not None else None),
            skills=(agent.skills if agent.skills is not None else config.agent.skills if config.agent is not None else None),
            mcp_binding=(agent.mcp_binding if agent.mcp_binding is not None else config.agent.mcp_binding if config.agent is not None else None),
            provider_fallback=provider_fallback,
        )
        resolved_provider = resolve_provider_config(
            model,
            provider_fallback,
            registry=self._model_provider_registry,
        )
        return RunConfig(
            approval_mode=config.approval_mode,
            permission=config.permission,
            model=model,
            tool_timeout_seconds=config.tool_timeout_seconds,
            reasoning_effort=config.reasoning_effort,
            provider_fallback=provider_fallback,
            providers=config.providers,
            resolved_provider=resolved_provider,
            agent=merged_agent,
            context_window=config.context_window,
            tools=config.tools,
            policy=config.policy,
            components=config.components,
        )

    def _session_agent_registry(self, metadata: Mapping[str, object] | None) -> AgentManifestRegistry:
        if metadata is None or "composition_ref" not in metadata:
            return self._agent_registry
        ref = composition_ref_from_session_metadata(metadata)
        frozen = self._repositories.recovery.load_execution_composition(ref=ref)

        recovered_custom: dict[str, AgentManifest] = dict(self._agent_registry.custom)
        for comp in frozen.binding.components:
            if comp.distribution == "voidcode" and comp.slot == "agent" and comp.name == "declarative":
                if not isinstance(comp.configuration, Mapping):
                    raise ValueError(f"declarative agent component '{comp.key}' configuration must be a mapping")
                manifest = AgentManifest.from_payload(comp.configuration)
                if manifest.source_scope not in ("project", "user"):
                    raise ValueError(f"declarative agent component '{comp.key}' requires project or user source_scope; got {manifest.source_scope!r}")
                if manifest.id in self._agent_registry.builtin:
                    raise ValueError(
                        f"declarative agent component '{comp.key}' uses builtin agent id '{manifest.id}'; builtin agent manifests cannot be replaced"
                    )
                recovered_custom[manifest.id] = manifest
        return AgentManifestRegistry(builtin=self._agent_registry.builtin, custom=recovered_custom)

    def _validate_runtime_agent_for_execution(
        self,
        agent: RuntimeAgentConfig,
        *,
        source: str,
        agent_registry: AgentManifestRegistry | None = None,
        allow_subagent_presets: bool = False,
    ) -> None:
        if agent.preset is None:
            return
        registry = agent_registry or self._agent_registry
        executable_primary = registry.executable_primary_ids()
        executable_subagents = registry.executable_subagent_ids()
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
        parent_session_id: str | None = None,  # noqa: ARG002 — retained for delegation route contract compatibility.
    ) -> RuntimeRequestMetadataPayload:
        resolved_route = runtime_subagent_route_from_metadata(
            metadata,
            callable_subagent_presets=self._agent_registry.executable_subagent_ids(),
        )
        if resolved_route is None:
            return metadata

        normalized_metadata: dict[str, object] = dict(metadata)
        raw_delegation_metadata = normalized_metadata["delegation"]
        if not isinstance(raw_delegation_metadata, dict):
            raise RuntimeRequestError("request metadata 'delegation' must be an object when provided")
        delegation_metadata: dict[str, object] = dict(raw_delegation_metadata)
        delegation_metadata["selected_preset"] = resolved_route.selected_preset

        raw_agent = normalized_metadata.get("agent")
        if raw_agent is None:
            delegated_model = self._delegated_model_for_route(
                selected_preset=resolved_route.selected_preset,
                request_agent=None,
            )
            delegated_provider_fallback = self._delegated_provider_fallback_for_route(
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
                    selected_preset=resolved_route.selected_preset,
                    request_agent=agent,
                )
                if delegated_model is not None:
                    agent = replace(agent, model=delegated_model)
            if agent.provider_fallback is None:
                delegated_provider_fallback = self._delegated_provider_fallback_for_route(
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
        validated = validate_runtime_request_metadata(
            normalized_metadata,
            allow_internal_fields=allow_internal_fields,
        )
        return validated

    def _delegated_model_for_route(
        self,
        *,
        selected_preset: str,
        request_agent: RuntimeAgentConfig | None,
    ) -> str | None:
        return delegated_model_for_route_from_configs(
            selected_preset=selected_preset,
            request_agent=request_agent,
            agents=self._config.agents or {},
            base_model=self._config.model,
        )

    def _delegated_provider_fallback_for_route(
        self,
        *,
        selected_preset: str,
        request_agent: RuntimeAgentConfig | None,
        model: str | None,
    ) -> RuntimeProviderFallbackConfig | None:
        if request_agent is not None and request_agent.provider_fallback is not None:
            return request_agent.provider_fallback
        return provider_fallback_for_agent_selection(
            model=model,
            preset_agent=(self._config.agents or {}).get(selected_preset),
            base_provider_fallback=self._config.provider_fallback,
        )

    @classmethod
    def _hook_preset_event_payload_from_session_metadata(
        cls,
        metadata: dict[str, object],
    ) -> dict[str, object] | None:
        _ = cls
        return hook_preset_event_payload_from_session_metadata(metadata)

    @staticmethod
    def _waiting_request_id_from_response(
        response: RuntimeResponse,
        *,
        request_kind: Literal["approval", "question"],
    ) -> str | None:
        return waiting_request_id_from_response(response, request_kind=request_kind)

    def run_config_from_metadata(self, metadata: dict[str, object] | None) -> RunConfig:
        approval_mode: ApprovalMode = self._config.approval_mode
        model = self._config.model
        reasoning_effort = self._config.reasoning_effort
        providers = self._config.providers
        provider_fallback = self._config.provider_fallback
        agent = self._config.agent or RuntimeAgentConfig(preset="default")
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
                serialize_runtime_agent_config(agent, include_runtime_internal=True),
                source="runtime config agent",
                hooks=self._config.hooks,
                agent_registry=self._agent_registry,
                allow_runtime_internal=True,
            )
            assert agent is not None
            self._validate_runtime_agent_for_execution(
                agent,
                source="runtime config agent",
            )
        model_override = agent.model if agent is not None and agent.model is not None else None
        provider_fallback_override = agent.provider_fallback if agent is not None and agent.provider_fallback is not None else None
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
            return RunConfig(
                approval_mode=approval_mode,
                permission=self._config.permission,
                model=model,
                tool_timeout_seconds=self._config.tool_timeout_seconds,
                reasoning_effort=reasoning_effort,
                provider_fallback=provider_fallback,
                providers=providers,
                resolved_provider=resolved_provider,
                agent=agent,
                context_window=context_window,
                tools=self._config.tools,
                policy=self._config.policy,
                reminders=self._config.reminders,
                components=self._config.components,
            )

        persisted_runtime_config = metadata.get("runtime_config")
        if not isinstance(persisted_runtime_config, dict):
            raise ValueError("persisted session metadata must include runtime_config")

        runtime_config: dict[str, object] = persisted_runtime_config
        materialized = parse_persisted_runtime_config(runtime_config)
        approval_mode = materialized.approval_mode
        permission = materialized.permission
        policy = materialized.policy
        model = materialized.model
        tool_timeout_seconds = materialized.tool_timeout_seconds
        reasoning_effort = materialized.reasoning_effort
        providers = materialized.providers
        provider_fallback = materialized.provider_fallback
        tools = materialized.tools
        context_window = materialized.context_window
        session_agent_registry = self._session_agent_registry(metadata)
        if materialized.has_agent:
            agent = parse_runtime_agent_payload(
                materialized.raw_agent,
                source="persisted runtime_config.agent",
                hooks=self._config.hooks,
                agent_registry=session_agent_registry,
                allow_runtime_internal=True,
            )
            if agent is not None:
                self._validate_runtime_agent_for_execution(
                    agent,
                    source="persisted runtime_config.agent",
                    agent_registry=session_agent_registry,
                    allow_subagent_presets=allow_persisted_subagent_presets,
                )
        else:
            agent = None
        activated_binding: tuple[ResolvedProviderConfig, BoundProviderConfig] | None = None
        if metadata is not None and "agent_capability_snapshot" in metadata:
            ref = composition_ref_from_session_metadata(metadata)
            activated_binding = self._activated_provider_bindings.get(ref)
        raw_resolved_provider = materialized.raw_resolved_provider
        bound_provider = None
        if activated_binding is not None:
            resolved_provider, bound_provider = activated_binding
            model = resolved_provider.model
            provider_fallback = resolved_provider.provider_fallback
        elif raw_resolved_provider is not None:
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
        return RunConfig(
            approval_mode=approval_mode,
            permission=permission,
            model=model,
            tool_timeout_seconds=tool_timeout_seconds,
            reasoning_effort=reasoning_effort,
            provider_fallback=provider_fallback,
            providers=providers,
            resolved_provider=resolved_provider,
            bound_provider=bound_provider,
            agent=agent,
            context_window=context_window,
            tools=tools,
            policy=policy,
            # A snapshot written before this key existed has no reminder policy;
            # the live resolved config is the only remaining source.
            reminders=materialized.reminders if materialized.reminders is not None else self._config.reminders,
            components=self._config.components,
        )

    def turn_producer_for_session_metadata(self, metadata: dict[str, object] | None) -> TurnProducer:
        if self._turn_producer_override is not None:
            return self._turn_producer_override
        config = self.run_config_from_metadata(metadata)
        return select_turn_producer_for_config(config=config).producer

    def _load_existing_session_if_present(self, *, session_id: str) -> RuntimeResponse | None:
        return self._finalize_coordinator.load_existing_session_if_present(session_id=session_id)

    def _load_stored_response(self, *, session_id: str) -> RuntimeResponse:
        return self._finalize_coordinator.load_stored_response(session_id=session_id)

    def _load_replay_response(self, *, session_id: str) -> RuntimeResponse:
        return self._finalize_coordinator.load_replay_response(session_id=session_id)

    def _sealed_session_status(self, *, session_id: str) -> SessionStatus | None:
        return self._finalize_coordinator.sealed_session_status(session_id=session_id)

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

    def interrupt_active_run(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        reason: str | None = None,
    ) -> ActiveRunInterruptResult:
        validate_id(session_id)
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


@dataclass(frozen=True, slots=True)
class _PreparedStreamSession:
    request: RuntimeRequest
    resolved_session_id: str
    config: RunConfig
    composition_ref: CompositionRef
    frozen_composition: FrozenComposition
    request_metadata: dict[str, object]
    existing_session: RuntimeResponse | None
    rehydrated_conversation_segments: tuple[ContextSegment, ...]
    rehydrated_tool_results: tuple[ReportedCall, ...]
    resolved_hook_presets: ResolvedHookPresetSnapshot
    resolved_hook_plan: ResolvedHookPlan
    provider_prompt: str | None = None
