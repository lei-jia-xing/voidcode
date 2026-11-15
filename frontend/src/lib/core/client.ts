/** Multiplexed JSON-RPC over WebSocket; HTTP serves health only. */
import type { RpcMethods } from "./generated/api";
import type {
  ArtifactPage,
  CommandDescriptor,
  CommandResult,
  HealthStatus,
  RunResult,
  PluginViewDescriptor,
  SessionRow,
  SessionUsageReport,
  ShowBody,
  SessionFrame,
} from "./types";

export class CoreApiError extends Error {
  constructor(
    message: string,
    public statusCode = 0,
    public code?: string,
  ) {
    super(message);
    this.name = "CoreApiError";
  }
}

export interface CommandCatalog {
  revisionId: string;
  commands: CommandDescriptor[];
}
export interface ViewsSnapshot {
  revisionId: string;
  views: PluginViewDescriptor[];
}

type Pending = {
  resolve: (result: unknown) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
};
type Subscription = {
  frame: (frame: SessionFrame) => void;
  end: (error?: Error) => void;
};
const MAX_MESSAGE = 4 * 1024 * 1024;
const MAX_BUFFER = 8 * 1024 * 1024;
const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function decodeFrame(value: unknown): SessionFrame {
  if (!object(value)) throw new Error("Malformed session frame");
  const event = (item: unknown) =>
    object(item) &&
    Number.isSafeInteger(item.sequence) &&
    (item.sequence as number) > 0 &&
    typeof item.session_id === "string" &&
    typeof item.type === "string" &&
    object(item.payload);
  if ("events" in value) {
    if (
      !object(value.session) ||
      typeof value.session.session_id !== "string" ||
      !Array.isArray(value.events) ||
      !value.events.every(event)
    ) {
      throw new Error("Malformed session head");
    }
  } else if (
    typeof value.session_id !== "string" ||
    !(value.kind === "event"
      ? event(value.event)
      : value.kind === "error"
        ? typeof value.error === "string"
        : value.kind === "server_restart" && Number.isSafeInteger(value.cursor))
  ) {
    throw new Error("Malformed session notification");
  }
  return value as unknown as SessionFrame;
}

export class CoreClient {
  private socket?: WebSocket;
  private connecting?: Promise<WebSocket>;
  private abortOpening?: (error: Error) => void;
  private pending = new Map<string, Pending>();
  private subscriptions = new Map<string, Subscription>();
  private nextId = 0;

  constructor(
    private baseUrl = "",
    private token?: string,
  ) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  close(): void {
    const socket = this.socket;
    if (socket) {
      this.lost(
        socket,
        new CoreApiError(
          "RPC connection closed; operation outcome may be unknown",
          0,
          "outcome_unknown",
        ),
      );
      socket.close();
    }
  }

  private lost(socket: WebSocket, error: Error): void {
    if (this.socket !== socket) return;
    this.socket = undefined;
    this.connecting = undefined;
    this.abortOpening?.(error);
    this.abortOpening = undefined;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
    for (const subscription of this.subscriptions.values())
      subscription.end(error);
  }

