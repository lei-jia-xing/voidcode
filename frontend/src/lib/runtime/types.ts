export type SessionStatus = "idle" | "running" | "waiting" | "completed" | "failed";
export type EventSource = "runtime" | "graph" | "tool";
export type ApprovalDecision = "allow" | "deny";

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
