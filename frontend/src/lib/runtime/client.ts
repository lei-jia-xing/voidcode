import {
  AgentSummary,
  BackgroundTaskOutput,
  BackgroundTaskState,
  BackgroundTaskSummary,
  RuntimeRequest,
  StoredSessionSummary,
  RuntimeResponse,
  RuntimeStreamChunk,
  ApprovalDecision,
  QuestionAnswer,
  ProviderModelsResult,
  ProviderSummary,
  ProviderValidationResult,
  RuntimeNotification,
  RuntimeSessionDebugSnapshot,
  RuntimeSessionResult,
  RuntimeSettings,
  RuntimeSettingsUpdate,
  RuntimeStatusSnapshot,
  ReviewFileDiff,
  WorkspaceRegistrySnapshot,
  WorkspaceReviewSnapshot,
} from "./types";

async function runtimeErrorMessage(
  res: Response,
  fallback: string,
): Promise<string> {
  let payload: unknown;
  try {
    payload = await res.clone().json();
  } catch {
    return `${fallback}: ${res.statusText || res.status}`;
  }

  if (payload && typeof payload === "object") {
    const error = (payload as { error?: unknown }).error;
    const code = (payload as { code?: unknown }).code;
    if (typeof error === "string" && error.length > 0) {
      return typeof code === "string" && code.length > 0
        ? `${fallback}: ${error} (${code})`
        : `${fallback}: ${error}`;
    }
  }

  return `${fallback}: ${res.statusText || res.status}`;
}

async function expectOk(res: Response, fallback: string): Promise<void> {
  if (!res.ok) {
    throw new Error(await runtimeErrorMessage(res, fallback));
  }
}

export class RuntimeClient {
  static async listWorkspaces(): Promise<WorkspaceRegistrySnapshot> {
    const res = await fetch(`/api/workspaces`);
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

  static async listSessions(): Promise<StoredSessionSummary[]> {
    const res = await fetch(`/api/sessions`);
    await expectOk(res, "Failed to list sessions");
    return res.json();
  }

  static async listProviders(): Promise<ProviderSummary[]> {
    const res = await fetch(`/api/providers`);
    await expectOk(res, "Failed to load providers");
    return res.json();
  }

  static async listProviderModels(
    providerName: string,
  ): Promise<ProviderModelsResult> {
    const res = await fetch(
      `/api/providers/${encodeURIComponent(providerName)}/models`,
    );
    if (!res.ok && res.status !== 409) {
      throw new Error(
        await runtimeErrorMessage(res, "Failed to load provider models"),
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
        await runtimeErrorMessage(res, "Failed to validate provider"),
      );
    }
    return res.json();
  }

  static async listAgents(): Promise<AgentSummary[]> {
    const res = await fetch(`/api/agents`);
    await expectOk(res, "Failed to load agents");
    return res.json();
  }

  static async getStatus(): Promise<RuntimeStatusSnapshot> {
    const res = await fetch(`/api/status`);
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

  static async getReview(): Promise<WorkspaceReviewSnapshot> {
    const res = await fetch(`/api/review`);
    await expectOk(res, "Failed to load review");
    return res.json();
  }

  static async getReviewDiff(path: string): Promise<ReviewFileDiff> {
    const res = await fetch(`/api/review/diff/${encodeURIComponent(path)}`);
    await expectOk(res, "Failed to load review diff");
    return res.json();
  }

  static async getSessionReplay(sessionId: string): Promise<RuntimeResponse> {
    const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
    await expectOk(res, "Failed to replay session");
    return res.json();
  }

  static async getSessionResult(
    sessionId: string,
  ): Promise<RuntimeSessionResult> {
    const res = await fetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/result`,
    );
    await expectOk(res, "Failed to load session result");
    return res.json();
  }

  static async getSessionDebug(
    sessionId: string,
  ): Promise<RuntimeSessionDebugSnapshot> {
    const res = await fetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/debug`,
    );
    await expectOk(res, "Failed to load session debug");
    return res.json();
  }

  static async resolveApproval(
    sessionId: string,
    requestId: string,
    decision: ApprovalDecision,
  ): Promise<RuntimeResponse> {
    const res = await fetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/approval`,
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
      `/api/sessions/${encodeURIComponent(sessionId)}/question`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId, responses }),
      },
    );

    await expectOk(res, "Failed to answer question");
    return res.json();
  }

  static async listNotifications(): Promise<RuntimeNotification[]> {
    const res = await fetch(`/api/notifications`);
    await expectOk(res, "Failed to load notifications");
    return res.json();
  }

  static async acknowledgeNotification(
    notificationId: string,
  ): Promise<RuntimeNotification> {
    const res = await fetch(
      `/api/notifications/${encodeURIComponent(notificationId)}/ack`,
      { method: "POST" },
    );
    await expectOk(res, "Failed to acknowledge notification");
    return res.json();
  }

  static async listBackgroundTasks(): Promise<BackgroundTaskSummary[]> {
    const res = await fetch(`/api/tasks`);
    await expectOk(res, "Failed to load background tasks");
    return res.json();
  }

  static async listSessionBackgroundTasks(
    sessionId: string,
  ): Promise<BackgroundTaskSummary[]> {
    const res = await fetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/tasks`,
    );
    await expectOk(res, "Failed to load session background tasks");
    return res.json();
  }

