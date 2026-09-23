"""Response models for the runtime HTTP transport.

Every JSON body the transport emits is described here, and the route table wires
these models onto FastAPI routes so the OpenAPI document at
``/api/openapi.json`` carries a real response schema per route instead of an
empty one. The models are the authoritative, machine-readable description of the
client-facing response surface: a future TypeScript type module is generated
from this document, so nothing may be hand-maintained twice.

Two properties of the transport shape the design:

* **The handlers own their rendering.** Every endpoint returns a starlette
  ``Response`` (the byte-stable :class:`~voidcode.runtime.transport.http_contract.JsonResponse`),
  so FastAPI never filters a payload through ``response_model`` and never
  re-serializes it. Setting ``response_model=`` is therefore documentation-only
  at runtime, and the wire keeps its sorted keys, explicit charset, and its
  deliberate ``null`` vs. absent choices. What pins the models to reality is
  ``tests/integration/test_http_response_schema.py``, which drives every route
  against a fixture runtime and asserts each emitted body validates against the
  declared model **and** round-trips through it unchanged.
* **``null`` and "absent" are different wire outcomes.** Some serializers omit a
  key entirely when a value is unset (``SessionRef.parent_id``,
  ``EventEnvelope``'s ``delegated_lifecycle``, agent/skill source fields, the
  debug snapshot's ``runtime_policy``, every provider-model capability), while
  others emit an explicit ``null`` (``BackgroundTaskState.child_session_id``,
  the error envelope's ``code``, ...). A model is only ever built by validating
  the body the transport wrote, so dumping it with ``exclude_unset=True``
  reproduces that distinction for generated clients (``field?: T`` vs
  ``field: T | null``).

Deliberately partially typed payloads (free-form, runtime-owned maps) are marked
``dynamic`` with the reason: event payloads, session/task metadata blobs,
persisted runtime-policy projections, provider reasoning controls, tool
arguments/artifacts, and hook reminders. They stay ``dict[str, object]`` rather
than ``object`` so generated clients at least see a JSON object.
"""

from __future__ import annotations

from typing import Literal, Self, final

from pydantic import BaseModel, ConfigDict, model_validator

from ..background.models import BackgroundTaskStatus
from ..background.routing import SubagentExecutionMode
from ..contracts import (
    CapabilityState,
    GitStatusState,
    ReviewFileDiffState,
    ReviewTreeNodeKind,
    RuntimeProviderContextDiagnosticPolicyAction,
    RuntimeProviderContextDiagnosticPolicyMode,
)
from ..events import DelegatedLifecycleStatus, EventSource
from ..session import SessionStatus


class ResponseModel(BaseModel):
    """Base for every transport response model.

    ``extra="forbid"`` is the drift guard: the transport emits exactly these
    keys, so an unexpected key in a real body is a contract violation and must
    fail validation rather than be silently accepted.

    ``null``-vs-absent is reproduced by dumping with ``exclude_unset=True``:
    a model is only ever built by validating the body the transport wrote, so
    the fields its serializer never set are exactly the keys the body omitted.
    """

    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ error envelope


@final
class ErrorEnvelope(ResponseModel):
    """The transport's error body: ``{"error", "code"}`` for every failing route.

    ``code`` is the runtime's stable machine-readable reason when the failing
    operation has one (``workspace_busy``, ``invalid_workspace``,
    ``session_sealed``, ``no_pending_approval``, ``no_pending_question``,
    ``delegated_context_missing``) and ``null`` otherwise. Validation failures
    are 400, unmatched paths 404, wrong methods 405, and unhandled failures 500.
    """

    error: str
    code: str | None = None


# ------------------------------------------------------------------- session shapes


@final
class SessionRefBody(ResponseModel):
    """``_serialize_session_ref``: a session identity, child lineage included."""

    id: str
    parent_id: str | None = None


@final
class SessionStateBody(ResponseModel):
    """``_serialize_session_state``: session identity plus status, turn, metadata.

    ``metadata`` is dynamic: it is the runtime-owned persisted session metadata
    map (runtime config, capability snapshots, todos, context projection,
    runtime policy, ...), projected per surface by the runtime itself.
    """

    session: SessionRefBody
    status: SessionStatus
    turn: int
    metadata: dict[str, object]


