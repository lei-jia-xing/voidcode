import {
  AgentSummary,
  CommandSummary,
  SkillSummary,
  BackgroundTaskOutput,
  ChildSessionContextResult,
  BackgroundTaskSummary,
  BackgroundTaskState,
  BackgroundTaskRetryResponse,
  BackgroundTaskSteerResponse,
  RuntimeRequest,
  StoredSessionSummary,
  RuntimeResponse,
  RuntimeResumeResponse,
  RuntimeInterruptResult,
  RuntimeStreamChunk,
  ApprovalDecision,
  QuestionAnswer,
  ProviderModelsResult,
  ProviderSummary,
  ProviderValidationResult,
  RuntimeSessionDebugSnapshot,
  RuntimeSettings,
  RuntimeSettingsUpdate,
  RuntimeStatusSnapshot,
  ReviewFileDiff,
  WorkspaceRegistrySnapshot,
  WorkspaceReviewSnapshot,
  RuntimeNotification,
} from "./types";

import { SseFrameParser, parseSseDataPayload } from "./sse-parser";

export class RuntimeClientError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = "RuntimeClientError";
  }
}

interface RuntimeErrorPayload {
  /** Rendered `<fallback>: <error> (<code>)` text, for display. */
  message: string;
  /** The stable backend error code, when the envelope carried one. */
  code?: string;
}

/**
 * Read the transport's `{error, code}` envelope once.
 *
 * The rendered message keeps the historical shape for UI text; the structured
 * `code` is what callers route on, so a reworded backend message can no longer
 * change client behaviour.
 */
async function runtimeErrorPayload(
  res: Response,
  fallback: string,
): Promise<RuntimeErrorPayload> {
  let payload: unknown;
  try {
    payload = await res.clone().json();
  } catch {
    return { message: `${fallback}: ${res.statusText || res.status}` };
  }

  if (payload !== null && typeof payload === "object") {
    const error = "error" in payload ? payload.error : undefined;
    const code = "code" in payload ? payload.code : undefined;
    const stableCode =
      typeof code === "string" && code.length > 0 ? code : undefined;
    if (typeof error === "string" && error.length > 0) {
      return {
        message: stableCode
          ? `${fallback}: ${error} (${stableCode})`
          : `${fallback}: ${error}`,
        code: stableCode,
      };
    }
  }

  return { message: `${fallback}: ${res.statusText || res.status}` };
}

async function expectOk(res: Response, fallback: string): Promise<void> {
  if (!res.ok) {
    const { message, code } = await runtimeErrorPayload(res, fallback);
    throw new RuntimeClientError(message, res.status, code);
  }
}

/**
 * Read one query endpoint, with an optional abort signal.
 *
 * The signal is what makes a superseded request a real cancellation: the query
 * layer aborts it, `fetch` rejects with `AbortError`, and the caller's promise
 * settles instead of decoding an answer nothing will use. The options object is
 * only added when a signal exists, so an uncancellable read issues exactly the
 * request it always did.
 */
function fetchQuery(url: string, signal?: AbortSignal): Promise<Response> {
  return signal === undefined ? fetch(url) : fetch(url, { signal });
}

function encodePathSegments(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

function withShowThinking(path: string): string {
  return path.includes("?")
    ? `${path}&show_thinking=true`
    : `${path}?show_thinking=true`;
}

async function* readSseStream(
  res: Response,
  fallback: string,
): AsyncGenerator<RuntimeStreamChunk, void, unknown> {
  await expectOk(res, fallback);
  if (!res.body) throw new Error("No response body for stream");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseFrameParser();
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
      const chunk = parseSseDataPayload(frame);
      if (chunk) yield chunk;
    }
  }
  for (const frame of parser.flush()) {
    const chunk = parseSseDataPayload(frame);
    if (chunk) yield chunk;
  }
}

export class RuntimeClient {
  static async *sessionEvents(
    sessionId: string,
    afterSequence = 0,
    signal?: AbortSignal,
  ): AsyncGenerator<RuntimeStreamChunk, void, unknown> {
    const res = await fetchQuery(
      withShowThinking(
        `/api/sessions/${encodeURIComponent(sessionId)}/events?after_sequence=${afterSequence}&follow=true`,
      ),
      signal,
    );
    yield* readSseStream(res, "Session event stream failed");
  }

  static async listWorkspaces(
    signal?: AbortSignal,
  ): Promise<WorkspaceRegistrySnapshot> {
    const res = await fetchQuery(`/api/workspaces`, signal);
    await expectOk(res, "Failed to load workspaces");
    return res.json();
  }

