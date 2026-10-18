/**
 * Browser HTTP and SSE client for the VoidCode kernel transport API.
 */

import type {
  CommandDescriptor,
  CommandResult,
  KernelResult,
  PluginViewDescriptor,
  SessionRecord,
  SessionUsageReport,
  ShowBody,
  SseEventFrame,
} from "./types";

export class KernelApiError extends Error {
  statusCode: number;
  code?: string;

  constructor(message: string, statusCode: number = 0, code?: string) {
    super(message);
    this.name = "KernelApiError";
    this.statusCode = statusCode;
    this.code = code;
  }
}

/** The catalog and the capability revision it was captured under. */
export interface CommandCatalog {
  revisionId: string;
  commands: CommandDescriptor[];
}

/** The rendered views and the revision of the snapshot that rendered them. */
export interface ViewsSnapshot {
  revisionId: string;
  views: PluginViewDescriptor[];
}

export class KernelClient {
  private baseUrl: string;
  private token?: string;

  constructor(baseUrl: string = "", token?: string) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.token = token;
  }

  private async request<T>(
    method: string,
    path: string,
    options: {
      body?: unknown;
      query?: Record<string, string | number | boolean | undefined>;
    } = {},
  ): Promise<T> {
    let url = `${this.baseUrl}${path}`;
    if (options.query) {
      const searchParams = new URLSearchParams();
      for (const [k, v] of Object.entries(options.query)) {
        if (v !== undefined) {
          searchParams.set(k, String(v));
        }
      }
      const qs = searchParams.toString();
      if (qs) {
        url = `${url}?${qs}`;
      }
    }

    const headers: Record<string, string> = {
      Accept: "application/json",
    };
    if (this.token) {
      headers.Authorization = `Bearer ${this.token}`;
    }

    let bodyData: string | undefined;
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      bodyData = JSON.stringify(options.body);
    }

    const resp = await fetch(url, {
      method,
      headers,
      body: bodyData,
    });

    if (!resp.ok) {
      let errMsg = `HTTP ${resp.status} ${resp.statusText}`;
      let errCode: string | undefined;
      try {
        const errJson = await resp.json();
        if (errJson && typeof errJson === "object") {
          if (typeof errJson.error === "string") {
            errMsg = errJson.error;
          }
          if (typeof errJson.code === "string") {
            errCode = errJson.code;
          }
        }
      } catch {
        // use default errMsg
      }
      throw new KernelApiError(errMsg, resp.status, errCode);
    }

    return (await resp.json()) as T;
  }

  async commandCatalog(): Promise<CommandCatalog> {
    const res = await this.request<{
      revision_id: string;
      commands: CommandDescriptor[];
    }>("GET", "/api/commands");
    return { revisionId: res.revision_id, commands: res.commands || [] };
  }

  async listSessions(limit: number = 100): Promise<SessionRecord[]> {
    const res = await this.request<{ sessions: SessionRecord[] }>(
      "GET",
      "/api/sessions",
      {
        query: { limit },
      },
    );
    return res.sessions || [];
  }

  async createSession(sessionId?: string): Promise<SessionRecord> {
    return await this.request<SessionRecord>("POST", "/api/sessions", {
      body: sessionId ? { session_id: sessionId } : {},
    });
  }

  async getSession(sessionId: string): Promise<SessionRecord> {
    return await this.request<SessionRecord>(
      "GET",
      `/api/sessions/${encodeURIComponent(sessionId)}`,
    );
  }

  async show(sessionId: string, events: boolean = false): Promise<ShowBody> {
    return await this.request<ShowBody>(
      "GET",
      `/api/sessions/${encodeURIComponent(sessionId)}/show`,
      { query: { events } },
    );
  }

  async run(
    sessionId: string,
    prompt: string,
    maxSteps: number = 32,
  ): Promise<KernelResult> {
    return await this.request<KernelResult>(
      "POST",
      `/api/sessions/${encodeURIComponent(sessionId)}/run`,
      {
        body: { prompt, session_id: sessionId, max_steps: maxSteps },
      },
    );
  }

  async resume(
    sessionId: string,
    approvalRequestId?: string,
    decision?: "allow" | "deny",
    maxSteps: number = 32,
  ): Promise<KernelResult> {
    const body: Record<string, unknown> = { max_steps: maxSteps };
    if (approvalRequestId) {
      body.approval_request_id = approvalRequestId;
    }
    if (decision) {
      body.decision = decision;
    }
    return await this.request<KernelResult>(
      "POST",
      `/api/sessions/${encodeURIComponent(sessionId)}/resume`,
      { body },
    );
  }

  async invoke(
    sessionId: string,
    name: string,
    args: Record<string, unknown>,
    expectedRevision: string,
    clientCapabilities: string[] = [],
  ): Promise<CommandResult> {
    return await this.request<CommandResult>(
      "POST",
      `/api/sessions/${encodeURIComponent(sessionId)}/invoke/${encodeURIComponent(name)}`,
      {
        body: {
          arguments: args,
          // The revision the client displayed; a stale one is refused visibly.
          expected_revision: expectedRevision,
          client_capabilities: clientCapabilities,
        },
      },
    );
  }

  async confirm(
    sessionId: string,
    requestId: string,
    decision: "allow" | "deny",
    expectedRevision: string,
    clientCapabilities: string[] = [],
  ): Promise<CommandResult> {
    return await this.request<CommandResult>(
      "POST",
      `/api/sessions/${encodeURIComponent(sessionId)}/confirm`,
      {
        body: {
          request_id: requestId,
          decision,
          expected_revision: expectedRevision,
          client_capabilities: clientCapabilities,
        },
      },
    );
  }

  async views(
    sessionId: string,
    clientCapabilities: string[] = [],
  ): Promise<ViewsSnapshot> {
    const res = await this.request<{
      revision_id: string;
      views: PluginViewDescriptor[];
    }>("GET", `/api/sessions/${encodeURIComponent(sessionId)}/views`, {
      query: {
        client_capabilities: clientCapabilities.join(","),
      },
    });
    return { revisionId: res.revision_id, views: res.views || [] };
  }

  async usage(
    sessionId: string,
    sinceSequence: number = 0,
    untilSequence?: number,
  ): Promise<SessionUsageReport> {
    return await this.request<SessionUsageReport>(
      "GET",
      `/api/sessions/${encodeURIComponent(sessionId)}/usage`,
      {
        query: {
          since_sequence: sinceSequence,
          until_sequence: untilSequence,
        },
      },
    );
  }

  async cancel(
    sessionId: string,
  ): Promise<{ cancellation_requested: boolean; session_id: string }> {
    return await this.request<{
      cancellation_requested: boolean;
      session_id: string;
    }>("POST", `/api/sessions/${encodeURIComponent(sessionId)}/cancel`);
  }

  /**
   * Connect to the SSE event stream for a session.
   * Uses monotonic event cursors and automatically reconnects upon stream end/error.
   * Returns a cleanup cancellation function.
   */
  streamEvents(
    sessionId: string,
    afterSequence: number,
    onFrame: (frame: SseEventFrame) => void,
    onError?: (err: Error) => void,
  ): () => void {
    let cursor = afterSequence;
    const abortController = new AbortController();
    let isCancelled = false;

    const connect = async () => {
      while (!isCancelled) {
        try {
          const url = `${this.baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/events?after_sequence=${cursor}&follow=true`;
          const headers: Record<string, string> = {
            Accept: "text/event-stream",
          };
          if (this.token) {
            headers.Authorization = `Bearer ${this.token}`;
          }

          const resp = await fetch(url, {
            headers,
            signal: abortController.signal,
          });

          if (!resp.ok) {
            throw new Error(`SSE stream failed: HTTP ${resp.status}`);
          }

          const reader = resp.body?.getReader();
          if (!reader) {
            throw new Error("No readable stream available on SSE response");
          }

          const decoder = new TextDecoder();
          let buffer = "";

          while (!isCancelled) {
            const { done, value } = await reader.read();
            if (done) {
              break;
            }
            buffer += decoder.decode(value, { stream: true });
            const parts = buffer.split("\n\n");
            buffer = parts.pop() || "";

            for (const part of parts) {
              const lines = part.trim().split("\n");
              for (const line of lines) {
                if (line.startsWith("data:")) {
                  const jsonStr = line.slice(5).trim();
                  if (jsonStr) {
                    try {
                      const frame = JSON.parse(jsonStr) as SseEventFrame;
                      if (frame.events && Array.isArray(frame.events)) {
                        for (const ev of frame.events) {
                          if (ev.sequence > cursor) {
                            cursor = ev.sequence;
                          }
                        }
                      } else if (frame.event?.sequence) {
                        if (frame.event.sequence > cursor) {
                          cursor = frame.event.sequence;
                        }
                      }
                      onFrame(frame);
                    } catch {
                      // ignore parse errors
                    }
                  }
                }
              }
            }
          }
        } catch (err: unknown) {
          if (isCancelled) {
            break;
          }
          if (onError && err instanceof Error) {
            onError(err);
          }
          // Backoff before reconnect
          const { promise, resolve } = Promise.withResolvers<void>();
          setTimeout(resolve, 1000);
          await promise;
        }
      }
    };

    void connect();

    return () => {
      isCancelled = true;
      abortController.abort();
    };
  }
}
