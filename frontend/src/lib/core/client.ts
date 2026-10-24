/**
 * Browser HTTP and SSE client for the VoidCode core transport API.
 */

import type {
  CommandDescriptor,
  CommandResult,
  HealthBody,
  CoreResult,
  PluginViewDescriptor,
  SessionRecord,
  SessionUsageReport,
  ShowBody,
  SseEventFrame,
} from "./types";

export class CoreApiError extends Error {
  statusCode: number;
  code?: string;

  constructor(message: string, statusCode: number = 0, code?: string) {
    super(message);
    this.name = "CoreApiError";
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

export class CoreClient {
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
      throw new CoreApiError(errMsg, resp.status, errCode);
    }

    return (await resp.json()) as T;
  }

  async health(): Promise<HealthBody> {
    return await this.request<HealthBody>("GET", "/api/health");
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
  ): Promise<CoreResult> {
    return await this.request<CoreResult>(
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
  ): Promise<CoreResult> {
    const body: Record<string, unknown> = { max_steps: maxSteps };
    if (approvalRequestId) {
      body.approval_request_id = approvalRequestId;
    }
    if (decision) {
      body.decision = decision;
    }
    return await this.request<CoreResult>(
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
  ): Promise<CommandResult> {
    return await this.request<CommandResult>(
      "POST",
      `/api/sessions/${encodeURIComponent(sessionId)}/invoke/${encodeURIComponent(name)}`,
      {
        body: {
          arguments: args,
          // The revision the client displayed; a stale one is refused visibly.
          expected_revision: expectedRevision,
        },
      },
    );
  }

  async confirm(
    sessionId: string,
    requestId: string,
    decision: "allow" | "deny",
    expectedRevision: string,
  ): Promise<CommandResult> {
    return await this.request<CommandResult>(
      "POST",
      `/api/sessions/${encodeURIComponent(sessionId)}/confirm`,
      {
        body: {
          request_id: requestId,
          decision,
          expected_revision: expectedRevision,
        },
      },
    );
  }

  async views(sessionId: string): Promise<ViewsSnapshot> {
    const res = await this.request<{
      revision_id: string;
      views: PluginViewDescriptor[];
    }>("GET", `/api/sessions/${encodeURIComponent(sessionId)}/views`);
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
   * A `server_restart` frame ends the current connection: its cursor is a
   * watermark of what THAT stream delivered, so the reconnect resumes from the
   * client's own applied cursor and the frame is also delivered to `onFrame`.
   * `onReconnect` fires once per (re)connection that yields its first frame,
   * so the caller reconciles on every established stream — announced restart
   * or silent drop — not just explicit restart frames.
   * Returns a cleanup cancellation function.
   */
  streamEvents(
    sessionId: string,
    afterSequence: number,
    onFrame: (frame: SseEventFrame) => void,
    onError?: (err: Error) => void,
    onReconnect?: () => void,
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

          if (resp.status === 503) {
            // Fenced child or handoff gap: the supervisor answers once a child
            // is elected; retry on the same applied cursor, never skip ahead.
            throw new Error("core is draining for an upgrade; retrying");
          }
          if (!resp.ok) {
            throw new Error(`SSE stream failed: HTTP ${resp.status}`);
          }

          const reader = resp.body?.getReader();
          if (!reader) {
            throw new Error("No readable stream available on SSE response");
          }

          try {
            const decoder = new TextDecoder();
            let buffer = "";
            let restartRequested = false;
            let firstFrameSeen = false;
            while (!isCancelled && !restartRequested) {
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
                  if (!line.startsWith("data:")) continue;
                  const jsonStr = line.slice(5).trim();
                  if (!jsonStr) continue;
                  let frame: SseEventFrame;
                  try {
                    frame = JSON.parse(jsonStr) as SseEventFrame;
                  } catch {
                    // A malformed frame fails the connection visibly: skipping
                    // fact N and applying N+1 would advance the resume cursor
                    // past a missed fact. Reconnect from the own applied cursor.
                    throw new Error("SSE stream carried a malformed frame");
                  }
                  if (!firstFrameSeen) {
                    firstFrameSeen = true;
                    if (onReconnect) onReconnect();
                  }
                  if (frame.kind === "server_restart") {
                    // Watermark only: the loop below reconnects from the
                    // client's own applied cursor, which never jumps over
                    // a row this client did not apply.
                    onFrame(frame);
                    restartRequested = true;
                    break;
                  }
                  onFrame(frame);
                  // The resume cursor advances only after the frame was
                  // applied: a callback failure below must not skip facts.
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
                }
                if (restartRequested) break;
              }
            }
          } finally {
            // Callback/parse/read failures all pass through here: the reader
            // is always released, and cancel() is awaited so its rejection
            // (already-closed stream) never escapes as an unhandled rejection.
            try {
              await reader.cancel();
            } catch {
              // already closed or errored; the reconnect below still runs
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
