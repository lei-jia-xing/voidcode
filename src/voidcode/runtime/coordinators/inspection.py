"""Inspection-bucket coordinator: read-mostly runtime projections.

Owns what ``VoidCodeRuntime`` only projected: capability state (LSP/MCP/ACP),
session replay/revert/artifacts, debug snapshots, bundles, storage pruning,
provider/agent/skill/command listings, status, review, and web settings reads.

Reads via constructor-injected collaborators plus a narrow ``RuntimeSurface``
for stream-prep-owned composition (effective config, context assembly, tool
registries). Cross-bucket entry points that stay owned elsewhere
(``load_background_task``, ``_refresh_mcp_tools``, active-run registry) arrive
as explicit callbacks so this module never pierces runtime privates and adds
no new storage write paths: every store mutation below delegates to an
existing ``SessionStore`` method.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from ...agent import AgentManifestRegistry
from ...command import load_command_registry
from ...command.models import CommandDefinition
from ...mcp.redaction import redact_mcp_command
from ...provider.auth import ProviderAuthResolver
from ...provider.model_catalog import ToolFeedbackMode
from ...provider.models import ResolvedProviderConfig, ResolvedProviderModel
from ...provider.naming import provider_label
from ...provider.protocol import ProviderAssembledContext
from ...provider.registry import ModelProviderRegistry
from ...provider.resolution import resolve_provider_config
from ...provider.snapshot import resolved_provider_snapshot
from ...tools.contracts import ToolResult
from ...tools.output import (
    read_tool_output_artifact,
    search_tool_output_artifact,
)
from ...tools.output import (
    resolve_tool_output_artifact as resolve_tool_output_artifact_metadata,
)
from ..acp import (
    AcpAdapter,
    AcpAdapterState,
)
from ..agent_capability import validate_agent_capability_snapshot
from ..background.routing import provider_fallback_for_agent_selection
from ..bundle import (
    SessionBundle,
    SessionBundleImportResult,
    SessionBundleOptions,
    apply_session_bundle,
    build_session_bundle,
    read_session_bundle,
)
from ..config import (
    RuntimeAgentConfig,
    RuntimeConfig,
    RuntimeContextWindowConfig,
    RuntimeProviderFallbackConfig,
    load_global_web_settings,
    parse_runtime_agents_payload,
    serialize_runtime_agent_config,
)
from ..config_materializer import (
    EffectiveRuntimeConfig,
    parse_persisted_runtime_config,
)
from ..context.provider import inspect_provider_context
from ..context.window import RuntimeAssembledContext
from ..contracts import (
    AgentSummary,
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
    RuntimeProviderContextPolicyDecision,
    RuntimeProviderContextSnapshot,
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
    SessionEventBatch,
    SkillSummary,
    UnknownSessionError,
    WorkspaceReviewSnapshot,
    validate_id,
    validate_session_title,
)
from ..effectiveness import ToolEffectivenessReport
from ..event_envelopes import (
    envelopes_for_acp_events,
    envelopes_for_lsp_events,
    envelopes_for_mcp_events,
)
from ..events import (
    RUNTIME_QUESTION_ANSWERED,
    EventEnvelope,
    runtime_policy_observability_payload,
)
from ..execution.provider_execution_metadata import run_id_from_session_metadata
from ..hook_preset_metadata import debug_hook_preset_snapshot
from ..lsp import LspManager, LspManagerState, LspRequest, LspRequestResult
from ..mcp import McpManager
from ..permission import PendingApproval
from ..provider_catalog_cache import RuntimeProviderCatalogCache
from ..provider_catalog_query import RuntimeProviderCatalogQuery
from ..provider_inspection import (
    ProviderReadinessFacts,
    ProviderSummaryProjector,
    ProviderValidationFacts,
    RuntimeProviderAuthInspector,
    RuntimeProviderReadinessProjector,
    RuntimeProviderValidationProjector,
)
from ..provider_metadata import (
    ReasoningEffortCapability,
    resolve_reasoning_effort_capability,
)
from ..question import PendingQuestion
from ..review import WorkspaceReviewService
from ..runtime_debug import (
    current_debug_status,
    debug_event,
    debug_failure,
    last_tool_summary,
    operator_guidance,
    prompt_and_tool_results_from_debug_events,
)
from ..session import (
    SessionEntrySummary,
    SessionRef,
    SessionState,
    StoredSessionForestEntry,
    StoredSessionLineageEntry,
    StoredSessionSummary,
    normalize_persisted_session_metadata,
    session_metadata_for_replay,
    validate_session_workspace,
)
from ..skill_metadata import fresh_request_metadata, skill_snapshot_from_metadata
from ..skills import SkillRegistry
from ..status_projection import project_acp_status
from ..storage import SessionStore

if TYPE_CHECKING:
    from ...graph.contracts import GraphRunRequest
    from ..background.supervisor import RuntimeBackgroundTaskSupervisor
    from ..runtime_surface import RuntimeSurface

logger = logging.getLogger(__name__)

_POLICY_PROJECTED_EVENT_TYPES = frozenset({"runtime.request_received"})


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


def _decode_subprocess_text_output(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return value.decode(errors="replace")
    return ""


def _acp_status_snapshot(acp_state: AcpAdapterState) -> CapabilityStatusSnapshot:
    return project_acp_status(acp_state)


def _debug_event(event: EventEnvelope | None) -> RuntimeSessionDebugEvent | None:
    return debug_event(event)


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


def _last_tool_summary(result: RuntimeSessionResult) -> RuntimeSessionDebugToolSummary | None:
    return last_tool_summary(result)


def _prompt_and_tool_results_from_debug_events(
    events: tuple[EventEnvelope, ...],
) -> tuple[str, list[ToolResult]]:
    return prompt_and_tool_results_from_debug_events(events)


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
    return fresh_request_metadata(request_metadata)


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
    active_request_metadata = fresh_request_metadata(request_metadata)
    persisted_request_metadata = _request_metadata_from_session_metadata(result.session.metadata)
    if active_request_metadata != persisted_request_metadata:
        return True
    active_prompt = active_metadata.get("prompt")
    return isinstance(active_prompt, str) and active_prompt != result.prompt


def _tool_feedback_mode_for_effective_config(
    effective_config: EffectiveRuntimeConfig,
) -> ToolFeedbackMode:
    active_target = effective_config.resolved_provider.active_target
    if active_target.metadata is not None and active_target.metadata.tool_feedback_mode is not None:
        return active_target.metadata.tool_feedback_mode
    return "standard"


class InspectionCoordinator:
    """Read-mostly runtime projections; see module docstring."""

    def __init__(
        self,
        surface: RuntimeSurface,
        *,
        session_store: SessionStore,
        workspace: Path,
        config: RuntimeConfig,
        lsp_manager: LspManager,
        mcp_manager: McpManager,
        acp_adapter: AcpAdapter,
        model_provider_registry: ModelProviderRegistry,
        provider_catalog_cache: RuntimeProviderCatalogCache,
        provider_catalog_query: RuntimeProviderCatalogQuery,
        provider_summary_projector: ProviderSummaryProjector,
        provider_auth_inspector: RuntimeProviderAuthInspector,
        provider_auth_resolver: ProviderAuthResolver,
        skill_registry: SkillRegistry,
        agent_registry: AgentManifestRegistry,
        background_task_supervisor: RuntimeBackgroundTaskSupervisor,
        resolved_provider_config: ResolvedProviderConfig,
        provider_model: ResolvedProviderModel,
        load_background_task: Callable[[str], object],
        refresh_mcp_tools: Callable[[], None],
        is_active_session: Callable[[str], bool],
        active_session_metadata: Callable[[str], dict[str, object] | None],
    ) -> None:
        self._surface = surface
        self._session_store = session_store
        self._workspace = workspace
        self._config = config
        self._lsp_manager = lsp_manager
        self._mcp_manager = mcp_manager
        self._acp_adapter = acp_adapter
        self._model_provider_registry = model_provider_registry
        self._provider_catalog_cache = provider_catalog_cache
        self._provider_catalog_query = provider_catalog_query
        self._provider_summary_projector = provider_summary_projector
        self._provider_auth_inspector = provider_auth_inspector
        self._provider_auth_resolver = provider_auth_resolver
        self._skill_registry = skill_registry
        self._agent_registry = agent_registry
        self._background_task_supervisor = background_task_supervisor
        self._resolved_provider_config = resolved_provider_config
        self._provider_model = provider_model
        self._load_background_task_fn = load_background_task
        self._refresh_mcp_tools_fn = refresh_mcp_tools
        self._is_active_session_fn = is_active_session
        self._active_session_metadata_fn = active_session_metadata

    def update_provider_state(
        self,
        *,
        config: RuntimeConfig,
        model_provider_registry: ModelProviderRegistry,
        provider_catalog_cache: RuntimeProviderCatalogCache,
        provider_catalog_query: RuntimeProviderCatalogQuery,
        provider_auth_inspector: RuntimeProviderAuthInspector,
        provider_auth_resolver: ProviderAuthResolver,
        resolved_provider_config: ResolvedProviderConfig,
        provider_model: ResolvedProviderModel,
    ) -> None:
        self._config = config
        self._model_provider_registry = model_provider_registry
        self._provider_catalog_cache = provider_catalog_cache
        self._provider_catalog_query = provider_catalog_query
        self._provider_auth_inspector = provider_auth_inspector
        self._provider_auth_resolver = provider_auth_resolver
        self._resolved_provider_config = resolved_provider_config
        self._provider_model = provider_model

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
        return self._mcp_manager.call_tool(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            workspace=workspace,
        )

    def shutdown_mcp(self) -> tuple[EventEnvelope, ...]:
        return envelopes_for_mcp_events(
            session_id="runtime",
            start_sequence=1,
            mcp_events=self._mcp_manager.shutdown(),
        )

    def shutdown_lsp(self) -> tuple[EventEnvelope, ...]:
        return envelopes_for_lsp_events(
            session_id="runtime",
            start_sequence=1,
            lsp_events=self._lsp_manager.shutdown(),
        )

    def current_acp_state(self):
        return self._acp_adapter.current_state()

    def disconnect_acp(self) -> tuple[EventEnvelope, ...]:
        return envelopes_for_acp_events(
            session_id="runtime",
            start_sequence=1,
            acp_events=self._acp_adapter.disconnect(),
        )

    def list_sessions(self) -> tuple[StoredSessionSummary, ...]:
        return self._session_store.list_sessions(workspace=self._workspace)

    def rename_session(self, *, session_id: str, title: str) -> StoredSessionSummary:
        """Set the user-settable title and return the updated summary.

        Title normalization/bounding lives in ``validate_session_title`` so the
        CLI, HTTP, and TUI surfaces all reject the same inputs; existence and
        workspace scoping come from the storage write, whose
        ``UnknownSessionError`` is the same signal ``revert_session`` uses.
        """
        validate_id(session_id)
        validated_title = validate_session_title(title)
        self._session_store.rename_session(
            workspace=self._workspace,
            session_id=session_id,
            title=validated_title,
        )
        for summary in self._session_store.list_sessions(workspace=self._workspace):
            if summary.session.id == session_id:
                return summary
        raise UnknownSessionError(f"unknown session: {session_id}")

    def fork_session(
        self,
        *,
        session_id: str,
        at_sequence: int | None = None,
    ) -> StoredSessionSummary:
        """Copy a session's event-log prefix into a new, independently continuable session.

        ``session_id`` is validated at the same boundary as every sibling
        session method. Storage owns the copy semantics (contiguous sequences,
        the fork's own watermark, provenance columns, an untouched source) and
        raises ``RuntimeSessionForkBoundaryError`` when the requested boundary
        splits a tool call from its result.
        """
        validate_id(session_id)
        if at_sequence is not None and at_sequence < 1:
            raise ValueError("fork sequence must be a positive integer")
        return self._session_store.fork_session(
            workspace=self._workspace,
            session_id=session_id,
            at_sequence=at_sequence,
        )

    def session_lineage(self, *, session_id: str | None = None) -> tuple[StoredSessionLineageEntry, ...]:
        """Read-only fork ancestry: oldest ancestor first, the named session last.

        Without ``session_id`` the whole-workspace provenance read excludes
        delegated background-task children (``parent_session_id`` set), which are
        not fork nodes; the named-session walk is unaffected.
        """
        if session_id is not None:
            validate_id(session_id)
        return self._session_store.session_lineage(
            workspace=self._workspace,
            session_id=session_id,
        )

    def session_forest(self) -> tuple[StoredSessionForestEntry, ...]:
        """Read-only workspace fork forest, parents before children."""
        return self._session_store.session_forest(workspace=self._workspace)

    def session_entries(self, *, session_id: str) -> tuple[SessionEntrySummary, ...]:
        """Read-only entry listing for one session, ascending ``sequence``.

        ``session_id`` is validated at the same boundary as every sibling
        session method; the store owns the path walk and the on-path marking.
        """
        validate_id(session_id)
        return self._session_store.session_entries(
            workspace=self._workspace,
            session_id=session_id,
        )

    def checkout_session(self, *, session_id: str, sequence: int) -> int:
        """Move a session's leaf to ``sequence``; returns the new leaf.

        Storage owns the position change (validate the target, refuse a path
        that splits an interaction, drop the position-scoped cached state, and
        leave the row continuable). This boundary validates identity and the
        sequence the way ``fork_session`` validates its boundary.
        """
        validate_id(session_id)
        if sequence < 1:
            raise ValueError("checkout sequence must be a positive integer")
        return self._session_store.checkout_session(
            workspace=self._workspace,
            session_id=session_id,
            sequence=sequence,
        )

    def tool_effectiveness_report(self) -> ToolEffectivenessReport:
        return self._session_store.tool_effectiveness_report(workspace=self._workspace)

    def _load_stored_response(self, *, session_id: str) -> RuntimeResponse:
        response = self._session_store.load_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        validate_session_workspace(response.session, session_id=session_id, workspace=self._workspace)
        return response

    def _load_existing_session_if_present(self, *, session_id: str) -> RuntimeResponse | None:
        if not self._session_store.has_session(workspace=self._workspace, session_id=session_id):
            return None
        return self._load_stored_response(session_id=session_id)

    def _load_session_result(self, *, session_id: str) -> RuntimeSessionResult:
        validate_id(session_id)
        result = self._session_store.load_session_result(
            workspace=self._workspace,
            session_id=session_id,
        )
        validate_session_workspace(result.session, session_id=session_id, workspace=self._workspace)
        raw_snapshot = result.session.metadata.get("agent_capability_snapshot")
        if raw_snapshot is None:
            raise ValueError("persisted session requires agent_capability_snapshot")
        if not isinstance(raw_snapshot, dict):
            raise ValueError("persisted agent_capability_snapshot must be an object")
        validate_agent_capability_snapshot(raw_snapshot)
        return result

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
            if event.event_type not in _POLICY_PROJECTED_EVENT_TYPES:
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
                        "runtime_policy": runtime_policy_observability_payload(raw_policy),
                    },
                )
            )
        return tuple(projected)

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
        validate_id(session_id)
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

    def session_events_after(self, *, session_id: str, after_sequence: int) -> SessionEventBatch:
        """Read only the persisted events after ``after_sequence`` plus the row status."""
        validate_id(session_id)
        stored = self._session_store.read_session_events_after(
            workspace=self._workspace,
            session_id=session_id,
            after_sequence=after_sequence,
        )
        events = stored.events
        if any(event.event_type in _POLICY_PROJECTED_EVENT_TYPES for event in events):
            events = self._events_with_runtime_policy_projection(
                events,
                metadata=session_metadata_for_replay(normalize_persisted_session_metadata(stored.metadata)),
            )
        return SessionEventBatch(status=stored.status, events=events)

    def revert_session(self, *, session_id: str, sequence: int) -> RuntimeSessionRevertMarker:
        validate_id(session_id)
        marker = self._session_store.revert_session(
            workspace=self._workspace,
            session_id=session_id,
            sequence=sequence,
        )
        validate_session_workspace(
            self._session_store.load_session_result(
                workspace=self._workspace,
                session_id=session_id,
            ).session,
            session_id=session_id,
            workspace=self._workspace,
        )
        return marker

    def undo_session(self, *, session_id: str) -> RuntimeSessionRevertMarker:
        validate_id(session_id)
        marker = self._session_store.undo_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        validate_session_workspace(
            self._session_store.load_session_result(
                workspace=self._workspace,
                session_id=session_id,
            ).session,
            session_id=session_id,
            workspace=self._workspace,
        )
        return marker

    def unrevert_session(self, *, session_id: str) -> RuntimeSessionRevertMarker | None:
        validate_id(session_id)
        marker = self._session_store.unrevert_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        validate_session_workspace(
            self._session_store.load_session_result(
                workspace=self._workspace,
                session_id=session_id,
            ).session,
            session_id=session_id,
            workspace=self._workspace,
        )
        return marker

    def resolve_tool_output_artifact(
        self,
        *,
        session_id: str,
        artifact_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, object]:
        """Resolve spilled tool output artifact metadata for a session."""
        validate_id(session_id)
        result = self._session_store.load_session_result(
            workspace=self._workspace,
            session_id=session_id,
        )
        validate_session_workspace(result.session, session_id=session_id, workspace=self._workspace)
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
        return {
            **artifact,
            "status": read_result["status"],
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

    def storage_diagnostics(self) -> dict[str, object]:
        return self._session_store.storage_diagnostics(workspace=self._workspace)

    def export_session_bundle(
        self,
        *,
        session_id: str,
        options: SessionBundleOptions | None = None,
    ) -> SessionBundle:
        validate_id(session_id)
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

    def session_debug_snapshot(self, *, session_id: str) -> RuntimeSessionDebugSnapshot:
        validate_id(session_id)
        active = self._is_active_session_id(session_id)
        active_metadata = self._active_session_metadata(session_id) if active else None
        try:
            result = self._load_session_result(session_id=session_id)
        except AttributeError, UnknownSessionError, ValueError:
            if not active:
                raise
            return self._active_only_session_debug_snapshot(session_id=session_id)
        if _should_prefer_active_debug_snapshot(
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
        current_status = _current_debug_status(
            result=result,
            active=active,
            pending_approval=pending_approval,
            pending_question=pending_question,
        )
        raw_checkpoint_kind = resume_checkpoint.get("kind") if isinstance(resume_checkpoint, dict) else None
        checkpoint_kind = raw_checkpoint_kind if isinstance(raw_checkpoint_kind, str) else None
        terminal = result.session.status in {"completed", "failed"}
        resumable = (
            result.session.status == "waiting"
            or result.session.status == "interrupted"
            or (result.session.status == "failed" and checkpoint_kind == "provider_failure_retryable")
        )
        replayable = bool(result.transcript) or result.output is not None or terminal
        last_relevant_event = _debug_event(
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
        last_failure_event = _debug_event(
            next(
                (event for event in reversed(result.transcript) if event.event_type == "runtime.failed"),
                None,
            )
        )
        last_tool = _last_tool_summary(result)
        provider_context = self._provider_context_debug_snapshot(result)
        failure = _debug_failure(
            result=result,
            last_failure_event=last_failure_event,
            last_tool=last_tool,
            pending_approval=pending_approval,
            pending_question=pending_question,
            resume_checkpoint=resume_checkpoint,
            persistence_error=persistence_error,
        )
        suggested_operator_action, operator_guidance_text = _operator_guidance(
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
            hook_presets=debug_hook_preset_snapshot(result.session.metadata),
            suggested_operator_action=suggested_operator_action,
            operator_guidance=operator_guidance_text,
        )

    def _is_active_session_id(self, session_id: str) -> bool:
        return self._is_active_session_fn(session_id)

    def _active_session_metadata(self, session_id: str) -> dict[str, object] | None:
        return self._active_session_metadata_fn(session_id)

    def _debug_skill_prompt_context(self, metadata: dict[str, object]) -> str:
        snapshot = skill_snapshot_from_metadata(metadata)
        if snapshot is None:
            return ""
        return snapshot.skill_prompt_context

    def _provider_context_debug_snapshot(
        self,
        result: RuntimeSessionResult,
    ) -> RuntimeProviderContextSnapshot:
        prompt, tool_results = _prompt_and_tool_results_from_debug_events(result.transcript)
        if not prompt:
            prompt = result.prompt
        assembled_context = self._surface.assemble_provider_context(
            prompt=prompt,
            tool_results=tuple(tool_results),
            session_metadata=result.session.metadata,
            skill_prompt_context=self._debug_skill_prompt_context(result.session.metadata),
        )
        context_window_metadata = result.session.metadata.get("context_window")
        if isinstance(context_window_metadata, dict):
            preserved_transform_metadata = context_window_metadata.get("context_transforms")
            if isinstance(preserved_transform_metadata, dict):
                assembled_context = RuntimeAssembledContext(
                    prompt=assembled_context.prompt,
                    tool_results=assembled_context.tool_results,
                    continuity_state=assembled_context.continuity_state,
                    segments=assembled_context.segments,
                    metadata={
                        **assembled_context.metadata,
                        "context_transforms": dict(preserved_transform_metadata),
                    },
                    loaded_skills=assembled_context.loaded_skills,
                )
        effective_config = self._surface.effective_runtime_config_from_metadata(result.session.metadata)
        return self._provider_context_snapshot_for_assembled_context(
            assembled_context=assembled_context,
            effective_config=effective_config,
        )

    def _provider_context_snapshot_for_assembled_context(
        self,
        *,
        assembled_context: ProviderAssembledContext,
        effective_config: EffectiveRuntimeConfig,
    ) -> RuntimeProviderContextSnapshot:
        active_target = effective_config.resolved_provider.active_target
        provider = active_target.selection.provider or "unresolved"
        model = active_target.selection.model or active_target.selection.raw_model or "unresolved"
        tool_registry = self._surface.tool_registry_for_effective_config(effective_config)
        context_window_config = effective_config.context_window or self._surface_context_window_default()
        return inspect_provider_context(
            assembled_context=assembled_context,
            provider=provider,
            model=model,
            execution_engine=effective_config.execution_engine,
            available_tool_count=len(self._surface.provider_tool_definitions(tool_registry, effective_config)),
            tool_feedback_mode=_tool_feedback_mode_for_effective_config(effective_config),
            oversized_tool_feedback_chars=(context_window_config.provider_context_oversized_feedback_chars),
            diagnostic_policy_mode=context_window_config.provider_context_diagnostics,
        )

    def _surface_context_window_default(self) -> RuntimeContextWindowConfig:
        return RuntimeContextWindowConfig()

    def _active_only_session_debug_snapshot(
        self,
        *,
        session_id: str,
    ) -> RuntimeSessionDebugSnapshot:
        active_metadata = self._active_session_metadata(session_id) or {}
        request_metadata = active_metadata.get("request_metadata")
        session_metadata = {
            **(dict(request_metadata) if isinstance(request_metadata, dict) else {}),
            "workspace": str(self._workspace),
        }
        raw_prompt = active_metadata.get("prompt")
        prompt = raw_prompt if isinstance(raw_prompt, str) else ""
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

    def effective_runtime_config(self, *, session_id: str | None = None) -> EffectiveRuntimeConfig:
        if session_id is None:
            return self._surface.effective_runtime_config_from_metadata(None)
        validate_id(session_id)
        response = self._load_stored_response(session_id=session_id)
        return self._surface.effective_runtime_config_from_metadata(response.session.metadata)

    def effective_agent_model_config(self, *, session_id: str | None = None) -> dict[str, object]:
        agents, base_model, base_provider_fallback = self._display_routing_config(session_id=session_id)
        payload: dict[str, object] = {}
        for manifest in self._agent_registry.list_manifests():
            preset_agent = agents.get(manifest.id)
            model = preset_agent.model if preset_agent is not None else manifest.model_preference
            if model is None:
                model = base_model
            provider_fallback = provider_fallback_for_agent_selection(
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
        Mapping[str, RuntimeAgentConfig],
        str | None,
        RuntimeProviderFallbackConfig | None,
    ]:
        if session_id is None:
            return (
                self._config.agents or {},
                self._config.model,
                self._config.provider_fallback,
            )
        validate_id(session_id)
        response = self._load_stored_response(session_id=session_id)
        runtime_config = response.session.metadata.get("runtime_config")
        if not isinstance(runtime_config, dict):
            raise ValueError("persisted session metadata must include runtime_config object")
        payload: dict[str, object] = runtime_config
        materialized = parse_persisted_runtime_config(payload)
        base_model = materialized.model
        base_provider_fallback = materialized.provider_fallback
        agents = parse_runtime_agents_payload(
            payload.get("agents"),
            source="persisted runtime_config.agents",
            hooks=self._config.hooks,
            agent_registry=self._agent_registry,
            allow_runtime_internal=True,
        )
        return agents or {}, base_model, base_provider_fallback

    def _canonical_known_provider_name(self, provider_name: str) -> str:
        """Canonical id for a provider this runtime knows, or a loud error."""
        if not provider_name or "/" in provider_name:
            raise ValueError("provider_name must be a non-empty provider id without '/'")
        return self._model_provider_registry.resolve_with_metadata(provider_name).provider_name

    def refresh_provider_models(self, provider_name: str) -> tuple[str, ...]:
        canonical_name = self._canonical_known_provider_name(provider_name)
        models = self._model_provider_registry.refresh_available_models(canonical_name)
        self._persist_provider_model_catalog_cache()
        return models

    def provider_model_catalog(self, provider_name: str) -> dict[str, object] | None:
        return self._provider_catalog_query.catalog_payload(provider_name)

    def _hydrate_provider_model_catalog_cache(self) -> None:
        self._provider_catalog_cache.hydrate()

    def _persist_provider_model_catalog_cache(self) -> None:
        self._provider_catalog_cache.persist()

    def _metadata_for_provider_model(self, provider_name: str, model_name: str) -> ProviderModelMetadata | None:
        return self._provider_catalog_query.metadata_for_model(provider_name, model_name)

    def reasoning_effort_capability(self, config: EffectiveRuntimeConfig) -> ReasoningEffortCapability:
        """Resolve the reasoning-effort capability of a config's active provider/model target."""
        selection = config.resolved_provider.active_target.selection
        provider_name = selection.provider
        model_name = selection.model
        model_metadata = (
            self._metadata_for_provider_model(provider_name, model_name) if provider_name is not None and model_name is not None else None
        )
        return resolve_reasoning_effort_capability(
            provider_name=provider_name,
            model_name=model_name,
            model_metadata=model_metadata,
        )

    def list_provider_summaries(self) -> tuple[ProviderSummary, ...]:
        return self._provider_summary_projector.project_all(
            self._model_provider_registry.providers,
            current_provider=self._current_provider_name(),
            label_for=provider_label,
            is_configured=self._provider_is_configured,
        )

    def provider_models_result(self, provider_name: str) -> ProviderModelsResult:
        canonical_name = self._canonical_known_provider_name(provider_name)
        configured = self._provider_is_configured(canonical_name)
        catalog = self.provider_model_catalog(canonical_name)
        if configured and catalog is None:
            _ = self.refresh_provider_models(canonical_name)
        return self._provider_catalog_query.models_result(
            canonical_name,
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
        """Report what the runtime will do with the configured reasoning-effort hint."""
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
        capability = resolve_reasoning_effort_capability(
            provider_name=provider_name,
            model_name=model_name,
            model_metadata=self._metadata_for_provider_model(provider_name, model_name),
        )
        payload["supports_reasoning_effort"] = capability.supported
        payload["capability_source"] = capability.source
        if effort is None:
            return payload
        if capability.supported is False:
            payload["status"] = "unsupported"
            payload["reason"] = "model_metadata_disallows_reasoning_effort"
            return payload
        if capability.supported is None:
            payload["status"] = "forwarded_unverified"
            payload["reason"] = "model_capability_unknown"
        else:
            payload["status"] = "forwarded"
        payload["forwarded"] = True
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
        canonical_name = self._canonical_known_provider_name(provider_name)
        summary = next(
            (provider for provider in self.list_provider_summaries() if provider.name == canonical_name),
            self._provider_summary_projector.project_one(
                canonical_name,
                current_provider=self._current_provider_name(),
                label_for=provider_label,
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
        canonical_name = self._canonical_known_provider_name(provider_name)
        provider_name = canonical_name
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
                provider_fallback_for_agent_selection(
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
            summaries.append(_command_summary(command))
        return tuple(summaries)

    def status_snapshot(self) -> RuntimeStatusSnapshot:
        """Project capability/background/git state without reconciling workers.

        The background-write reconcile (`reconcile_...`/`drain_...`) stays in
        the service facade; this is the pure projection half of `current_status`.
        """
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
            command = list(runtime_state.command) if runtime_state is not None and runtime_state.command else list(server_config.command)
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
                    "transport": server_config.transport,
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
            acp=_acp_status_snapshot(acp_state),
            background_tasks=RuntimeBackgroundTaskStatusSnapshot(
                active_worker_slots=self._background_task_supervisor.active_worker_slots(),
                queued_count=background_status_counts.get("queued", 0),
                running_count=background_status_counts.get("running", 0),
                terminal_count=sum(background_status_counts.get(status, 0) for status in self._terminal_task_statuses()),
                default_concurrency=self._config.background_task.default_concurrency,
                provider_concurrency=dict(self._config.background_task.provider_concurrency),
                model_concurrency=dict(self._config.background_task.model_concurrency),
                status_counts=background_status_counts,
            ),
        )

    def _terminal_task_statuses(self) -> tuple[str, ...]:
        from ..background.models import BACKGROUND_TASK_TERMINAL_STATUSES

        return tuple(BACKGROUND_TASK_TERMINAL_STATUSES)

    def retry_mcp_connections(self) -> RuntimeStatusSnapshot:
        self._mcp_manager.retry_connections(workspace=self._workspace)
        try:
            self._refresh_mcp_tools_fn()
        except Exception:
            logger.debug("failed to refresh MCP tools after retry", exc_info=True)
        self._background_task_supervisor.reconcile_background_tasks_if_needed()
        self._background_task_supervisor.drain_queued_background_tasks()
        return self.status_snapshot()

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
        stdout = _decode_subprocess_text_output(result.stdout)
        stderr = _decode_subprocess_text_output(result.stderr)
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

    def _current_provider_name(self) -> str | None:
        active_target = self._resolved_provider_config.active_target
        selection = active_target.selection
        return selection.provider

    def _provider_is_configured(self, provider_name: str) -> bool:
        return self._provider_auth_inspector.is_configured(provider_name)

    def web_settings(self) -> dict[str, object]:
        settings = load_global_web_settings()
        effective_config = self._surface.effective_runtime_config_from_metadata(None)
        return {
            "provider": settings.provider,
            "provider_api_key_present": settings.provider_api_key_present,
            "model": effective_config.model,
        }

    def provider_context_policy_decision_for_graph_request(
        self,
        *,
        graph_request: GraphRunRequest,
        effective_config: EffectiveRuntimeConfig,
    ) -> RuntimeProviderContextPolicyDecision | None:
        if effective_config.execution_engine != "provider":
            return None
        context_window_config = effective_config.context_window or self._surface_context_window_default()
        if context_window_config.provider_context_diagnostics == "off" and context_window_config.context_transform_failure_policy != "block":
            return None
        snapshot = self._provider_context_snapshot_for_assembled_context(
            assembled_context=graph_request.assembled_context,
            effective_config=effective_config,
        )
        return snapshot.policy_decision
