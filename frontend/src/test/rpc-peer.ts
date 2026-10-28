/** In-memory JSON-RPC peer: exercises the real browser client's message path. */
import { vi } from "vitest";

export interface FrameController {
  enqueue: (frames: unknown[]) => void;
  close: () => void;
}
export interface FrameSource {
  frames: unknown[];
  controllers: FrameController[];
  close: boolean;
}
export type MockResponse = Response | FrameSource;

export function frameSource(
  frames: string[],
  controllers: FrameController[],
  close = false,
): FrameSource {
  return {
    frames: frames.map((frame) => JSON.parse(frame) as unknown),
    controllers,
    close,
  };
}

export function installRpcPeer(
  handler: (
    method: string,
    params: Record<string, unknown>,
  ) => Promise<MockResponse>,
) {
  const sockets: Socket[] = [];
  class Socket {
    static OPEN = 1;
    static CLOSED = 3;
    readyState = 0;
    bufferedAmount = 0;
    onopen?: () => void;
    onclose?: () => void;
    onerror?: () => void;
    onmessage?: (message: { data: string }) => void;
    private live = new Set<string>();

    constructor() {
      sockets.push(this);
      queueMicrotask(() => {
        if (this.readyState === 0) {
          this.readyState = 1;
          this.onopen?.();
        }
      });
    }
    private receive(message: object) {
      if (this.readyState === 1)
        this.onmessage?.({ data: JSON.stringify(message) });
    }
    send(text: string): void {
      const message = JSON.parse(text) as {
        method: string;
        id: string;
        params: Record<string, unknown>;
      };
      void this.dispatch(message);
    }
    private async dispatch(message: {
      method: string;
      id: string;
      params: Record<string, unknown>;
    }) {
      const success = (result: unknown) =>
        this.receive({ jsonrpc: "2.0", id: message.id, result });
      if (message.method === "connection.authenticate") {
        success({ protocol_version: 1 });
        return;
      }
      if (message.method === "session.ack") return;
      if (message.method === "session.unsubscribe") {
        const id = message.params.subscription_id as string;
        success({ removed: this.live.delete(id) });
        return;
      }
      const response = await handler(message.method, message.params);
      if (response instanceof Response) {
        const body = (await response.json()) as Record<string, unknown>;
        if (response.ok) success(body);
        else
          this.receive({
            jsonrpc: "2.0",
            id: message.id,
            error: {
              code: -32000,
              message: body.error,
              data: { status: response.status, code: body.code },
            },
          });
        return;
      }
      const id = message.params.subscription_id as string;
      this.live.add(id);
      success({ subscription_id: id });
      const controller: FrameController = {
        enqueue: (frames) => {
          for (const frame of frames)
            if (this.live.has(id))
              this.receive({
                jsonrpc: "2.0",
                method: "session.frame",
                params: { subscription_id: id, frame },
              });
        },
        close: () => {
          if (this.live.delete(id))
            this.receive({
              jsonrpc: "2.0",
              method: "subscription.closed",
              params: { subscription_id: id },
            });
        },
      };
      controller.enqueue(response.frames);
      if (response.close) controller.close();
      else response.controllers.push(controller);
    }
    close(): void {
      this.readyState = 3;
      this.live.clear();
      this.onclose?.();
    }
  }
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("fetch", async () => handler("health.get", {}));
  return {
    drop: () => {
      for (const socket of sockets) if (socket.readyState === 1) socket.close();
    },
  };
}