  private connect(): Promise<WebSocket> {
    if (this.socket?.readyState === WebSocket.OPEN && !this.connecting)
      return Promise.resolve(this.socket);
    if (this.connecting) return this.connecting;
    const url = new URL(this.baseUrl || window.location.origin);
    url.protocol =
      url.protocol === "https:" || url.protocol === "wss:" ? "wss:" : "ws:";
    url.pathname = url.pathname.replace(/\/$/, "") + "/api/rpc";
    url.search = "";
    url.hash = "";
    const socket = new WebSocket(url, "voidcode.rpc.v1");
    this.socket = socket;
    this.connecting = new Promise<WebSocket>((resolve, reject) => {
      let authenticated = false;
      const identity = `auth-${++this.nextId}`;
      const timer = setTimeout(() => {
        reject(new Error("RPC authentication timed out"));
        socket.close();
      }, 10000);
      this.abortOpening = (error) => {
        clearTimeout(timer);
        reject(error);
      };
      const fail = (error: Error) => {
        clearTimeout(timer);
        if (!authenticated) reject(error);
        this.lost(socket, error);
      };
      socket.onopen = () =>
        socket.send(
          JSON.stringify({
            jsonrpc: "2.0",
            id: identity,
            method: "connection.authenticate",
            params: { token: this.token ?? null },
          }),
        );
      socket.onclose = () =>
        fail(
          new CoreApiError(
            "RPC connection lost; operation outcome may be unknown. Inspect session history before retrying.",
            0,
            "outcome_unknown",
          ),
        );
      socket.onerror = () => {
        fail(new Error("RPC connection failed"));
        socket.close();
      };
      socket.onmessage = (message) => {
        if (this.socket !== socket) return;
        try {
          if (
            typeof message.data !== "string" ||
            new TextEncoder().encode(message.data).length > MAX_MESSAGE
          )
            throw new Error("Invalid RPC message size or encoding");
          const raw: unknown = JSON.parse(message.data);
          if (!object(raw) || raw.jsonrpc !== "2.0")
            throw new Error("Malformed RPC message");
          if ("method" in raw) {
            if (
              !authenticated ||
              "id" in raw ||
              !object(raw.params) ||
              typeof raw.params.subscription_id !== "string"
            )
              throw new Error("Malformed RPC notification");
            const subscription = this.subscriptions.get(
              raw.params.subscription_id,
            );
            if (raw.method === "session.frame") {
              const frame = decodeFrame(raw.params.frame);
              subscription?.frame(frame);
            } else if (raw.method === "subscription.closed")
              subscription?.end();
            else throw new Error("Unknown RPC notification");
            return;
          }
          if (typeof raw.id !== "string" || "result" in raw === "error" in raw)
            throw new Error("Malformed RPC response");
          let error: CoreApiError | undefined;
          if ("error" in raw) {
            if (
              !object(raw.error) ||
              !Number.isInteger(raw.error.code) ||
              typeof raw.error.message !== "string"
            )
              throw new Error("Malformed RPC error");
            const data = object(raw.error.data) ? raw.error.data : {};
            error = new CoreApiError(
              raw.error.message,
              typeof data.status === "number" ? data.status : 0,
              typeof data.code === "string" ? data.code : undefined,
            );
          }
          if (!authenticated) {
            if (raw.id !== identity)
              throw new Error("Unexpected authentication response");
            if (error) {
              fail(error);
              socket.close();
              return;
            }
            if (!object(raw.result) || raw.result.protocol_version !== 1)
              throw new Error("Unsupported RPC protocol");
            authenticated = true;
            clearTimeout(timer);
            this.connecting = undefined;
            this.abortOpening = undefined;
            resolve(socket);
            return;
          }
          const pending = this.pending.get(raw.id);
          if (pending) {
            this.pending.delete(raw.id);
            clearTimeout(pending.timer);
            if (error) pending.reject(error);
            else pending.resolve(raw.result);
          }
        } catch (error) {
          fail(
            new CoreApiError(
              `Malformed RPC message: ${String(error)}`,
              400,
              "protocol_error",
            ),
          );
          socket.close(1002, "invalid RPC message");
        }
      };
    });
    return this.connecting;
  }

