export type SessionStatus =
  | "idle"
  | "running"
  | "waiting"
  | "completed"
  | "failed";
export type EventSource = "runtime" | "graph" | "tool";
export type ApprovalDecision = "allow" | "deny";
export type GitStatusState = "git_ready" | "not_git_repo" | "git_error";
export type CapabilityState = "running" | "stopped" | "failed" | "unconfigured";
export type AsyncStatus = "idle" | "loading" | "success" | "error";

export interface SessionRef {
  id: string;
  parent_id?: string | null;
}

export interface SessionState {
  session: SessionRef;
  status: SessionStatus;
  turn: number;
  metadata: Record<string, unknown>;
}

export interface StoredSessionSummary {
  session: SessionRef;
  status: SessionStatus;
  turn: number;
  prompt: string;
  updated_at: number;
}

export interface WorkspaceSummary {
  path: string;
  label: string;
  available: boolean;
  current: boolean;
  last_opened_at?: number | null;
}

export interface WorkspaceRegistrySnapshot {
  current: WorkspaceSummary | null;
  recent: WorkspaceSummary[];
  candidates: WorkspaceSummary[];
}

export interface ProviderSummary {
  name: string;
  label: string;
  configured: boolean;
  current: boolean;
}

export interface ProviderModelsResult {
  provider: string;
  configured: boolean;
  models: string[];
  model_metadata?: Record<
    string,
    {
      context_window?: number | null;
      max_input_tokens?: number | null;
      max_output_tokens?: number | null;
      supports_reasoning?: boolean | null;
      supports_reasoning_effort?: boolean | null;
      default_reasoning_effort?: string | null;
    }
  >;
  source?: string | null;
  last_refresh_status?: string | null;
  last_error?: string | null;
  discovery_mode?: string | null;
}

export interface ProviderValidationResult {
  provider: string;
  configured: boolean;
  ok: boolean;
  status: string;
  message: string;
  source?: string | null;
  last_error?: string | null;
  discovery_mode?: string | null;
  failure_kind?: string | null;
  guidance?: string | null;
}

export interface AgentSummary {
  id: string;
  label: string;
  description?: string | null;
  mode?: string | null;
  selectable?: boolean;
  configured?: boolean;
  execution_engine?: string | null;
  model?: string | null;
  model_label?: string | null;
  model_source?: string | null;
  provider?: string | null;
  fallback_chain?: string[];
  source_scope?: string | null;
  source_path?: string | null;
}

export interface GitStatusSnapshot {
  state: GitStatusState;
  root?: string | null;
  error?: string | null;
}

export interface CapabilityStatusSnapshot {
  state: CapabilityState;
  error?: string | null;
  details?: Record<string, unknown>;
}

export interface McpServerStatusDetail {
  server: string;
  status: "running" | "stopped" | "failed";
  workspace_root?: string | null;
  stage?: string | null;
  error?: string | null;
  command?: string[];
  retry_available?: boolean;
}

export interface RuntimeBackgroundTaskStatusSnapshot {
  active_worker_slots: number;
  queued_count: number;
  running_count: number;
  terminal_count: number;
  default_concurrency: number;
  provider_concurrency?: Record<string, number>;
  model_concurrency?: Record<string, number>;
  status_counts?: Record<string, number>;
}

export interface RuntimeStatusSnapshot {
  git: GitStatusSnapshot;
  lsp: CapabilityStatusSnapshot;
  mcp: CapabilityStatusSnapshot;
  acp?: CapabilityStatusSnapshot;
  background_tasks: RuntimeBackgroundTaskStatusSnapshot;
}

export interface ReviewChangedFile {
  path: string;
  change_type:
    | "added"
    | "modified"
    | "deleted"
    | "renamed"
    | "untracked"
    | "copied"
    | "type_changed"
    | "unknown";
  old_path?: string | null;
}

export interface ReviewTreeNode {
  path: string;
  name: string;
  kind: "file" | "directory";
  changed: boolean;
  children: ReviewTreeNode[];
}

