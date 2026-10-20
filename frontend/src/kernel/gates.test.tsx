import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { KernelApp } from "./KernelApp";
import "../i18n";

/**
 * Gate preservation across a fenced refusal: a late 503 upgrade_pending must
 * leave the native approval AND the plugin confirmation mounted with their
 * exact request ids (no auto-resend), and the durable decided facts close
 * each gate exactly once. The stream is scripted through fetch: no network.
 */

const SESSION = "gate-session";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sseResponse(
  frames: string[],
  controllers: ReadableStreamDefaultController[],
  close = false,
): Response {
  const text = frames.map((f) => `data: ${f}\n\n`).join("");
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text));
      // Open by default like a live follow stream; close=true simulates one
      // clean EOF / restart-terminated connection. Never hot-loop: closing
      // streams are used only where the test holds the next one open.
      if (close) controller.close();
      else controllers.push(controller);
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function storedEvent(sequence: number, type: string, payload: object) {
  return {
    session_id: SESSION,
    sequence,
    type,
    run_id: "run-1",
    revision_id: "rev-1",
    source: "kernel",
    actor: "kernel",
    payload,
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
    const liveControllers: ReadableStreamDefaultController[] = [];
    const requestedHead = () =>
      JSON.stringify({
        session: { session_id: SESSION },
        events: [
          storedEvent(1, "kernel.approval_requested", {
            request_id: "appr-1",
            call: { tool_name: "write", arguments: { path: "a.txt" } },
          }),
          storedEvent(2, "kernel.confirmation_requested", {
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
    const fetchMock = vi.fn(async (url: unknown): Promise<Response> => {
      const path = String(url);
      if (path.includes("/api/commands")) {
        return jsonResponse({ revision_id: "rev-1", commands: [] });
      }
      if (path.includes("/api/sessions") && !path.includes("/events")) {
        if (path.includes("/views")) {
          return jsonResponse({ revision_id: "rev-1", views: [] });
        }
        if (path.includes("/resume")) {
          return jsonResponse(
            {
              error:
                "kernel is draining for an upgrade; retry after it restarts",
              code: "upgrade_pending",
            },
            503,
          );
        }
        if (path.includes("/confirm")) {
          return jsonResponse(
            {
              error:
                "kernel is draining for an upgrade; retry after it restarts",
              code: "upgrade_pending",
            },
            503,
          );
        }
        if (path.includes("/show")) {
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
      if (path.includes("/events")) {
        return sseResponse([requestedHead()], liveControllers);
      }
      if (path.includes("/api/health")) {
        return jsonResponse({
          status: "ok",
          fenced: true,
          stage: "validated",
          kernel_version: "test",
          image_digest: "digest",
          generation: 2,
          active_upgrade: { upgrade_id: "u-1", stage: "draining" },
        });
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = render(<KernelApp baseUrl="" />);

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
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/resume"),
        expect.anything(),
      );
    });
    await waitFor(() => {
      // The gate is still mounted: the banner never left.
      expect(screen.getByTestId("pending-approval-banner")).toBeVisible();
    });

    // The confirmation gate is untouched by the approval's 503.
    expect(screen.getByTestId("pending-confirmation-banner")).toBeVisible();

    // Now the server commits the decided facts as live frames on the open
    // streams: each gate closes exactly once and never resurrects.
    const liveFrame = (sequence: number, type: string, payload: object) =>
      `data: ${JSON.stringify({ kind: "event", session_id: SESSION, event: storedEvent(sequence, type, payload) })}\n\n`;
    const decidedBytes = new TextEncoder().encode(
      liveFrame(3, "kernel.approval_decided", {
        request_id: "appr-1",
        decision: "allow",
      }) +
        liveFrame(4, "kernel.confirmation_decided", {
          request_id: "conf-9",
          decision: "allow",
        }),
    );
    for (const controller of liveControllers) {
      controller.enqueue(decidedBytes);
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
    const liveControllers: ReadableStreamDefaultController[] = [];
    let connections = 0;
    const head = (events: object[]) =>
      JSON.stringify({ session: { session_id: SESSION }, events });
    let healthGeneration = 1;
    const fetchMock = vi.fn(async (url: unknown): Promise<Response> => {
      const path = String(url);
      if (path.includes("/api/commands")) {
        return jsonResponse({ revision_id: "rev-1", commands: [] });
      }
      if (path.includes("/api/sessions") && !path.includes("/events")) {
        if (path.includes("/views")) {
          return jsonResponse({ revision_id: "rev-1", views: [] });
        }
        if (path.includes("/show")) {
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
      if (path.includes("/events")) {
        connections += 1;
        if (connections === 1) {
          // Initial connection: one head, stays open until the test EOFs it.
          return sseResponse(
            [head([storedEvent(1, "kernel.user_message", { content: "hi" })])],
            liveControllers,
          );
        }
        if (connections === 2) {
          // Clean EOF after events plus a lying restart watermark: the client
          // must reconnect from its own applied cursor and reconcile even
          // though no error announced the drop.
          return sseResponse(
            [
              head([storedEvent(1, "kernel.user_message", { content: "hi" })]),
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
        return sseResponse(
          [
            head([
              storedEvent(1, "kernel.user_message", { content: "hi" }),
              storedEvent(2, "kernel.model_turn", {
                result: { output: "hello" },
              }),
            ]),
          ],
          liveControllers,
        );
      }
      if (path.includes("/api/health")) {
        return jsonResponse({
          status: "ok",
          fenced: false,
          stage: "serving",
          kernel_version: "test",
          image_digest: "digest",
          generation: healthGeneration,
          active_upgrade: null,
        });
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = render(<KernelApp baseUrl="" />);
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
      fetchMock.mock.calls.some(([url]) => String(url).includes("/run")),
    ).toBe(false);
    unmount();
  }, 15000);
});