class EventBody(ResponseModel):
    """``_serialize_event``: one ordered runtime event.

    ``payload`` is dynamic: each event type owns its payload shape (see
    ``voidcode.runtime.events``), and the envelope is what clients route on.
    Reasoning payloads arrive already redacted unless ``show_thinking=true``.
    """

    session_id: str
    sequence: int
    event_type: str
    source: EventSource
    payload: dict[str, object]
    delegated_lifecycle: DelegationEventBody | None = None


@final
class TranscriptEventBody(EventBody):
    """A session-result transcript entry: an event plus its revert-marker state."""

    reverted: bool


@final
class RevertMarkerBody(ResponseModel):
    """``serialize_revert_marker``: where the session was reverted to, if anywhere."""

    sequence: int
    active: bool


@final
class SessionRevertBody(ResponseModel):
    """``POST /api/sessions/{id}/undo|revert|unrevert``: the resulting revert marker.

    ``null`` means the session currently has no active revert marker, which is
    what ``unrevert`` leaves behind.
    """

    revert_marker: RevertMarkerBody | None = None


@final
class RuntimeResponseBody(ResponseModel):
    """``_serialize_runtime_response``: the run/resume/replay/answer surface."""

    session: SessionStateBody
    events: list[EventBody]
    output: str | None = None


@final
class SessionSummaryBody(ResponseModel):
    """``_serialize_stored_session_summary``: one entry of ``GET /api/sessions``."""

    session: SessionRefBody
    status: SessionStatus
    turn: int
    prompt: str
    updated_at: int


@final
class SessionResultBody(ResponseModel):
    """``_serialize_session_result``: the session's terminal result and transcript."""

    session: SessionStateBody
    prompt: str
    status: str
    summary: str
    output: str | None = None
    error: str | None = None
    last_event_sequence: int
    revert_marker: RevertMarkerBody | None = None
    transcript: list[TranscriptEventBody]


@final
class SessionCancelBody(ResponseModel):
    """``ActiveRunInterruptResult.as_payload``: the outcome of a run cancellation.

    ``interrupted`` and ``cancelled`` carry the same value: the run was actually
    stopped (``status == "interrupted"``) rather than merely absent/stale.
    """

    session_id: str
    status: Literal["interrupted", "not_active", "stale"]
    interrupted: bool
    cancelled: bool
    run_id: str | None = None
    reason: str | None = None


@final
class SessionSteerBody(ResponseModel):
    """``POST /api/sessions/{id}/steer``: the queued steering messages' count."""

    session_id: str
    queued: int


# ------------------------------------------------------------------ debug snapshot


@final
class SessionDebugEventBody(ResponseModel):
    """``serialize_session_debug_event``: a bounded event projection (no envelope id)."""

    sequence: int
    event_type: str
    source: str
    payload: dict[str, object]


@final
class SessionDebugPendingApprovalBody(ResponseModel):
    """The debug snapshot's pending-approval projection."""

    request_id: str
    tool_name: str
    target_summary: str
    reason: str
    policy_mode: str
    arguments: dict[str, object]
    owner_session_id: str | None = None
    owner_parent_session_id: str | None = None
    delegated_task_id: str | None = None


@final
class SessionDebugPendingQuestionBody(ResponseModel):
    """The debug snapshot's pending-question projection."""

    request_id: str
    tool_name: str
    question_count: int
    headers: list[str]


@final
class SessionDebugToolSummaryBody(ResponseModel):
    """The debug snapshot's last-tool projection."""

    tool_name: str
    status: str
    summary: str
    arguments: dict[str, object]
    artifact: dict[str, object]
    sequence: int | None = None


@final
class SessionDebugFailureBody(ResponseModel):
    """The debug snapshot's failure classification."""

    classification: str
    message: str


@final
class ProviderContextSegmentBody(ResponseModel):
    """One provider-context segment as the debug surface reports it."""

    index: int
    role: str
    source: str
    content: str | None = None
    content_truncated: bool = False
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: dict[str, object]
    metadata: dict[str, object]


@final
class ProviderContextMessageBody(ResponseModel):
    """One provider-facing message as the debug surface reports it."""

    index: int
    role: str
    source: str
    content: str | None = None
    content_truncated: bool = False
    tool_call_id: str | None = None
    tool_calls: list[dict[str, object]]


