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
      max_output_tokens?: number | null;
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
}

export interface AgentSummary {
  id: string;
  label: string;
  description?: string | null;
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

export interface RuntimeStatusSnapshot {
  git: GitStatusSnapshot;
  lsp: CapabilityStatusSnapshot;
  mcp: CapabilityStatusSnapshot;
  acp?: CapabilityStatusSnapshot;
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
}

export interface RuntimeRequest {
  prompt: string;
  session_id?: string | null;
  parent_session_id?: string | null;
  metadata?: {
    skills?: string[];
    provider_stream?: boolean;
    [key: string]: unknown;
  };
}

export interface RuntimeResponse {
  session: SessionState;
  events: EventEnvelope[];
  output: string | null;
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