export interface ReviewFileDiff {
  root: string;
  path: string;
  state: "changed" | "clean" | "not_git_repo";
  diff?: string | null;
}

export interface WorkspaceReviewSnapshot {
  root: string;
  git: GitStatusSnapshot;
  changed_files: ReviewChangedFile[];
  tree: ReviewTreeNode[];
}

export interface EventEnvelope {
  session_id: string;
  sequence: number;
  event_type: string;
  source: EventSource;
  payload: Record<string, unknown>;
  received_at?: number;
}

export interface ToolDisplay {
  kind: string;
  title: string;
  summary: string;
  args?: string[];
  copyable?: Record<string, unknown>;
  hidden?: boolean;
}

export interface ToolStatusPayload {
  invocation_id?: unknown;
  tool_name?: unknown;
  phase?: unknown;
  status?: unknown;
  label?: unknown;
  display?: unknown;
}

export interface RuntimeRequest {
  prompt: string;
  session_id?: string | null;
  parent_session_id?: string | null;
  metadata?: {
    skills?: string[];
    provider_stream?: boolean;
    reasoning_effort?: string;
    [key: string]: unknown;
  };
}

export interface RuntimeResponse {
  session: SessionState;
  events: EventEnvelope[];
  output: string | null;
}

export interface RuntimeInterruptResult {
  session_id: string;
  status: "interrupted" | "not_active" | "stale";
  interrupted: boolean;
  cancelled: boolean;
  run_id?: string | null;
  reason?: string | null;
}

export interface ApiErrorPayload {
  error?: string;
  code?: string;
}

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

export interface QuestionAnswer {
  header: string;
  answers: string[];
}

export interface RuntimeNotification {
  id: string;
  session: SessionRef;
  kind:
    | "completion"
    | "failure"
    | "cancellation"
    | "approval_blocked"
    | "question_blocked";
  status: "unread" | "acknowledged";
  summary: string;
  event_sequence: number;
  created_at: number;
  acknowledged_at?: number | null;
  payload: Record<string, unknown>;
}

export interface BackgroundTaskRequestSnapshot {
  prompt: string;
  session_id?: string | null;
  parent_session_id?: string | null;
  metadata: Record<string, unknown>;
  allocate_session_id?: boolean;
}

export interface RuntimeRequest {
  prompt: string;
  session_id?: string | null;
  parent_session_id?: string | null;
  metadata?: {
    skills?: string[];
    provider_stream?: boolean;
    reasoning_effort?: string;
    [key: string]: unknown;
  };
  allocate_session_id?: boolean;
}

export interface BackgroundTaskRouting {
  mode: string;
  category?: string | null;
  subagent_type?: string | null;
  description?: string | null;
  command?: string | null;
}

export interface BackgroundTaskSummary {
  task: { id: string };
  status: string;
  prompt: string;
  session_id?: string | null;
  error?: string | null;
  created_at: number;
  updated_at: number;
  created_at_unix_ms?: number | null;
  observability?: Record<string, unknown> | null;
}

export interface BackgroundTaskState {
  task: { id: string };
  status: string;
  request: BackgroundTaskRequestSnapshot;
  parent_session_id?: string | null;
  requested_child_session_id?: string | null;
  child_session_id?: string | null;
  approval_request_id?: string | null;
  question_request_id?: string | null;
  result_available: boolean;
  cancellation_cause?: string | null;
  error?: string | null;
  created_at: number;
  updated_at: number;
  started_at?: number | null;
  finished_at?: number | null;
  cancel_requested_at?: number | null;
  routing?: BackgroundTaskRouting | null;
  created_at_unix_ms?: number | null;
  started_at_unix_ms?: number | null;
  finished_at_unix_ms?: number | null;
  observability?: Record<string, unknown> | null;
}