@final
class ProviderContextDiagnosticBody(ResponseModel):
    """One provider-context diagnostic.

    ``details`` is dynamic: diagnostics attach free-form context owned by the
    rule that produced them.
    """

    severity: Literal["info", "warning", "error"]
    code: str
    message: str
    source: str | None = None
    segment_indices: list[int]
    suggested_fix: str | None = None
    details: dict[str, object]
    policy_action: RuntimeProviderContextDiagnosticPolicyAction
    policy_blocking: bool


@final
class ProviderContextPolicyDecisionBody(ResponseModel):
    """The provider-context diagnostic policy decision for the last turn."""

    mode: RuntimeProviderContextDiagnosticPolicyMode
    action: RuntimeProviderContextDiagnosticPolicyAction
    blocked: bool
    diagnostic_count: int
    diagnostic_codes: list[str]
    blocking_diagnostic_codes: list[str]
    message: str


@final
class ProviderContextBody(ResponseModel):
    """``serialize_provider_context_snapshot``: what the model was actually sent.

    ``context_window`` is dynamic: it is the window budget projection owned by
    ``runtime/context`` (segment/message budgets, limits, and their sources).
    """

    provider: str
    model: str
    segment_count: int
    message_count: int
    context_window: dict[str, object]
    segments: list[ProviderContextSegmentBody]
    provider_messages: list[ProviderContextMessageBody]
    policy_decision: ProviderContextPolicyDecisionBody | None = None
    diagnostics: list[ProviderContextDiagnosticBody]


@final
class HookPresetBody(ResponseModel):
    """``serialize_hook_preset_snapshot``: the hook presets the runtime resolved."""

    refs: list[str]
    kinds: list[str]
    source: str
    count: int


@final
class RuntimePolicyMaterializationBody(ResponseModel):
    """Fixed marker: this payload is a runtime-materialized policy projection."""

    kind: Literal["runtime_policy_materialized"]
    source: Literal["runtime_control_plane"]
    snapshot_present: Literal[True]


@final
class RuntimePolicyIntentBody(ResponseModel):
    """The bounded intent slice of the runtime policy projection.

    Intent routing is not authoritative today (the materializer records the
    neutral label), so ``label``/``confidence`` stay nullable pass-throughs of
    the persisted snapshot.
    """

    label: str | None = None
    confidence: float | None = None
    authoritative: bool
    matched_rule_ids: list[str]


@final
class RuntimePolicyToolPolicyBody(ResponseModel):
    """The allowed/denied tool projection.

    ``denied`` entries are bounded ``{"target", "reason"}`` objects the
    projection builds itself, so the entry keys are exact.
    """

    allowed: list[str]
    denied: list[dict[str, str]]
    source: str | None = None


@final
class RuntimePolicyDelegationPolicyBody(ResponseModel):
    """The allowed delegated-preset projection plus its bounded denials."""

    allowed_presets: list[str]
    denied: list[dict[str, str]]


@final
class RuntimePolicyHookPolicyBody(ResponseModel):
    """The hook-scope/action projection."""

    allowed_event_scopes: list[str]
    actions: list[str]
    authoritative: bool


@final
class RuntimePolicyPromptActivationBody(ResponseModel):
    """The prompt-activation projection (never the raw prompt)."""

    enabled: bool
    raw_prompt_stored: bool
    activated_this_turn: bool
    activated_refs: list[str]


@final
class RuntimePolicyBody(ResponseModel):
    """``runtime_policy_observability_payload``: the bounded policy projection.

    Scalars here are pass-throughs of the persisted runtime policy snapshot,
    typed from that snapshot's single writer (``runtime.policy``). ``diagnostics``
    and ``precedence_trace`` are the projection's own bounded, redacted shapes:
    trace entries keep whichever of their six keys were scalars.
    """

    schema_version: int | None = None
    policy_version: str | None = None
    mode: str | None = None
    read_only: bool | None = None
    agent_preset: str | None = None
    agent_manifest_id: str | None = None
    materialization: RuntimePolicyMaterializationBody
    intent: RuntimePolicyIntentBody
    tool_policy: RuntimePolicyToolPolicyBody
    delegation_policy: RuntimePolicyDelegationPolicyBody
    hook_policy: RuntimePolicyHookPolicyBody
    prompt_activation: RuntimePolicyPromptActivationBody
    precedence_trace: list[dict[str, str | bool | int | float | None]]
    diagnostics: dict[str, object]
    redacted: Literal[True]
    bounded: Literal[True]


