import {
  AgentSummary,
  RuntimeRequest,
  StoredSessionSummary,
  RuntimeResponse,
  RuntimeStreamChunk,
  ApprovalDecision,
  ProviderModelsResult,
  ProviderSummary,
  RuntimeSettings,
  RuntimeSettingsUpdate,
  RuntimeStatusSnapshot,
  ReviewFileDiff,
  WorkspaceRegistrySnapshot,
  WorkspaceReviewSnapshot,
} from "./types";

export class RuntimeClient {
  static async listWorkspaces(): Promise<WorkspaceRegistrySnapshot> {
    const res = await fetch(`/api/workspaces`);
    if (!res.ok)
      throw new Error(`Failed to load workspaces: ${res.statusText}`);
    return res.json();
  }

  static async openWorkspace(path: string): Promise<WorkspaceRegistrySnapshot> {
    const res = await fetch(`/api/workspaces/open`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (!res.ok) throw new Error(`Failed to open workspace: ${res.statusText}`);
    return res.json();
  }

  static async listSessions(): Promise<StoredSessionSummary[]> {
    const res = await fetch(`/api/sessions`);
    if (!res.ok) throw new Error(`Failed to list sessions: ${res.statusText}`);
    return res.json();
  }

  static async listProviders(): Promise<ProviderSummary[]> {
    const res = await fetch(`/api/providers`);
    if (!res.ok) throw new Error(`Failed to load providers: ${res.statusText}`);
    return res.json();
  }

  static async listProviderModels(
    providerName: string,
  ): Promise<ProviderModelsResult> {
    const res = await fetch(
      `/api/providers/${encodeURIComponent(providerName)}/models`,
    );
    if (!res.ok && res.status !== 409) {
      throw new Error(`Failed to load provider models: ${res.statusText}`);
    }
    return res.json();
  }

  static async listAgents(): Promise<AgentSummary[]> {
    const res = await fetch(`/api/agents`);
    if (!res.ok) throw new Error(`Failed to load agents: ${res.statusText}`);
    return res.json();
  }

  static async getStatus(): Promise<RuntimeStatusSnapshot> {
    const res = await fetch(`/api/status`);
    if (!res.ok) throw new Error(`Failed to load status: ${res.statusText}`);
    return res.json();
  }

  static async retryMcpConnections(): Promise<RuntimeStatusSnapshot> {
    const res = await fetch(`/api/status/mcp/retry`, {
      method: "POST",
    });
    if (!res.ok)
      throw new Error(`Failed to retry MCP connections: ${res.statusText}`);
    return res.json();
  }

  static async getReview(): Promise<WorkspaceReviewSnapshot> {
    const res = await fetch(`/api/review`);
    if (!res.ok) throw new Error(`Failed to load review: ${res.statusText}`);
    return res.json();
  }

  static async getReviewDiff(path: string): Promise<ReviewFileDiff> {
    const res = await fetch(`/api/review/diff/${encodeURIComponent(path)}`);
    if (!res.ok)
      throw new Error(`Failed to load review diff: ${res.statusText}`);
    return res.json();
  }

  static async getSessionReplay(sessionId: string): Promise<RuntimeResponse> {
    const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (!res.ok) throw new Error(`Failed to replay session: ${res.statusText}`);
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

    if (!res.ok)
      throw new Error(`Failed to resolve approval: ${res.statusText}`);
    return res.json();
  }

  static async getSettings(): Promise<RuntimeSettings> {
    const res = await fetch(`/api/settings`);
    if (!res.ok) throw new Error(`Failed to load settings: ${res.statusText}`);
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
    if (!res.ok) throw new Error(`Failed to save settings: ${res.statusText}`);
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

    if (!res.ok) throw new Error(`Stream request failed: ${res.statusText}`);
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