export interface BackgroundTaskResultPayload {
  task_id: string;
  status: string;
  parent_session_id?: string | null;
  requested_child_session_id?: string | null;
  child_session_id?: string | null;
  approval_request_id?: string | null;
  question_request_id?: string | null;
  approval_blocked: boolean;
  summary_output?: string | null;
  error?: string | null;
  result_available: boolean;
  cancellation_cause?: string | null;
  routing?: BackgroundTaskRouting | null;
  delegation?: Record<string, unknown>;
  message?: Record<string, unknown>;
  duration_seconds?: number | null;
  tool_call_count?: number;
  observability?: Record<string, unknown> | null;
}

export interface BackgroundTaskOutput {
  task: BackgroundTaskResultPayload;
  session_result: RuntimeSessionResult | null;
  output: string | null;
}

export interface RuntimeSessionRevertMarker {
  sequence: number;
  active: boolean;
}

export interface RuntimeSessionResult {
  session: SessionState;
  prompt: string;
  status: string;
  summary: string;
  output?: string | null;
  error?: string | null;
  last_event_sequence?: number | null;
  transcript: EventEnvelope[];
  revert_marker?: RuntimeSessionRevertMarker | null;
}

export interface RuntimeSessionDebugEvent {
  sequence: number;
  event_type: string;
  source: EventSource | string;
  payload: Record<string, unknown>;
}

export interface RuntimeHookPresetSnapshot {
  refs: string[];
  kinds: string[];
  source: string;
  count: number;
}

export interface RuntimeProviderContextSegment {
  index: number;
  role: string;
  source: string;
  content?: string | null;
  content_truncated?: boolean;
  tool_call_id?: string | null;
  tool_name?: string | null;
  tool_arguments?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

export interface RuntimeProviderContextSnapshot {
  provider: string;
  model: string;
  execution_engine: string;
  segment_count: number;
  message_count: number;
  context_window?: Record<string, unknown>;
  segments?: RuntimeProviderContextSegment[];
  provider_messages?: Record<string, unknown>[];
  diagnostics?: Record<string, unknown>[];
  policy_decision?: Record<string, unknown> | null;
}

export interface RuntimeSessionDebugSnapshot {
  session: SessionState;
  prompt: string;
  persisted_status: string;
  current_status: string;
  active: boolean;
  resumable: boolean;
  replayable: boolean;
  terminal: boolean;
  resume_checkpoint_kind?: string | null;
  pending_approval?: {
    request_id: string;
    tool_name: string;
    target_summary: string;
    reason: string;
    policy_mode: string;
    arguments: Record<string, unknown>;
    owner_session_id?: string | null;
    owner_parent_session_id?: string | null;
    delegated_task_id?: string | null;
    path_scope?: string | null;
    operation_class?: string | null;
    canonical_path?: string | null;
    matched_rule?: string | null;
    policy_surface?: string | null;
  } | null;
  pending_question?: {
    request_id: string;
    tool_name: string;
    question_count: number;
    headers: string[];
  } | null;
  revert_marker?: RuntimeSessionRevertMarker | null;
  last_event_sequence?: number | null;
  last_relevant_event?: RuntimeSessionDebugEvent | null;
  last_failure_event?: RuntimeSessionDebugEvent | null;
  failure?: { classification: string; message: string } | null;
  last_tool?: {
    tool_name: string;
    status: string;
    summary: string;
    arguments: Record<string, unknown>;
    artifact?: Record<string, unknown>;
    sequence?: number | null;
  } | null;
  provider_context?: RuntimeProviderContextSnapshot | null;
  hook_presets?: RuntimeHookPresetSnapshot | null;
  suggested_operator_action?: string | null;
  operator_guidance?: string | null;
}

export type RuntimeStreamChunkKind = "event" | "output";

export interface RuntimeStreamChunk {
  kind: RuntimeStreamChunkKind;
  session: SessionState;
  event: EventEnvelope | null;
  output: string | null;
}

export interface RuntimeSettings {
  provider?: string;
  provider_api_key_present?: boolean;
  model?: string;
}

export interface RuntimeSettingsUpdate {
  provider?: string;
  provider_api_key?: string;
  model?: string;
}
