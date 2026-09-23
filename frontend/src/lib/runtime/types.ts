/**
 * The frontend's runtime data vocabulary.
 *
 * Two ownership classes live in this file and each block says which one it is:
 *
 * 1. **Backend-owned wire shapes** are bindings to `./generated/api`, which is
 *    generated from the document the transport serves at `GET /api/openapi.json`
 *    (`mise run frontend:api:generate`; `mise run frontend:api:check` fails when
 *    the committed file is stale). No shape below is restated by hand: change the
 *    pydantic model in `src/voidcode/runtime/transport/http_models.py`, or the
 *    runtime contract behind it, and regenerate. The short names exist because
 *    the app reads better without the transport's `...Body` suffix — they are
 *    bindings, not definitions.
 * 2. **Frontend-owned shapes** are the ones the backend does not describe: local
 *    UI state, request bodies narrowed to what the shell actually sends, and the
 *    event *payload* shapes the transport deliberately leaves dynamic. They are
 *    hand-written and marked as such.
 *
 * Where the document is weaker than the wire, the refinement is a documented
 * intersection or an explicit local type rather than a rewrite of the generated
 * shape — `EventEnvelope` (the frontend's own receive-time stamp),
 * `ReviewChangedFile` (an enum the transport declares as a plain string),
 * `RuntimeRequest` (a body the route narrows in a validator), `QuestionAnswer`
 * and `ApprovalDecision`. Every one of them narrows; none may widen.
 *
 * One property of the generated shapes is worth stating because it shows up at
 * every read site: an optional field is `field?: T | null`. Pydantic marks a
 * field with a default as *not required* in JSON Schema, so the document cannot
 * distinguish "the serializer always writes this key, sometimes with `null`"
 * from "the serializer drops the key when unset". Read sites therefore treat
 * every optional field as possibly absent (`??`/`?.`), which is what the
 * transport's own `null`-vs-absent contract allows. Making those fields required
 * would be wrong in the other direction: the models that omit unset keys
 * (`_absent_when_none`) really do drop them.
 *
 * This module is compile-time only. Nothing here validates a response body, and
 * no runtime check may be derived from it: the transport's bodies are pinned by
 * `tests/integration/test_http_response_schema.py`.
 */
import type { components } from "./generated/api";

/** The generated schema map, keyed by the transport's model names. */
type Schemas = components["schemas"];

// ------------------------------------------------------- backend-owned wire shapes

export type SessionStatus = Schemas["SessionStatus"];
export type EventSource = Schemas["EventSource"];
export type GitStatusState = Schemas["GitStatusState"];
export type CapabilityState = Schemas["CapabilityState"];
export type ReviewTreeNodeKind = Schemas["ReviewTreeNodeKind"];
export type BackgroundTaskStatus = Schemas["BackgroundTaskStatus"];

export type SessionRef = Schemas["SessionRefBody"];
export type SessionState = Schemas["SessionStateBody"];
export type StoredSessionSummary = Schemas["SessionSummaryBody"];
export type WorkspaceSummary = Schemas["WorkspaceSummaryBody"];
export type WorkspaceRegistrySnapshot = Schemas["WorkspaceRegistryBody"];
export type ProviderSummary = Schemas["ProviderSummaryBody"];
export type ProviderModelsResult = Schemas["ProviderModelsBody"];
/** One model's capability record inside `ProviderModelsResult.model_metadata`. */
export type ProviderModelMetadata = Schemas["ProviderModelMetadataBody"];
export type ProviderValidationResult = Schemas["ProviderValidationBody"];
export type AgentSummary = Schemas["AgentSummaryBody"];
export type SkillSummary = Schemas["SkillSummaryBody"];
export type CommandSummary = Schemas["CommandSummaryBody"];
export type GitStatusSnapshot = Schemas["GitStatusBody"];
export type CapabilityStatusSnapshot = Schemas["CapabilityStatusBody"];
export type RuntimeBackgroundTaskStatusSnapshot =
  Schemas["RuntimeBackgroundTaskStatusBody"];