@final
class SessionDebugBody(ResponseModel):
    """``serialize_session_debug_snapshot``: the operator's session debug view.

    ``runtime_policy`` is present only when the session metadata carries a policy
    snapshot; the other projections are explicit ``null`` when absent.
    """

    session: SessionStateBody
    runtime_policy: RuntimePolicyBody | None = None
    prompt: str
    persisted_status: str
    current_status: str
    active: bool
    resumable: bool
    replayable: bool
    terminal: bool
    resume_checkpoint_kind: str | None = None
    pending_approval: SessionDebugPendingApprovalBody | None = None
    pending_question: SessionDebugPendingQuestionBody | None = None
    revert_marker: RevertMarkerBody | None = None
    last_event_sequence: int
    last_relevant_event: SessionDebugEventBody | None = None
    last_failure_event: SessionDebugEventBody | None = None
    failure: SessionDebugFailureBody | None = None
    last_tool: SessionDebugToolSummaryBody | None = None
    provider_context: ProviderContextBody | None = None
    hook_presets: HookPresetBody | None = None
    suggested_operator_action: str
    operator_guidance: str


# ------------------------------------------------------------------- background tasks


@final
class BackgroundTaskRefBody(ResponseModel):
    """``{"id": <task id>}``: the transport's task identity wrapper."""

    id: str


@final
class BackgroundTaskRequestSnapshotBody(ResponseModel):
    """``_serialize_background_task_request_snapshot``: the stored request.

    ``metadata`` is dynamic: it is the runtime request metadata blob the task was
    created with (delegation routing, skills, output schema, ...).
    """

    prompt: str
    session_id: str | None = None
    parent_session_id: str | None = None
    metadata: dict[str, object]
    allocate_session_id: bool


@final
class TaskConcurrencyObservabilityBody(ResponseModel):
    """The concurrency slice of a task's observability."""

    provider: str
    model: str
    limit: int
    limit_source: str
    running_provider: int
    running_model: int
    running_total: int
    active_worker_slots: int
    queued_provider: int
    queued_model: int
    queued_total: int


@final
class TaskRetryObservabilityBody(ResponseModel):
    """The retry slice of a task's observability."""

    retry_count: int
    max_retries: int
    backoff_seconds: float
    next_retry_at: int | None = None


@final
class TaskObservabilityBody(ResponseModel):
    """``BackgroundTaskObservability.as_payload``: why a task waits or ended."""

    waiting_reason: str
    terminal_reason: str | None = None
    queue_position: int | None = None
    concurrency: TaskConcurrencyObservabilityBody | None = None
    retry: TaskRetryObservabilityBody | None = None


@final
class SchemaValidationBody(ResponseModel):
    """``SchemaValidation.as_payload``: the child's structured-output verdict."""

    schema_source: str | None = None
    schema_mode: Literal["permissive", "strict"]
    valid: bool
    error: str | None = None


@final
class SubagentRoutingBody(ResponseModel):
    """``_serialize_subagent_routing``: the requested delegation routing."""

    mode: SubagentExecutionMode
    subagent_type: str | None = None
    description: str | None = None
    command: str | None = None


@final
class DelegatedExecutionBody(ResponseModel):
    """``DelegatedExecutionPayload.as_payload``: parent/child lineage and lifecycle."""

    parent_session_id: str | None = None
    requested_child_session_id: str | None = None
    child_session_id: str | None = None
    delegated_task_id: str | None = None
    approval_request_id: str | None = None
    question_request_id: str | None = None
    routing: SubagentRoutingBody | None = None
    selected_preset: str | None = None
    selected_execution_engine: str | None = None
    lifecycle_status: DelegatedLifecycleStatus | None = None
    approval_blocked: bool
    result_available: bool
    cancellation_cause: str | None = None


@final
class DelegatedLifecycleMessageBody(ResponseModel):
    """``DelegatedLifecycleMessage.as_payload``: the delegated lifecycle summary."""

    kind: Literal["delegated_lifecycle"]
    status: DelegatedLifecycleStatus | None = None
    summary_output: str | None = None
    error: str | None = None
    approval_blocked: bool
    result_available: bool


