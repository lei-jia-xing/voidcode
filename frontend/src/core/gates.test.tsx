import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { CoreApp } from "./CoreApp";
import "../i18n";
import {
  installRpcPeer,
  frameSource,
  type FrameController,
  type MockResponse,
} from "../test/rpc-peer";

/**
 * Gate preservation across a fenced refusal: a late 503 upgrade_pending must
 * leave the native approval AND the plugin confirmation mounted with their
 * exact request ids (no auto-resend), and the durable decided facts close
 * each gate exactly once. The stream is scripted through a JSON-RPC peer: no network.
 */

const SESSION = "gate-session";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function storedEvent(
  sequence: number,
  type: string,
  payload: object,
  transcript: object | null = null,
) {
  return {
    session_id: SESSION,
    sequence,
    type,
    run_id: "run-1",
    revision_id: "rev-1",
    source: "core",
    actor: "core",
    payload,
    transcript,
  };
}

function toolTranscript(
  sequence: number,
  toolName: string,
  text: string,
  opts: {
    status?: "ok" | "error";
    error?: string | null;
    icon?: { glyph: string } | null;
    interrupted?: boolean;
    execution?: {
      cancellation_signalled: boolean;
      execution_stopped: boolean;
      side_effect_state: "settled" | "unknown";
    } | null;
  } = {},
) {
  return {
    sequence,
    run_id: "run-1",
    revision_id: "rev-1",
    source: "core",
    content: {
      kind: "tool",
      body: {
        call: { tool_name: toolName, arguments: {}, tool_call_id: null },
        result: {
          status: opts.status ?? "ok",
          tool_name: toolName,
          output: {
            type: "text",
            text,
            presentation: null,
          },
          error: opts.error ?? null,
          body: null,
          changes: null,
          coverage: null,
          diagnostics: null,
          execution: opts.execution ?? null,
          timeout_seconds: null,
        },
        interrupted: opts.interrupted ?? false,
        batch_sequence: 2,
        deferred: false,
        icon: opts.icon ?? null,
      },
    },
  };
}

function messageTranscript(sequence: number, content: string) {
  return {
    sequence,
    run_id: "run-1",
    revision_id: "rev-1",
    source: "core",
    content: {
      kind: "message",
      body: {
        role: "user",
        content,
        is_error: false,
        tool_calls: [],
      },
    },
  };
}