  static async openWorkspace(path: string): Promise<WorkspaceRegistrySnapshot> {
    const res = await fetch(`/api/workspaces/open`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    await expectOk(res, "Failed to open workspace");
    return res.json();
  }

  static async listNotifications(
    signal?: AbortSignal,
  ): Promise<RuntimeNotification[]> {
    const res = await fetchQuery(`/api/notifications`, signal);
    await expectOk(res, "Failed to load notifications");
    return res.json();
  }

  static async ackNotification(
    notificationId: string,
  ): Promise<RuntimeNotification> {
    const res = await fetch(
      `/api/notifications/${encodeURIComponent(notificationId)}/ack`,
      { method: "POST" },
    );
    await expectOk(res, "Failed to acknowledge notification");
    return res.json();
  }

  static async listSessions(
    signal?: AbortSignal,
  ): Promise<StoredSessionSummary[]> {
    const res = await fetchQuery(`/api/sessions`, signal);
    await expectOk(res, "Failed to list sessions");
    return res.json();
  }

  static async listProviders(signal?: AbortSignal): Promise<ProviderSummary[]> {
    const res = await fetchQuery(`/api/providers`, signal);
    await expectOk(res, "Failed to load providers");
    return res.json();
  }

  static async listProviderModels(
    providerName: string,
    signal?: AbortSignal,
  ): Promise<ProviderModelsResult> {
    const res = await fetchQuery(
      `/api/providers/${encodeURIComponent(providerName)}/models`,
      signal,
    );
    if (!res.ok && res.status !== 409) {
      throw new Error(
        (await runtimeErrorPayload(res, "Failed to load provider models"))
          .message,
      );
    }
    return res.json();
  }

  static async validateProviderCredentials(
    providerName: string,
  ): Promise<ProviderValidationResult> {
    const res = await fetch(
      `/api/providers/${encodeURIComponent(providerName)}/validate`,
      { method: "POST" },
    );
    if (!res.ok && res.status !== 409) {
      throw new Error(
        (await runtimeErrorPayload(res, "Failed to validate provider")).message,
      );
    }
    return res.json();
  }

  static async listAgents(signal?: AbortSignal): Promise<AgentSummary[]> {
    const res = await fetchQuery(`/api/agents`, signal);
    await expectOk(res, "Failed to load agents");
    return res.json();
  }

  static async listSkills(signal?: AbortSignal): Promise<SkillSummary[]> {
    const res = await fetchQuery(`/api/skills`, signal);
    await expectOk(res, "Failed to load skills");
    return res.json();
  }

  static async listCommands(signal?: AbortSignal): Promise<CommandSummary[]> {
    const res = await fetchQuery(`/api/commands`, signal);
    await expectOk(res, "Failed to load commands");
    return res.json();
  }

  static async getStatus(signal?: AbortSignal): Promise<RuntimeStatusSnapshot> {
    const res = await fetchQuery(`/api/status`, signal);
    await expectOk(res, "Failed to load status");
    return res.json();
  }

  static async retryMcpConnections(): Promise<RuntimeStatusSnapshot> {
    const res = await fetch(`/api/status/mcp/retry`, {
      method: "POST",
    });
    await expectOk(res, "Failed to retry MCP connections");
    return res.json();
  }

  static async getReview(
    signal?: AbortSignal,
  ): Promise<WorkspaceReviewSnapshot> {
    const res = await fetchQuery(`/api/review`, signal);
    await expectOk(res, "Failed to load review");
    return res.json();
  }

  static async getReviewDiff(
    path: string,
    signal?: AbortSignal,
  ): Promise<ReviewFileDiff> {
    const res = await fetchQuery(
      `/api/review/diff/${encodePathSegments(path)}`,
      signal,
    );
    await expectOk(res, "Failed to load review diff");
    return res.json();
  }

  static async resumeSession(
    sessionId: string,
  ): Promise<RuntimeResumeResponse> {
    const res = await fetch(
      withShowThinking(`/api/sessions/${encodeURIComponent(sessionId)}/resume`),
      { method: "POST" },
    );
    await expectOk(res, "Failed to resume session");
    return res.json();
  }

  static async getSessionReplay(
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<RuntimeResponse> {
    const res = await fetchQuery(
      withShowThinking(`/api/sessions/${encodeURIComponent(sessionId)}`),
      signal,
    );
    await expectOk(res, "Failed to replay session");
    return res.json();
  }

  static async getSessionDebug(
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<RuntimeSessionDebugSnapshot> {
    const res = await fetchQuery(
      withShowThinking(`/api/sessions/${encodeURIComponent(sessionId)}/debug`),
      signal,
    );
    await expectOk(res, "Failed to load session debug");
    return res.json();
  }

  static async steerSession(
    sessionId: string,
    content: string,
  ): Promise<{ session_id: string; queued: number }> {
    const res = await fetch(
      withShowThinking(`/api/sessions/${encodeURIComponent(sessionId)}/steer`),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      },
    );
    await expectOk(res, "Failed to queue message");
    return res.json();
  }

  static async cancelSession(
    sessionId: string,
    runId: string,
    reason = "web user interrupt",
  ): Promise<RuntimeInterruptResult> {
    const res = await fetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/cancel`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason, run_id: runId }),
      },
    );
    await expectOk(res, "Failed to interrupt session");
    return res.json();
  }

  static async resolveApproval(
    sessionId: string,
    requestId: string,
    decision: ApprovalDecision,
  ): Promise<RuntimeResponse> {
    const res = await fetch(
      withShowThinking(
        `/api/sessions/${encodeURIComponent(sessionId)}/approval`,
      ),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId, decision }),
      },
    );

    await expectOk(res, "Failed to resolve approval");
    return res.json();
  }

  static async answerQuestion(
    sessionId: string,
    requestId: string,
    responses: QuestionAnswer[],
  ): Promise<RuntimeResponse> {
    const res = await fetch(
      withShowThinking(
        `/api/sessions/${encodeURIComponent(sessionId)}/question`,
      ),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId, responses }),
      },
    );

    await expectOk(res, "Failed to answer question");
    return res.json();
  }

  static async listBackgroundTasks(
    signal?: AbortSignal,
  ): Promise<BackgroundTaskSummary[]> {
    const res = await fetchQuery(`/api/tasks`, signal);
    await expectOk(res, "Failed to load background tasks");
    return res.json();
  }

  static async listSessionBackgroundTasks(
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<BackgroundTaskSummary[]> {
    const res = await fetchQuery(
      `/api/sessions/${encodeURIComponent(sessionId)}/tasks`,
      signal,
    );
    await expectOk(res, "Failed to load session background tasks");
    return res.json();
  }

  static async getBackgroundTaskOutput(
    taskId: string,
    signal?: AbortSignal,
  ): Promise<BackgroundTaskOutput> {
    const res = await fetchQuery(
      withShowThinking(`/api/tasks/${encodeURIComponent(taskId)}/output`),
      signal,
    );
    await expectOk(res, "Failed to load background task output");
    return res.json();
  }

  static async cancelBackgroundTask(
    taskId: string,
  ): Promise<BackgroundTaskState> {
    const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: "POST",
    });
    await expectOk(res, "Failed to cancel background task");
    return res.json();
  }

  static async retryBackgroundTask(
    taskId: string,
  ): Promise<BackgroundTaskRetryResponse> {
    const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/retry`, {
      method: "POST",
    });
    await expectOk(res, "Failed to retry background task");
    return res.json();
  }

  static async steerBackgroundTask(
    taskId: string,
    prompt: string,
  ): Promise<BackgroundTaskSteerResponse> {
    const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/steer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    await expectOk(res, "Failed to steer background task");
    return res.json();
  }

  static async getChildSessionContext(
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<ChildSessionContextResult> {
    const res = await fetchQuery(
      withShowThinking(
        `/api/sessions/${encodeURIComponent(sessionId)}/delegated-context`,
      ),
      signal,
    );
    await expectOk(res, "Failed to load delegated child session context");
    return res.json();
  }

  static async getSettings(signal?: AbortSignal): Promise<RuntimeSettings> {
    const res = await fetchQuery(`/api/settings`, signal);
    await expectOk(res, "Failed to load settings");
    return res.json();
  }

  static async updateSettings(
    settings: RuntimeSettingsUpdate,
  ): Promise<RuntimeSettings> {
    const res = await fetch(`/api/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
    await expectOk(res, "Failed to save settings");
    return res.json();
  }

  static async *runStream(
    request: RuntimeRequest,
    signal?: AbortSignal,
  ): AsyncGenerator<RuntimeStreamChunk, void, unknown> {
    const res = await fetch(withShowThinking(`/api/runtime/run/stream`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
      signal,
    });

    yield* readSseStream(res, "Stream request failed");
  }
}