@final
class DelegationEventBody(ResponseModel):
    """``DelegatedLifecycleEventPayload.as_payload``: an event's delegated view."""

    delegation: DelegatedExecutionBody
    message: DelegatedLifecycleMessageBody
    session_id: str | None = None
    parent_session_id: str | None = None


@final
class BackgroundTaskStateBody(ResponseModel):
    """``_serialize_background_task_state``: the full task state.

    ``output_schema``/``structured_output`` are dynamic JSON-schema/instance
    payloads supplied by the delegating caller.
    """

    task: BackgroundTaskRefBody
    status: BackgroundTaskStatus
    request: BackgroundTaskRequestSnapshotBody
    parent_session_id: str | None = None
    requested_child_session_id: str | None = None
    child_session_id: str | None = None
    approval_request_id: str | None = None
    question_request_id: str | None = None
    result_available: bool
    cancellation_cause: str | None = None
    error: str | None = None
    created_at: int
    created_at_unix_ms: int | None = None
    updated_at: int
    started_at: int | None = None
    started_at_unix_ms: int | None = None
    finished_at: int | None = None
    finished_at_unix_ms: int | None = None
    cancel_requested_at: int | None = None
    keep_alive: bool
    steer_prompt: str | None = None
    routing: SubagentRoutingBody | None = None
    observability: TaskObservabilityBody | None = None
    output_schema: dict[str, object] | None = None
    schema_mode: Literal["permissive", "strict"]
    structured_output: dict[str, object] | None = None
    schema_validation: SchemaValidationBody | None = None


@final
class BackgroundTaskSummaryBody(ResponseModel):
    """``_serialize_background_task_summary``: one entry of the task list routes."""

    task: BackgroundTaskRefBody
    status: BackgroundTaskStatus
    prompt: str
    session_id: str | None = None
    error: str | None = None
    created_at: int
    updated_at: int
    created_at_unix_ms: int | None = None
    keep_alive: bool
    steer_prompt: str | None = None
    output_schema: dict[str, object] | None = None
    schema_mode: Literal["permissive", "strict"]
    observability: TaskObservabilityBody | None = None


@final
class BackgroundTaskResultBody(ResponseModel):
    """``_serialize_background_task_result``: the terminal task read model.

    ``hook_reminder`` is dynamic: it is the hook-owned reminder payload the
    runtime attaches to the result.
    """

    task_id: str
    status: BackgroundTaskStatus
    parent_session_id: str | None = None
    requested_child_session_id: str | None = None
    delegated_prompt: str | None = None
    child_session_id: str | None = None
    approval_request_id: str | None = None
    question_request_id: str | None = None
    approval_blocked: bool
    summary_output: str | None = None
    error: str | None = None
    result_available: bool
    cancellation_cause: str | None = None
    duration_seconds: float | None = None
    tool_call_count: int
    routing: SubagentRoutingBody | None = None
    observability: TaskObservabilityBody | None = None
    hook_reminder: dict[str, object] | None = None
    delegation: DelegatedExecutionBody
    message: DelegatedLifecycleMessageBody
    structured_output: dict[str, object] | None = None
    schema_validation: SchemaValidationBody | None = None


@final
class BackgroundTaskOutputBody(ResponseModel):
    """The shared body of ``/api/tasks/{id}/output`` and ``delegated-context``.

    ``output`` is the resolved child output: the child session's own output, else
    the task's summarized output, else its error.
    """

    task: BackgroundTaskResultBody
    session_result: SessionResultBody | None = None
    output: str | None = None


@final
class BackgroundTaskRetryBody(ResponseModel):
    """``POST /api/tasks/{id}/retry``: the new task plus the retried task id."""

    retry_of_task_id: str
    task: BackgroundTaskStateBody


@final
class BackgroundTaskSteerBody(ResponseModel):
    """``POST /api/tasks/{id}/steer``: the task plus the accepted prompt."""

    steer_prompt: str
    task: BackgroundTaskStateBody


# ------------------------------------------------------------------------- settings