  static async getBackgroundTask(taskId: string): Promise<BackgroundTaskState> {
    const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`);
    await expectOk(res, "Failed to load background task");
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

  static async getBackgroundTaskOutput(
    taskId: string,
  ): Promise<BackgroundTaskOutput> {
    const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/output`);
    await expectOk(res, "Failed to load background task output");
    return res.json();
  }

  static async getSettings(): Promise<RuntimeSettings> {
    const res = await fetch(`/api/settings`);
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
  ): AsyncGenerator<RuntimeStreamChunk, void, unknown> {
    const res = await fetch(`/api/runtime/run/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });

    await expectOk(res, "Stream request failed");
    if (!res.body) throw new Error("No response body for stream");

    const reader = res.body.getReader();
    const decoder = new TextDecoder();

    let buffer = "";
    let dataLines: string[] = [];

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let eolIndex = buffer.indexOf("\n");
      while (eolIndex >= 0) {
        const line = buffer.slice(0, eolIndex);
        buffer = buffer.slice(eolIndex + 1);
        const trimmedLine = line.replace(/\r$/, "");

        if (trimmedLine === "") {
          // Empty line indicates end of an SSE event
          if (dataLines.length > 0) {
            try {
              const chunk = JSON.parse(
                dataLines.join("\n"),
              ) as RuntimeStreamChunk;
              yield chunk;
            } catch (e) {
              console.warn(
                "Failed to parse SSE data chunk:",
                dataLines.join("\n"),
                e,
              );
            }
            dataLines = [];
          }
        } else if (trimmedLine.startsWith("data:")) {
          const data = trimmedLine.slice(5).replace(/^ /, "");
          dataLines.push(data);
        }

        eolIndex = buffer.indexOf("\n");
      }
    }

    buffer += decoder.decode();

    let eolIndex = buffer.indexOf("\n");
    while (eolIndex >= 0) {
      const line = buffer.slice(0, eolIndex);
      buffer = buffer.slice(eolIndex + 1);
      const trimmedLine = line.replace(/\r$/, "");

      if (trimmedLine === "") {
        if (dataLines.length > 0) {
          try {
            const chunk = JSON.parse(
              dataLines.join("\n"),
            ) as RuntimeStreamChunk;
            yield chunk;
          } catch (e) {
            console.warn(
              "Failed to parse SSE data chunk:",
              dataLines.join("\n"),
              e,
            );
          }
          dataLines = [];
        }
      } else if (trimmedLine.startsWith("data:")) {
        const data = trimmedLine.slice(5).replace(/^ /, "");
        dataLines.push(data);
      }

      eolIndex = buffer.indexOf("\n");
    }

    if (buffer.length > 0) {
      const trimmedLine = buffer.replace(/\r$/, "");
      if (trimmedLine.startsWith("data:")) {
        dataLines.push(trimmedLine.slice(5).replace(/^ /, ""));
      }
      buffer = "";
    }

    // Process any remaining buffered data after stream closes
    if (dataLines.length > 0) {
      try {
        const chunk = JSON.parse(dataLines.join("\n")) as RuntimeStreamChunk;
        yield chunk;
      } catch (e) {
        console.warn(
          "Failed to parse trailing SSE data chunk:",
          dataLines.join("\n"),
          e,
        );
      }
    }
  }
}