export type RuntimeStatusSnapshot = Schemas["RuntimeStatusBody"];
export type ReviewTreeNode = Schemas["ReviewTreeNodeBody"];
export type ReviewFileDiff = Schemas["ReviewFileDiffBody"];
export type RuntimeResponse = Schemas["RuntimeResponseBody"];
export type RuntimeInterruptResult = Schemas["SessionCancelBody"];
/** `POST /api/sessions/{id}/steer`: how many messages the queue accepted. */
export type SessionSteerResult = Schemas["SessionSteerBody"];
export type BackgroundTaskRequestSnapshot =
  Schemas["BackgroundTaskRequestSnapshotBody"];
export type BackgroundTaskRouting = Schemas["SubagentRoutingBody"];
export type BackgroundTaskSummary = Schemas["BackgroundTaskSummaryBody"];
export type BackgroundTaskState = Schemas["BackgroundTaskStateBody"];
export type BackgroundTaskResultPayload = Schemas["BackgroundTaskResultBody"];
export type BackgroundTaskOutput = Schemas["BackgroundTaskOutputBody"];
export type BackgroundTaskRetryResponse = Schemas["BackgroundTaskRetryBody"];
export type BackgroundTaskSteerResponse = Schemas["BackgroundTaskSteerBody"];
export type RuntimeSessionResult = Schemas["SessionResultBody"];
/** One entry of a session result's transcript: an event plus its revert-marker state. */
export type TranscriptEvent = Schemas["TranscriptEventBody"];
export type RuntimeSessionDebugEvent = Schemas["SessionDebugEventBody"];
export type RuntimeSessionDebugSnapshot = Schemas["SessionDebugBody"];
export type ProviderContextSegmentSnapshot =
  Schemas["ProviderContextSegmentBody"];
export type ProviderContextSnapshot = Schemas["ProviderContextBody"];
/** `GET /api/settings`: the effective provider/model, never the API key itself. */
export type RuntimeSettings = Schemas["WebSettingsBody"];
/** `POST /api/settings`: the fields the client may set. */
export type RuntimeSettingsUpdate = Schemas["_SettingsRequestPayload"];
/** One request item inside `_QuestionAnswerRequestPayload.responses`. */
type QuestionAnswerItem = NonNullable<
  Schemas["_QuestionAnswerRequestPayload"]["responses"]
>[number];

/**
 * One entry of the `responses` array `POST /api/sessions/{id}/question` accepts.
 *
 * Refinement: the document types both fields of the request item as optional and
 * nullable (the route checks them in validators), so the generated item admits
 * `{}`. The shell builds these entries, so `header` and `answers` are required
 * here: exactly the two fields the route rejects when they are missing or blank.
 * Emptiness of an individual answer string is still the route's own 400.
 */
export type QuestionAnswer = QuestionAnswerItem & {
  header: string;
  answers: string[];
};

/**
 * One ordered runtime event, as the shell carries it.
 *
 * Backend-owned except for `received_at`: the transport sends the envelope and
 * the frontend stamps its own receive time on top (the reasoning-duration
 * projection reads it), so the stamp is an addition to the generated shape.
 */
export type EventEnvelope = Schemas["EventBody"] & { received_at?: number };

/**
 * One changed path from `GET /api/review`.
 *
 * Refinement: `ReviewChangedFile.change_type` is a `Literal` in
 * `runtime/review.py`, but the transport's `ReviewChangedFileBody` declares it as
 * a plain `str`, so the document publishes `string`. The frontend keeps the
 * union the runtime can actually emit — every value is produced by
 * `ReviewParser._map_change_type`.
 */
export type ReviewChangedFile = Omit<
  Schemas["ReviewChangedFileBody"],
  "change_type"
> & {
  change_type:
    | "added"
    | "modified"
    | "deleted"
    | "renamed"
    | "untracked"
    | "copied"
    | "type_changed"
    | "unknown";
};

/**
 * `GET /api/review`: the workspace review surface.
 *
 * Refinement: `changed_files` carries the narrowed {@link ReviewChangedFile}, for
 * the reason stated there.
 */
export type WorkspaceReviewSnapshot = Omit<
  Schemas["WorkspaceReviewBody"],
  "changed_files"
> & { changed_files: ReviewChangedFile[] };

/**
 * The body of `POST /api/runtime/run/stream`.
 *
 * Refinement: the document describes the transport's boundary model
 * (`_RunStreamRequestPayload`), which declares `prompt` optional and nullable and
 * leaves `metadata` a free object, because the route enforces the rest in field
 * validators. The shell builds these bodies, so the intersection states what the
 * shell must send: `prompt` is present, and `metadata` names the keys the shell
 * sets. What it does *not* enforce at compile time is emptiness — `prompt: string`
 * admits `""`, and a blank prompt is still the route's own 400 to report.
 */