@final
class WebSettingsBody(ResponseModel):
    """``VoidCodeRuntime.web_settings``: the web client's effective provider/model.

    The API key itself is never returned, only whether one is present.
    """

    provider: str | None = None
    provider_api_key_present: bool
    model: str | None = None


# ----------------------------------------------------------------------- workspaces


@final
class WorkspaceSummaryBody(ResponseModel):
    """``_serialize_workspace_summary``: one registry entry."""

    path: str
    label: str
    available: bool
    current: bool
    last_opened_at: int | None = None


@final
class WorkspaceRegistryBody(ResponseModel):
    """``_serialize_workspace_registry_snapshot``: the whole workspace registry."""

    current: WorkspaceSummaryBody | None = None
    recent: list[WorkspaceSummaryBody]
    candidates: list[WorkspaceSummaryBody]


# ------------------------------------------------------------------------ providers


@final
class ProviderSummaryBody(ResponseModel):
    """``_serialize_provider_summary``: one configured/known provider."""

    name: str
    label: str
    configured: bool
    current: bool


@final
class ProviderModelMetadataBody(ResponseModel):
    """``_serialize_provider_model_metadata``: one model's capability record.

    The serializer drops every unset capability instead of emitting ``null``, so
    an entry only carries the capabilities the catalog actually knows.
    """

    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    supports_streaming: bool | None = None
    supports_reasoning: bool | None = None
    supports_json_mode: bool | None = None
    cost_per_input_token: float | None = None
    cost_per_output_token: float | None = None
    cost_per_cache_read_token: float | None = None
    cost_per_cache_write_token: float | None = None
    supports_reasoning_effort: bool | None = None
    default_reasoning_effort: str | None = None
    supported_effort_levels: list[str] | None = None
    supports_reasoning_summary: bool | None = None
    supports_thinking_budget: bool | None = None
    supports_interleaved_reasoning: bool | None = None
    reasoning_visibility: str | None = None
    modalities_input: list[str] | None = None
    modalities_output: list[str] | None = None
    model_status: str | None = None
    tool_feedback_mode: Literal["standard", "synthetic_user_message"] | None = None


@final
class ProviderModelsBody(ResponseModel):
    """``_serialize_provider_models_result``: a provider's model catalog.

    ``model_metadata`` is the dynamic per-model map; its keys are model ids.
    """

    provider: str
    configured: bool
    models: list[str]
    model_metadata: dict[str, ProviderModelMetadataBody]
    source: str | None = None
    last_refresh_status: str | None = None
    last_error: str | None = None
    discovery_mode: str | None = None


@final
class ProviderValidationBody(ResponseModel):
    """``_serialize_provider_validation_result``: the credential check verdict."""

    provider: str
    configured: bool
    ok: bool
    status: str
    message: str
    source: str | None = None
    last_error: str | None = None
    discovery_mode: str | None = None


@final
class ProviderReadinessBody(ResponseModel):
    """``_serialize_provider_readiness_result``: whether the model can be run.

    ``reasoning_controls`` is dynamic: it is the provider-specific reasoning
    control projection assembled by the provider inspection layer.
    """

    provider: str | None = None
    model: str | None = None
    configured: bool
    ok: bool
    status: str
    guidance: str
    auth_present: bool | None = None
    streaming_configured: bool | None = None
    streaming_supported: bool | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    fallback_chain: list[str]
    reasoning_controls: dict[str, object]


@final
class ProviderInspectBody(ResponseModel):
    """``_serialize_provider_inspect_result``: the provider's full inspection."""

    provider: ProviderSummaryBody
    models: ProviderModelsBody
    validation: ProviderValidationBody
    current_model: str | None = None
    current_model_metadata: ProviderModelMetadataBody | None = None
    readiness: ProviderReadinessBody | None = None


# ----------------------------------------------------------------- agents/skills/commands


@final
class AgentSummaryBody(ResponseModel):
    """``_serialize_agent_summary``: one selectable agent.

    ``source_scope``/``source_path`` appear only for agents that came from a
    manifest file.
    """

    id: str
    label: str
    description: str | None = None
    mode: str | None = None
    selectable: bool
    configured: bool
    model: str | None = None
    model_label: str | None = None
    model_source: str | None = None
    provider: str | None = None
    fallback_chain: list[str]
    source_scope: str | None = None
    source_path: str | None = None