  private async call<M extends keyof RpcMethods>(
    method: M,
    params: RpcMethods[M]["params"],
  ): Promise<RpcMethods[M]["result"]> {
    const socket = await this.connect();
    if (socket.readyState !== WebSocket.OPEN)
      throw new CoreApiError(
        "RPC connection closed before send",
        0,
        "outcome_unknown",
      );
    const id = `call-${++this.nextId}`;
    const text = JSON.stringify({ jsonrpc: "2.0", id, method, params });
    if (
      new TextEncoder().encode(text).length > MAX_MESSAGE ||
      socket.bufferedAmount + text.length > MAX_BUFFER
    )
      throw new Error("RPC outbound buffer limit reached");
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(
          new CoreApiError(
            "RPC response timed out; operation outcome may be unknown",
            0,
            "outcome_unknown",
          ),
        );
      }, 300000);
      this.pending.set(id, {
        resolve: (result) => resolve(result as RpcMethods[M]["result"]),
        reject,
        timer,
      });
      try {
        socket.send(text);
      } catch {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(
          new CoreApiError(
            "RPC send failed; operation outcome may be unknown",
            0,
            "outcome_unknown",
          ),
        );
      }
    });
  }

  async health(): Promise<HealthStatus> {
    const response = await fetch(`${this.baseUrl}/api/health`, {
      headers: this.token ? { Authorization: `Bearer ${this.token}` } : {},
    });
    if (!response.ok)
      throw new CoreApiError(
        `Health check failed: HTTP ${response.status}`,
        response.status,
      );
    return response.json() as Promise<HealthStatus>;
  }
  async commandCatalog(): Promise<CommandCatalog> {
    const result = await this.call("command.catalog", {});
    return { revisionId: result.revision_id, commands: result.commands };
  }
  async listSessions(limit = 100): Promise<SessionRow[]> {
    return (await this.call("session.list", { limit })).sessions;
  }
  async createSession(sessionId?: string): Promise<SessionRow> {
    return this.call("session.create", { session_id: sessionId });
  }
  async getSession(sessionId: string): Promise<SessionRow> {
    return this.call("session.get", { session_id: sessionId });
  }
  async show(sessionId: string, events = false): Promise<ShowBody> {
    return this.call("session.show", { session_id: sessionId, events });
  }
  async readArtifact(
    sha256: string,
    { offset = 0, limit = 65536 }: { offset?: number; limit?: number } = {},
  ): Promise<ArtifactPage> {
    const page = await this.call("artifact.read", { sha256, offset, limit });
    try {
      if (
        !object(page) ||
        !object(page.artifact) ||
        page.artifact.sha256 !== sha256 ||
        !/^[0-9a-f]{64}$/.test(sha256) ||
        !Number.isSafeInteger(page.artifact.bytes) ||
        page.artifact.bytes < 0 ||
        typeof page.artifact.media_type !== "string" ||
        page.offset !== offset ||
        typeof page.eof !== "boolean" ||
        typeof page.data !== "string" ||
        !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(
          page.data,
        )
      )
        throw new Error("Invalid artifact page metadata/base64");
      const binary = atob(page.data);
      const data = Uint8Array.from(binary, (character) =>
        character.charCodeAt(0),
      );
      const end = page.offset + data.byteLength;
      if (
        data.byteLength > limit ||
        (data.byteLength > 0 && end > page.artifact.bytes) ||
        (page.offset < page.artifact.bytes && data.byteLength === 0) ||
        page.eof !== end >= page.artifact.bytes
      )
        throw new Error("Invalid artifact page byte progress/EOF");
      return {
        ...page,
        artifact: { ...page.artifact, media_type: page.artifact.media_type },
        data,
      };
    } catch {
      throw new CoreApiError(
        "Malformed artifact page response",
        502,
        "protocol_error",
      );
    }
  }
  async run(
    sessionId: string,
    prompt: string,
    maxSteps?: number,
    rootActionBudget?: number,
  ): Promise<RunResult> {
    return this.call("session.run", {
      session_id: sessionId,
      prompt,
      ...(maxSteps === undefined ? {} : { max_steps: maxSteps }),
      ...(rootActionBudget === undefined
        ? {}
        : { root_action_budget: rootActionBudget }),
    });
  }
  async resume(
    sessionId: string,
    approvalRequestId?: string,
    decision?: "allow" | "deny",
    maxSteps?: number,
    extendRootActions?: number,
    rootActionGrantId?: string,
  ): Promise<RunResult> {
    return this.call("session.resume", {
      session_id: sessionId,
      ...(approvalRequestId === undefined
        ? {}
        : { approval_request_id: approvalRequestId }),
      ...(decision === undefined ? {} : { decision }),
      ...(maxSteps === undefined ? {} : { max_steps: maxSteps }),
      ...(extendRootActions === undefined
        ? {}
        : { extend_root_actions: extendRootActions }),
      ...(rootActionGrantId === undefined
        ? {}
        : { root_action_grant_id: rootActionGrantId }),
    });
  }
  async commandInput(
    sessionId: string,
    name: string,
    expectedRevision: string,
    args: Record<string, unknown> = {},
  ) {
    return this.call("command.input", {
      session_id: sessionId,
      name,
      arguments: args,
      expected_revision: expectedRevision,
    });
  }
  async invoke(
    sessionId: string,
    name: string,
    args: Record<string, unknown>,
    expectedRevision: string,
  ): Promise<CommandResult> {
    return this.call("command.invoke", {
      session_id: sessionId,
      name,
      arguments: args,
      expected_revision: expectedRevision,
    });
  }
  async confirm(
    sessionId: string,
    requestId: string,
    decision: "allow" | "deny",
    expectedRevision: string,
  ): Promise<CommandResult> {
    return this.call("action.confirm", {
      session_id: sessionId,
      request_id: requestId,
      decision,
      expected_revision: expectedRevision,
    });
  }
  async views(sessionId: string): Promise<ViewsSnapshot> {
    const result = await this.call("session.views", { session_id: sessionId });
    return { revisionId: result.revision_id, views: result.views };
  }
  async usage(
    sessionId: string,
    sinceSequence = 0,
    untilSequence?: number,
  ): Promise<SessionUsageReport> {
    return this.call("session.usage", {
      session_id: sessionId,
      since_sequence: sinceSequence,
      until_sequence: untilSequence,
    });
  }
  async cancel(
    sessionId: string,
  ): Promise<{ cancellation_requested: boolean; session_id: string }> {
    return this.call("session.cancel", { session_id: sessionId });
  }

  /** Only subscriptions reconnect automatically; mutations never do. */
  streamEvents(
    sessionId: string,
    afterSequence: number,
    onFrame: (frame: SessionFrame) => void,
    onError?: (error: Error) => void,
    onReconnect?: () => void,
  ): () => void {
    let cursor = afterSequence;
    let cancelled = false;
    let subscriptionId: string | undefined;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const attempt = async () => {
      if (cancelled) return;
      const identity = `sub-${++this.nextId}`;
      subscriptionId = identity;
      let first = true;
      let end: (error?: Error) => void = () => {};
      const ended = new Promise<Error | undefined>((resolve) => {
        end = resolve;
      });
      this.subscriptions.set(identity, {
        end,
        frame: (frame) => {
          if (cancelled) return;
          if (first) {
            first = false;
            onReconnect?.();
          }
          if (
            !("events" in frame) &&
            frame.kind === "event" &&
            frame.event.sequence <= cursor
          )
            return;
          onFrame(frame);
          if ("events" in frame)
            for (const event of frame.events)
              cursor = Math.max(cursor, event.sequence);
          else if (frame.kind === "event")
            cursor = Math.max(cursor, frame.event.sequence);
          else if (frame.kind === "error") {
            cancelled = true;
            end(new Error(frame.error));
          } else if (frame.kind === "server_restart") end();
          if (
            ("events" in frame || frame.kind === "event") &&
            this.socket?.readyState === WebSocket.OPEN
          ) {
            this.socket.send(
              JSON.stringify({
                jsonrpc: "2.0",
                method: "session.ack",
                params: {
                  subscription_id: identity,
                  through_sequence: cursor,
                },
              }),
            );
          }
        },
      });
      try {
        await this.call("session.subscribe", {
          session_id: sessionId,
          after_sequence: cursor,
          subscription_id: identity,
          follow: true,
        });
        if (cancelled) {
          void this.call("session.unsubscribe", {
            subscription_id: identity,
          }).catch(() => {});
          return;
        }
        const error = await ended;
        if (error) {
          onError?.(error);
          if (
            error instanceof CoreApiError &&
            [400, 401, 403, 404].includes(error.statusCode)
          )
            cancelled = true;
        }
      } catch (error) {
        const failure =
          error instanceof Error ? error : new Error(String(error));
        if (!cancelled) onError?.(failure);
        if (
          failure instanceof CoreApiError &&
          [400, 401, 403, 404].includes(failure.statusCode)
        )
          cancelled = true;
      } finally {
        this.subscriptions.delete(identity);
        if (!cancelled)
          retryTimer = setTimeout(() => {
            void attempt();
          }, 1000);
      }
    };
    void attempt();
    return () => {
      cancelled = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (subscriptionId) {
        this.subscriptions.get(subscriptionId)?.end();
        this.subscriptions.delete(subscriptionId);
        if (this.socket?.readyState === WebSocket.OPEN)
          void this.call("session.unsubscribe", {
            subscription_id: subscriptionId,
          }).catch(() => {});
      }
    };
  }
}