export type RuntimeRequest = Schemas["_RunStreamRequestPayload"] & {
  prompt: string;
  metadata?: {
    agent?: Record<string, unknown>;
    mode?: "normal" | "plan";
    read_only?: boolean;
    skills?: string[];
    force_load_skills?: string[];
    context_transform_refs?: string[];
    delegation?: Record<string, unknown>;
    provider_stream?: boolean;
    reasoning_effort?: string;
    [key: string]: unknown;
  };
};

/**
 * One decoded server-sent-events frame.
 *
 * Refinement of the two frame schemas the document publishes
 * (`RunStreamFrameBody` for `POST /api/runtime/run/stream`, `SessionEventFrameBody`
 * for `GET /api/sessions/{id}/events`): the transport writes all four keys on
 * every frame with an explicit `null` for the slots the kind does not use
 * (`_serialize_runtime_stream_chunk`, `_session_event_frames`), so the payload
 * slots are present-or-null here instead of optional, and `event` is the
 * `EventEnvelope` refinement that carries the receive-time stamp. `kind` is the
 * generated union of both frame kinds.
 */
type GeneratedStreamFrame =
  Schemas["RunStreamFrameBody"] | Schemas["SessionEventFrameBody"];

export type RuntimeStreamChunk = {
  kind: GeneratedStreamFrame["kind"];
  session: NonNullable<GeneratedStreamFrame["session"]> | null;
  event: EventEnvelope | null;
  output: NonNullable<GeneratedStreamFrame["output"]> | null;
};

// --------------------------------------------------------------- frontend-owned

/** The shell's view of an asynchronous panel: local state, not a wire shape. */
export type AsyncStatus = "idle" | "loading" | "success" | "error";

/**
 * The two decisions the approval route accepts.
 *
 * Backend-owned in spirit — `_ApprovalResolutionRequestPayload` validates
 * `decision` against exactly these two words — but the document publishes the
 * field as a nullable string, so the union is stated here instead of derived.
 */
export type ApprovalDecision = "allow" | "deny";

/** One render instruction the shell derives from a tool event's payload. */
export interface ToolDisplay {
  kind: string;
  title: string;
  summary: string;
  args?: string[];
  copyable?: Record<string, unknown>;
  hidden?: boolean;
}
/** Read-only live preview for a write-like tool call; never an execution result. */
export interface ToolDiffPreview {
  path?: string;
  kind?: string;
  old_text?: string;
  new_text?: string;
  diff?: string;
  truncated?: boolean;
  degraded?: boolean;
  error?: string;
}

/**
 * A tool event's `tool_status` payload, as the shell reads it.
 *
 * Frontend-owned: the transport keeps event payloads dynamic
 * (`EventBody.payload: dict[str, object]`), so no schema describes this shape.
 */
export interface ToolStatusPayload {
  invocation_id: string;
  tool_name: string;
  phase: string;
  status: string;
  label?: string;
  display: ToolDisplay;
}

/**
 * The runtime's status details for one MCP server.
 *
 * Frontend-owned: `CapabilityStatusBody.details` is the capability manager's own
 * dynamic map, and this is the MCP manager's slice of it.
 */
export interface McpServerStatusDetail {
  server: string;
  status: "running" | "stopped" | "failed";
  workspace_root?: string | null;
  stage?: string | null;
  error?: string | null;
  command?: string[];
  retry_available?: boolean;
  scope?: string | null;
  transport?: string | null;
}

/**
 * The runtime's status details for one language server.
 *
 * Frontend-owned: the LSP manager's slice of `CapabilityStatusBody.details`.
 */
export interface LspServerStatusDetail {
  server: string;
  status: "running" | "stopped" | "starting" | "failed" | "disabled";
  available?: boolean;
  command?: string[];
  error?: string | null;
}

/**
 * One question prompt the shell renders from a question event's payload.
 *
 * Frontend-owned: the prompt arrives inside a dynamic event payload, so no
 * schema describes it.
 */
export interface QuestionOption {
  label: string;
  description?: string | null;
}

export interface QuestionPrompt {
  header: string;
  question?: string | null;
  multiple: boolean;
  options: QuestionOption[];
}