@final
class SkillSummaryBody(ResponseModel):
    """``_serialize_skill_summary``: one catalog-visible skill."""

    name: str
    description: str
    origin: str
    source_path: str | None = None


@final
class CommandSummaryBody(ResponseModel):
    """``_serialize_command_summary``: one available slash command."""

    name: str
    description: str
    source: str
    enabled: bool
    hidden: bool
    agent: str | None = None
    model: str | None = None
    subtask: bool
    path: str | None = None


# --------------------------------------------------------------------------- status


@final
class GitStatusBody(ResponseModel):
    """``_serialize_git_status_snapshot``: the workspace's git readiness."""

    state: GitStatusState
    root: str | None = None
    branch: str | None = None
    error: str | None = None


@final
class CapabilityStatusBody(ResponseModel):
    """``_serialize_capability_status_snapshot``: one capability manager's state.

    ``details`` is dynamic: each capability manager owns its own detail map.
    """

    state: CapabilityState
    error: str | None = None
    details: dict[str, object]


@final
class RuntimeBackgroundTaskStatusBody(ResponseModel):
    """The background-task slice of the runtime status snapshot."""

    active_worker_slots: int
    queued_count: int
    running_count: int
    terminal_count: int
    default_concurrency: int
    provider_concurrency: dict[str, int]
    model_concurrency: dict[str, int]
    status_counts: dict[str, int]


@final
class RuntimeStatusBody(ResponseModel):
    """``_serialize_runtime_status_snapshot``: the whole runtime status."""

    git: GitStatusBody
    lsp: CapabilityStatusBody
    mcp: CapabilityStatusBody
    acp: CapabilityStatusBody
    background_tasks: RuntimeBackgroundTaskStatusBody


# --------------------------------------------------------------------------- review


@final
class ReviewChangedFileBody(ResponseModel):
    """``_serialize_review_changed_file``: one changed path."""

    path: str
    change_type: str
    old_path: str | None = None


@final
class ReviewTreeNodeBody(ResponseModel):
    """``_serialize_review_tree_node``: one node of the changed-file tree."""

    path: str
    name: str
    kind: ReviewTreeNodeKind
    changed: bool
    children: list[ReviewTreeNodeBody]


@final
class WorkspaceReviewBody(ResponseModel):
    """``_serialize_workspace_review_snapshot``: the workspace review surface."""

    root: str
    git: GitStatusBody
    changed_files: list[ReviewChangedFileBody]
    tree: list[ReviewTreeNodeBody]


@final
class ReviewFileDiffBody(ResponseModel):
    """``_serialize_review_file_diff``: one file's diff, or why there is none."""

    root: str
    path: str
    state: ReviewFileDiffState
    diff: str | None = None


# -------------------------------------------------------------------- streaming frames


@final
class RunStreamFrameBody(ResponseModel):
    """One frame of ``POST /api/runtime/run/stream``.

    ``kind`` is the chunk's own kind (``event`` or ``output``); ``session`` is the
    serialized session state, emitted on the first frame and whenever it changes,
    and ``null`` in between (the client keeps the state it has). The payload that
    belongs to the kind is the only one that may be non-null.
    """

    kind: Literal["event", "output"]
    session: SessionStateBody | None = None
    event: EventBody | None = None
    output: str | None = None

    @model_validator(mode="after")
    def _require_kind_payload(self) -> Self:
        if self.kind == "event" and self.event is None:
            raise ValueError("event frames must carry an event")
        if self.kind == "output" and self.output is None:
            raise ValueError("output frames must carry output content")
        return self


@final
class SessionEventFrameBody(ResponseModel):
    """One frame of ``GET /api/sessions/{id}/events``.

    The stream opens with a ``session`` frame (the replayed session state, no
    event) and then emits one ``event`` frame per ordered event. This stream
    never carries run output, so ``output`` is always ``null``.
    """

    kind: Literal["session", "event"]
    session: SessionStateBody | None = None
    event: EventBody | None = None
    output: None = None

    @model_validator(mode="after")
    def _require_kind_payload(self) -> Self:
        if self.kind == "session" and self.session is None:
            raise ValueError("session frames must carry the session state")
        if self.kind == "event" and self.event is None:
            raise ValueError("event frames must carry an event")
        return self