function turnTranscript(sequence: number, output: string) {
  return {
    sequence,
    run_id: "run-1",
    revision_id: "rev-1",
    source: "core",
    content: {
      kind: "turn",
      body: {
        output,
        reasoning: null,
        tool_calls: [],
        done_reason: "stop",
        finish_reason_reported: false,
      },
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});
// jsdom has no layout: the app's scroll-to-bottom ref is a safe no-op here.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

describe("gate preservation across upgrade_pending", () => {
  it("keeps both gates actionable with the same id, then closes on decided facts", async () => {
    // One requested head per connection (StrictMode mounts twice); the decided
    // facts arrive later as live frames on the open streams — exactly like a
    // real follow stream. No instant EOF anywhere: that would hot-loop the
    // client's reconnect.
    const liveControllers: FrameController[] = [];
    const requestedHead = () =>
      JSON.stringify({
        session: { session_id: SESSION },
        events: [
          storedEvent(1, "core.approval_requested", {
            request_id: "appr-1",
            call: { tool_name: "write", arguments: { path: "a.txt" } },
          }),
          storedEvent(2, "core.confirmation_requested", {
            request_id: "conf-9",
            ui_type: "confirmation",
            ui_data: {
              request_id: "conf-9",
              title: "Confirm",
              detail: "Proceed?",
            },
          }),
        ],
      });
    const rpcMock = vi.fn(async (method: string): Promise<MockResponse> => {
      if (method === "command.catalog") {
        return jsonResponse({ revision_id: "rev-1", commands: [] });
      }
      if (
        (method.startsWith("session.") && method !== "session.subscribe") ||
        method === "action.confirm"
      ) {
        if (method === "session.views") {
          return jsonResponse({ revision_id: "rev-1", views: [] });
        }
        if (method === "session.resume") {
          return jsonResponse(
            {
              error: "core is draining for an upgrade; retry after it restarts",
              code: "upgrade_pending",
            },
            503,
          );
        }
        if (method === "action.confirm") {
          return jsonResponse(
            {
              error: "core is draining for an upgrade; retry after it restarts",
              code: "upgrade_pending",
            },
            503,
          );
        }
        if (method === "session.show") {
          return jsonResponse({
            session_id: SESSION,
            status: "waiting",
            run_id: "run-1",
            revision_id: "rev-1",
            turn: 1,
            last_event_sequence: 2,
            checkpoint: null,
          });
        }
        // Session list (may carry ?limit=).
        return jsonResponse({
          sessions: [
            {
              session_id: SESSION,
              workspace: "/tmp/ws",
              status: "waiting",
              active_revision_id: "rev-1",
              run_id: "run-1",
              turn: 1,
              last_event_sequence: 2,
            },
          ],
        });
      }
      if (method === "session.subscribe") {
        return frameSource([requestedHead()], liveControllers);
      }
      if (method === "health.get") {
        return jsonResponse({
          status: "ok",
          fenced: true,
          stage: "validated",
          core_version: "test",
          image_digest: "digest",
          generation: 2,
          active_upgrade: { upgrade_id: "u-1", stage: "draining" },
        });
      }
      return jsonResponse({});
    });
    installRpcPeer(rpcMock);

    const { unmount } = render(<CoreApp baseUrl="" />);

    // Both gates mount from the durable requested facts.
    await waitFor(() => {
      expect(screen.getByTestId("pending-approval-banner")).toBeVisible();
    });
    await waitFor(() => {
      expect(screen.getByTestId("pending-confirmation-banner")).toBeVisible();
    });

    // Decide the approval: the 503 leaves the gate armed with the same id.
    fireEvent.click(screen.getByTestId("approval-allow-btn"));
    await waitFor(() => {
      expect(rpcMock).toHaveBeenCalledWith("session.resume", expect.anything());
    });
    await waitFor(() => {
      // The gate is still mounted: the banner never left.
      expect(screen.getByTestId("pending-approval-banner")).toBeVisible();
    });

    // The confirmation gate is untouched by the approval's 503.
    expect(screen.getByTestId("pending-confirmation-banner")).toBeVisible();

    // Now the server commits the decided facts as live frames on the open
    // streams: each gate closes exactly once and never resurrects.
    const liveFrame = (sequence: number, type: string, payload: object) => ({
      kind: "event",
      session_id: SESSION,
      event: storedEvent(sequence, type, payload),
    });
    for (const controller of liveControllers) {
      controller.enqueue([
        liveFrame(3, "core.approval_decided", {
          request_id: "appr-1",
          decision: "allow",
        }),
        liveFrame(4, "core.confirmation_decided", {
          request_id: "conf-9",
          decision: "allow",
        }),
      ]);
    }
    await waitFor(
      () => {
        expect(
          screen.queryByTestId("pending-approval-banner"),
        ).not.toBeInTheDocument();
      },
      { timeout: 5000 },
    );
    await waitFor(
      () => {
        expect(
          screen.queryByTestId("pending-confirmation-banner"),
        ).not.toBeInTheDocument();
      },
      { timeout: 5000 },
    );
    // Decided ids never resurrect on late failures: the gates stay closed.
    expect(
      screen.queryByTestId("pending-approval-banner"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("pending-confirmation-banner"),
    ).not.toBeInTheDocument();
    unmount();
  });

  it("reconciles a clean EOF and a restart frame through the real reader path", async () => {
    // No manual callback counting: connection 1 delivers events then a clean
    // EOF, connection 2 delivers a server_restart frame (watermark 99, a lie
    // the client must not follow), connection 3 stays open and continues.
    // The UI must show the new server identity + refreshed catalog/status
    // with every settled row exactly once and no prompt resend.
    const liveControllers: FrameController[] = [];
    let connections = 0;
    const head = (events: object[]) =>
      JSON.stringify({ session: { session_id: SESSION }, events });
    let healthGeneration = 1;
    const rpcMock = vi.fn(async (method: string): Promise<MockResponse> => {
      if (method === "command.catalog") {
        return jsonResponse({ revision_id: "rev-1", commands: [] });
      }
      if (
        (method.startsWith("session.") && method !== "session.subscribe") ||
        method === "action.confirm"
      ) {
        if (method === "session.views") {
          return jsonResponse({ revision_id: "rev-1", views: [] });
        }
        if (method === "session.show") {
          return jsonResponse({
            session_id: SESSION,
            status: "idle",
            run_id: null,
            revision_id: "rev-1",
            turn: 1,
            last_event_sequence: 3,
            checkpoint: null,
          });
        }
        return jsonResponse({
          sessions: [
            {
              session_id: SESSION,
              workspace: "/tmp/ws",
              status: "idle",
              active_revision_id: "rev-1",
              run_id: null,
              turn: 1,
              last_event_sequence: 3,
            },
          ],
        });
      }
      if (method === "session.subscribe") {
        connections += 1;
        if (connections === 1) {
          // Initial connection: one head, stays open until the test EOFs it.
          return frameSource(
            [
              head([
                storedEvent(
                  1,
                  "core.user_message",
                  { content: "hi" },
                  messageTranscript(1, "hi"),
                ),
              ]),
            ],
            liveControllers,
          );
        }
        if (connections === 2) {
          // Clean EOF after events plus a lying restart watermark: the client
          // must reconnect from its own applied cursor and reconcile even
          // though no error announced the drop.
          return frameSource(
            [
              head([
                storedEvent(
                  1,
                  "core.user_message",
                  { content: "hi" },
                  messageTranscript(1, "hi"),
                ),
              ]),
              JSON.stringify({
                kind: "server_restart",
                session_id: SESSION,
                cursor: 99,
              }),
            ],
            liveControllers,
            true,
          );
        }
        // Reconnected server, new identity; backlog replays, then continues.
        healthGeneration = 2;
        return frameSource(
          [
            head([
              storedEvent(
                1,
                "core.user_message",
                { content: "hi" },
                messageTranscript(1, "hi"),
              ),
              storedEvent(
                2,
                "core.model_turn",
                { result: { output: "hello" } },
                turnTranscript(2, "hello"),
              ),
            ]),
          ],
          liveControllers,
        );
      }
      if (method === "health.get") {
        return jsonResponse({
          status: "ok",
          fenced: false,
          stage: "serving",
          core_version: "test",
          image_digest: "digest",
          generation: healthGeneration,
          active_upgrade: null,
        });
      }
      return jsonResponse({});
    });
    installRpcPeer(rpcMock);

    const { unmount } = render(<CoreApp baseUrl="" />);
    // Initial settled row from the first connections.
    await waitFor(() => {
      expect(screen.getByTestId("event-1")).toBeVisible();
    });
    // Drive the clean-EOF connection: close every open stream so the live
    // reader sees EOF and the transport reconnects through the real path.
    for (const controller of liveControllers) {
      try {
        controller.close();
      } catch {
        // already ended; the live one still EOFs
      }
    }
    await waitFor(
      () => {
        expect(screen.getByTestId("event-2")).toBeVisible();
      },
      { timeout: 8000 },
    );
    // No duplicate settled rows and no prompt resend: only the two rows, and
    // /run was never called.
    expect(screen.getAllByTestId("event-1")).toHaveLength(1);
    expect(screen.getAllByTestId("event-2")).toHaveLength(1);
    expect(
      rpcMock.mock.calls.some(([method]) => method === "session.run"),
    ).toBe(false);
    unmount();
  }, 15000);
});

describe("tool birth icons", () => {
  it("renders the recorded glyph", async () => {
    const head = JSON.stringify({
      session: { session_id: SESSION },
      events: [
        storedEvent(
          3,
          "core.tool_completed",
          {
            batch_sequence: 2,
            call: { tool_name: "read", arguments: {} },
            result: {
              status: "ok",
              tool_name: "read",
              output: { type: "text", text: "contents" },
            },
            icon: { glyph: "⛁" },
          },
          toolTranscript(3, "read", "contents", {
            icon: { glyph: "⛁" },
          }),
        ),
      ],
    });
    const rpcMock = vi.fn(async (method: string): Promise<MockResponse> => {
      if (method === "command.catalog") {
        return jsonResponse({ revision_id: "rev-1", commands: [] });
      }
      if (method === "session.views") {
        return jsonResponse({ revision_id: "rev-1", views: [] });
      }
      if (method === "session.subscribe") {
        return frameSource([head], []);
      }
      if (method === "health.get") {
        return jsonResponse({ status: "ok", fenced: false });
      }
      if (
        (method.startsWith("session.") && method !== "session.subscribe") ||
        method === "action.confirm"
      ) {
        return jsonResponse({
          sessions: [
            {
              session_id: SESSION,
              workspace: "/tmp/ws",
              status: "idle",
              active_revision_id: "rev-1",
              run_id: "run-1",
              turn: 1,
              last_event_sequence: 9,
            },
          ],
        });
      }
      return jsonResponse({});
    });
    installRpcPeer(rpcMock);

    const { unmount } = render(<CoreApp baseUrl="" />);
    await waitFor(() => {
      expect(screen.getByTestId("event-3")).toHaveTextContent("⛁");
    });
    expect(screen.getByTestId("event-3")).toHaveTextContent("Tool [read]");
    unmount();
  });

  it("falls back to the generic mark when history carries no icon", async () => {
    const head = JSON.stringify({
      session: { session_id: SESSION },
      events: [
        storedEvent(
          4,
          "core.tool_completed",
          {
            batch_sequence: 2,
            call: { tool_name: "mystery_tool", arguments: {} },
            result: {
              status: "ok",
              tool_name: "mystery_tool",
              output: { type: "text", text: "done" },
            },
          },
          toolTranscript(4, "mystery_tool", "done", { icon: null }),
        ),
      ],
    });
    const rpcMock = vi.fn(async (method: string): Promise<MockResponse> => {
      if (method === "command.catalog") {
        return jsonResponse({ revision_id: "rev-1", commands: [] });
      }
      if (method === "session.views") {
        return jsonResponse({ revision_id: "rev-1", views: [] });
      }
      if (method === "session.subscribe") {
        return frameSource([head], []);
      }
      if (method === "health.get") {
        return jsonResponse({ status: "ok", fenced: false });
      }
      if (
        (method.startsWith("session.") && method !== "session.subscribe") ||
        method === "action.confirm"
      ) {
        return jsonResponse({
          sessions: [
            {
              session_id: SESSION,
              workspace: "/tmp/ws",
              status: "idle",
              active_revision_id: "rev-1",
              run_id: "run-1",
              turn: 1,
              last_event_sequence: 9,
            },
          ],
        });
      }
      return jsonResponse({});
    });
    installRpcPeer(rpcMock);
    const { unmount } = render(<CoreApp baseUrl="" />);
    await waitFor(() => {
      expect(screen.getByTestId("event-4")).toHaveTextContent("▷");
    });
    expect(screen.getByTestId("event-4")).toHaveTextContent(
      "Tool [mystery_tool]",
    );
    unmount();
  });
});

describe("canonical transcript cutover", () => {
  it("renders continuation/wakeup canonical messages with role alignment", async () => {
    const head = JSON.stringify({
      session: { session_id: SESSION },
      events: [
        storedEvent(
          7,
          "core.continuation_started",
          { role: "user", payload: { text: "continued work" } },
          {
            sequence: 7,
            run_id: "run-1",
            revision_id: "rev-1",
            source: "core",
            content: {
              kind: "message",
              body: {
                role: "user",
                content: "continued work",
                is_error: false,
                tool_calls: [],
              },
            },
          },
        ),
        storedEvent(
          8,
          "core.wakeup_fired",
          { role: "system", owner: "cron", payload: { tick: 1 } },
          {
            sequence: 8,
            run_id: "run-1",
            revision_id: "rev-1",
            source: "core",
            content: {
              kind: "message",
              body: {
                role: "system",
                content: '[Durable wakeup from cron]\n{"tick": 1}',
                is_error: false,
                tool_calls: [],
              },
            },
          },
        ),
      ],
    });
    const rpcMock = vi.fn(async (method: string): Promise<MockResponse> => {
      if (method === "command.catalog") {
        return jsonResponse({ revision_id: "rev-1", commands: [] });
      }
      if (method === "session.views") {
        return jsonResponse({ revision_id: "rev-1", views: [] });
      }
      if (method === "session.subscribe") {
        return frameSource([head], []);
      }
      if (method === "health.get") {
        return jsonResponse({ status: "ok", fenced: false });
      }
      if (
        (method.startsWith("session.") && method !== "session.subscribe") ||
        method === "action.confirm"
      ) {
        return jsonResponse({
          sessions: [
            {
              session_id: SESSION,
              workspace: "/tmp/ws",
              status: "idle",
              active_revision_id: "rev-1",
              run_id: "run-1",
              turn: 1,
              last_event_sequence: 9,
            },
          ],
        });
      }
      return jsonResponse({});
    });
    installRpcPeer(rpcMock);
    const { unmount } = render(<CoreApp baseUrl="" />);
    await waitFor(() => {
      expect(screen.getByTestId("event-7")).toHaveTextContent("continued work");
    });
    expect(screen.getByTestId("event-7")).toHaveClass("items-end");
    await waitFor(() => {
      expect(screen.getByTestId("event-8")).toHaveTextContent("Durable wakeup");
    });
    expect(screen.getByTestId("event-8")).toHaveClass("items-start");
    unmount();
  });

  it("renders a cancelled tool with its execution cue and never the payload fallback", async () => {
    // The payload deliberately disagrees: canonical UI reads transcript only.
    const head = JSON.stringify({
      session: { session_id: SESSION },
      events: [
        storedEvent(
          5,
          "core.tool_completed",
          {
            batch_sequence: 2,
            call: { tool_name: "payload_name", arguments: {} },
            result: {
              status: "ok",
              tool_name: "payload_name",
              output: { type: "text", text: "payload text" },
            },
          },
          toolTranscript(5, "read", "canonical text", {
            status: "error",
            error: "tool invocation was cancelled",
            execution: {
              cancellation_signalled: true,
              execution_stopped: true,
              side_effect_state: "settled",
            },
          }),
        ),
        storedEvent(
          6,
          "core.tool_interrupted",
          {
            batch_sequence: 2,
            call: { tool_name: "write", arguments: {} },
            result: {
              status: "error",
              tool_name: "write",
              output: { type: "text", text: "unconfirmed text" },
            },
          },
          toolTranscript(6, "write", "unconfirmed text", {
            status: "error",
            error: "tool timed out",
            execution: {
              cancellation_signalled: false,
              execution_stopped: false,
              side_effect_state: "unknown",
            },
          }),
        ),
      ],
    });
    const rpcMock = vi.fn(async (method: string): Promise<MockResponse> => {
      if (method === "command.catalog") {
        return jsonResponse({ revision_id: "rev-1", commands: [] });
      }
      if (method === "session.views") {
        return jsonResponse({ revision_id: "rev-1", views: [] });
      }
      if (method === "session.subscribe") {
        return frameSource([head], []);
      }
      if (method === "health.get") {
        return jsonResponse({ status: "ok", fenced: false });
      }
      if (
        (method.startsWith("session.") && method !== "session.subscribe") ||
        method === "action.confirm"
      ) {
        return jsonResponse({
          sessions: [
            {
              session_id: SESSION,
              workspace: "/tmp/ws",
              status: "idle",
              active_revision_id: "rev-1",
              run_id: "run-1",
              turn: 1,
              last_event_sequence: 9,
            },
          ],
        });
      }
      return jsonResponse({});
    });
    installRpcPeer(rpcMock);
    const { unmount } = render(<CoreApp baseUrl="" />);
    await waitFor(() => {
      expect(screen.getByTestId("event-5")).toHaveTextContent("Tool [read]");
    });
    // Transcript wins over the disagreeing payload on name and error text.
    expect(screen.getByTestId("event-5")).not.toHaveTextContent("payload");
    expect(screen.getByTestId("event-5")).toHaveTextContent(
      "tool invocation was cancelled",
    );
    // A stopped/settled tool carries no execution cue.
    expect(screen.getByTestId("event-5")).not.toHaveTextContent("side effects");
    // An unstopped tool shows the unknown-effects cue inline.
    await waitFor(() => {
      expect(screen.getByTestId("event-6")).toHaveTextContent(
        "may still be running; side effects unknown",
      );
    });
    unmount();
  });

  it("shows a terminal error frame once and stops without reconnecting", async () => {
    let connections = 0;
    const frames = [
      JSON.stringify({
        session: { session_id: SESSION },
        events: [
          storedEvent(
            1,
            "core.user_message",
            { content: "hi" },
            messageTranscript(1, "hi"),
          ),
        ],
      }),
      JSON.stringify({
        kind: "error",
        session_id: SESSION,
        error:
          "projection failed: sequence=2 type=core.tool_completed error=ValidationError",
      }),
    ];
    const rpcMock = vi.fn(async (method: string): Promise<MockResponse> => {
      if (method === "command.catalog") {
        return jsonResponse({ revision_id: "rev-1", commands: [] });
      }
      if (method === "session.views") {
        return jsonResponse({ revision_id: "rev-1", views: [] });
      }
      if (method === "session.subscribe") {
        connections += 1;
        return frameSource(frames, [], connections > 1);
      }
      if (method === "health.get") {
        return jsonResponse({ status: "ok", fenced: false });
      }
      if (
        (method.startsWith("session.") && method !== "session.subscribe") ||
        method === "action.confirm"
      ) {
        return jsonResponse({
          sessions: [
            {
              session_id: SESSION,
              workspace: "/tmp/ws",
              status: "idle",
              active_revision_id: "rev-1",
              run_id: "run-1",
              turn: 1,
              last_event_sequence: 9,
            },
          ],
        });
      }
      return jsonResponse({});
    });
    installRpcPeer(rpcMock);
    const { unmount } = render(<CoreApp baseUrl="" />);
    await waitFor(() => {
      expect(screen.getByTestId("event-1")).toHaveTextContent("hi");
    });
    // The safe error surfaces once in the status line; settled rows stay.
    await waitFor(() => {
      expect(screen.getByText(/Stream error:/)).toHaveTextContent(
        "projection failed: sequence=2",
      );
    });
    expect(screen.getByTestId("event-1")).toBeVisible();
    const settledConnections = connections;
    // No hot retry on the corrupt cursor: the stream stays stopped.
    const { promise, resolve } = Promise.withResolvers<void>();
    setTimeout(resolve, 1500);
    await promise;
    expect(connections).toBe(settledConnections);
    unmount();
  });
});
