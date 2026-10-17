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

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

    ``metadata`` is a bounded projection of the runtime-owned persisted
    metadata (runtime config, capability snapshots, todos, context projection,
    runtime policy, ...). Durable phase snapshots and the composition turn
    cursor are private and never included.
    """

    session: SessionRefBody
    status: SessionStatus
    turn: int
    metadata: dict[str, object]


class EventBody(ResponseModel):
    """``_serialize_event``: one ordered runtime event.

    ``payload`` is dynamic: each event type owns its payload shape (see
    ``voidcode.runtime.events``), and the envelope is what clients route on.
    Reasoning payload text is omitted unless ``show_thinking=true``.
    """

    session_id: str
    sequence: int
    event_type: str
    source: EventSource
    payload: dict[str, object]
    delegated_lifecycle: DelegationEventBody | None = None


@final
class RuntimeResponseBody(ResponseModel):
    """``_serialize_runtime_response``: the run/resume/replay/answer surface."""

    session: SessionStateBody
    events: list[EventBody]
    output: str | None = None


@final
class SessionSummaryBody(ResponseModel):
    """``_serialize_stored_session_summary``: one entry of ``GET /api/sessions``.

    ``title`` is the user-settable label (``null`` until a rename), not a
    prompt-derived one: deriving display text stays client-side so the server
    never ships a second copy of the client's labeling rules.

    ``depth`` is the row's topological fork depth, read from the runtime's
    ``session_forest`` projection rather than re-derived here. ``null`` means
    the session is not part of that forest (this list is filtered to top-level
    sessions, which the forest superset can omit); the client renders ``null``
    at depth 0.

    The list itself is emitted in that same forest order — roots by recency,
    each parent before its children — so a client renders the array as sent and
    never re-sorts it.
    """

    session: SessionRefBody
    status: SessionStatus
    turn: int
    prompt: str
    updated_at: int
    title: str | None = None
    depth: int | None = None


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
    title: str | None = None
    transcript: list[EventBody]


@final
class RuntimeCompactBody(ResponseModel):
    """Result of the native ``/compact`` session action."""

    session_id: str
    compacted: bool
    reason: str | None = None
    event_sequence: int | None = None
    original_tool_result_count: int | None = None
    retained_tool_result_count: int | None = None


@final
class UserShellResultBody(ResponseModel):
    """Result of the native ``!`` shell escape (no provider turn started)."""

    session_id: str
    command: str
    exit_code: int
    status: str
    duration_seconds: float
    output: str
    truncated: bool
    timed_out: bool
    event_sequence: int


@final
class SideQuestionResultBody(ResponseModel):
    """Result of the native ``/btw`` side question (no history written)."""

    session_id: str
    question: str
    answer: str


@final
class SessionLoopResultBody(ResponseModel):
    """Result of scheduling a native /loop recurring task."""

    session_id: str
    message: str
    schedule_id: str | None = None
    schedule: str | None = None
    human_schedule: str | None = None
    prompt: str | None = None
    task_id: str | None = None
    is_usage: bool = False


@final
class CollaborationModeBody(ResponseModel):
    session_id: str
    collaboration_mode: str
    previous_mode: str
    event_sequence: int | None = None
    unchanged: bool | None = None


@final
class ThreadGoalItem(ResponseModel):
    thread_id: str
    objective: str
    status: str
    token_budget: int | None = None
    tokens_used: int = 0
    time_used_seconds: int = 0
    created_at: int = 0
    updated_at: int = 0


@final
class GoalResponseBody(ResponseModel):
    goal: ThreadGoalItem | None = None


@final
class ClearGoalResponseBody(ResponseModel):
    cleared: bool


@final
class SessionStatusPanelBody(ResponseModel):
    """Native ``/status`` panel: session, model, approval, usage, provider."""

    session_id: str
    title: str | None = None
    status: str
    turn: int
    model: str | None = None
    provider: str | None = None
    reasoning_effort: str | None = None
    approval_mode: str | None = None
    collaboration_mode: str
    goal_objective: str | None = None
    goal_status: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    workspace: str | None = None
    base_url: str | None = None
    forked_from_session_id: str | None = None


@final
class SessionExportBody(ResponseModel):
    """Native ``/export`` transcript: full Markdown plus the default filename."""

    session_id: str
    filename: str
    markdown: str


@final
class SessionExportWriteBody(ResponseModel):
    """Native ``/export <path>``: where the transcript was written."""

    session_id: str
    path: str
    filename: str


@final
class CopyTargetBody(ResponseModel):
    """One native ``/copy`` choice: label, preview, full text."""

    label: str
    preview: str
    text: str


@final
class SessionCopyTargetsBody(ResponseModel):
    """Native ``/copy`` choices from the last assistant response."""

    session_id: str
    targets: list[CopyTargetBody]


@final
class ContextUsageItemBody(ResponseModel):
    """One per-item row of native ``/context all``."""

    label: str
    tokens: int


@final
class ContextUsageSegmentBody(ResponseModel):
    """One per-segment bucket of native ``/context``."""

    label: str
    tokens: int
    items: list[ContextUsageItemBody]


@final
class SessionContextUsageBody(ResponseModel):
    """Native ``/context`` usage from the real assembly projection."""

    session_id: str
    model: str | None = None
    used_tokens: int
    total_tokens: int | None = None
    percent: float | None = None
    over_budget: bool
    estimated: bool
    segments: list[ContextUsageSegmentBody]


@final
class McpServerProjectionBody(ResponseModel):
    """One configured MCP server of native ``/mcp``."""

    name: str
    status: str
    transport: str | None = None
    auth: str | None = None
    tool_count: int | None = None
    tools: list[str] | None = None
    command: list[str] | None = None
    url: str | None = None
    stage: str | None = None
    error: str | None = None


@final
class SessionMcpInventoryBody(ResponseModel):
    """Native ``/mcp`` inventory: configured servers and passive tool surface."""

    session_id: str
    verbose: bool
    servers: list[McpServerProjectionBody]


@final
class HookSurfaceProjectionBody(ResponseModel):
    """One lifecycle-hook surface of native ``/hooks``."""

    surface: str
    commands: list[str]
    match: list[str] | None = None


@final
class SessionHooksListingBody(ResponseModel):
    """Native ``/hooks`` listing: event → commands from the effective config."""

    session_id: str
    enabled: bool
    surfaces: list[HookSurfaceProjectionBody]


@final
class SkillProjectionBody(ResponseModel):
    """One skill of native ``/skills``."""

    name: str
    description: str
    origin: str
    source_path: str | None = None
    enabled: bool = True


@final
class SessionSkillsListingBody(ResponseModel):
    """Native ``/skills`` listing from the skill registry."""

    session_id: str
    skills: list[SkillProjectionBody]


@final
class AgentSessionRowBody(ResponseModel):
    """One live agent session of native ``/agents``."""

    session_id: str
    title: str | None = None
    status: str
    turn: int
    kind: str


@final
class AgentTaskRowBody(ResponseModel):
    """One background child of native ``/agents``."""

    task_id: str
    status: str
    child_session_id: str | None = None


@final
class SessionTokenUsageBody(ResponseModel):
    """Native ``/usage`` activity from session cumulative metadata."""

    session_id: str
    scope: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    turn_count: int | None = None
    estimated: bool


@final
class SessionModelBody(ResponseModel):
    """Session active model and reasoning effort."""

    session_id: str
    model: str
    reasoning_effort: str | None = None
    provider: str | None = None


@final
class SessionPermissionsBody(ResponseModel):
    """Session permissions and approval mode."""

    session_id: str
    approval_mode: str
    preset: str
    read_only: bool | None = None


@final
class PendingApprovalItemBody(ResponseModel):
    """One pending approval item waiting in the session queue."""

    request_id: str
    tool_name: str
    arguments: dict[str, object]
    target_summary: str
    reason: str
    policy_mode: str
    session_id: str


@final
class ApproveActionResultBody(ResponseModel):
    """Result of approving a pending or denied action."""

    approved: bool
    session_id: str | None = None
    request_id: str | None = None
    tool_name: str | None = None
    target_summary: str | None = None
    reason: str | None = None
    message: str | None = None


@final
class SessionEntryBody(ResponseModel):
    """``serialize_session_entry_summary``: one entry of ``GET /api/sessions/{id}/entries``.

    The frozen ``SessionEntrySummary`` field set verbatim. ``parent_sequence``
    is the ancestor edge (``null`` for the session's first entry), so a client
    renders the in-session tree without a second read; ``on_current_path`` is
    false exactly for the branches a checkout left behind, which is how the
    listing shows an abandoned continuation as still stored and re-selectable.
    """

    sequence: int
    event_type: str
    parent_sequence: int | None = None
    on_current_path: bool
    preview: str


@final
class SessionEntriesBody(ResponseModel):
    """``GET /api/sessions/{id}/entries``: the session's stored entry listing."""

    session_id: str
    entries: list[SessionEntryBody]


@final
class SessionCheckoutBody(ResponseModel):
    """``POST /api/sessions/{id}/checkout``: the session's new leaf position.

    A checkout is a position change, so the body is the moved-to ``sequence``
    itself; the abandoned continuation stays stored off the path.
    """

    session_id: str
    leaf_sequence: int


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
class RuntimePolicyBody(ResponseModel):
    """``runtime_policy_observability_payload``: the bounded policy projection.

    Scalars here are pass-throughs of the persisted runtime policy snapshot,
    typed from that snapshot's single writer (``runtime.policy``). ``diagnostics``
    and ``precedence_trace`` are bounded projections:
    trace entries keep whichever of their six keys were scalars.
    """

    schema_version: int | None = None
    policy_version: str | None = None
    read_only: bool | None = None
    agent_preset: str | None = None
    agent_manifest_id: str | None = None
    materialization: RuntimePolicyMaterializationBody
    intent: RuntimePolicyIntentBody
    tool_policy: RuntimePolicyToolPolicyBody
    delegation_policy: RuntimePolicyDelegationPolicyBody
    hook_policy: RuntimePolicyHookPolicyBody
    precedence_trace: list[dict[str, str | bool | int | float | None]]
    diagnostics: dict[str, object]
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
    handoff: dict[str, object] | None = None
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
    api: str | None = None
    display_name: str | None = None


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
    enabled: bool = True


@final
class SkillToggleRequestBody(BaseModel):
    """Payload to enable or disable a skill."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    enabled: bool
    scope: Literal["workspace", "user"] = "workspace"


@final
class SkillToggleResponseBody(ResponseModel):
    """Result of toggling skill enablement."""

    name: str
    enabled: bool


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


@final
class SessionAgentsOverviewBody(ResponseModel):
    """Native ``/agents`` overview: sessions + background children + presets."""

    session_id: str
    current_session_id: str | None = None
    sessions: list[AgentSessionRowBody]
    tasks: list[AgentTaskRowBody]
    presets: list[AgentSummaryBody]


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


@final
class ReviewBranchesBody(ResponseModel):
    branches: list[str]
    current: str | None = None


@final
class ReviewCommitEntryBody(ResponseModel):
    sha: str
    timestamp: int
    subject: str


@final
class ReviewCommitsBody(ResponseModel):
    commits: list[ReviewCommitEntryBody]


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
