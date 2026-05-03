import { afterEach, describe, expect, it, vi } from "vitest";

import { RuntimeClient } from "./client";

describe("RuntimeClient integration contract", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("loads runtime-owned status snapshots from /api/status", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({
        git: { state: "git_ready", root: "/workspace", error: null },
        lsp: { state: "running", error: null, details: {} },
        mcp: {
          state: "failed",
          error: "MCP[demo]: failed to initialize server",
          details: {
            configured_server_count: 1,
            running_server_count: 0,
            failed_server_count: 1,
            retry_available: true,
            servers: [
              {
                server: "demo",
                status: "failed",
                workspace_root: "/workspace",
                stage: "startup",
                error: "MCP[demo]: failed to initialize server",
                command: ["demo"],
                retry_available: true,
              },
            ],
          },
        },
        acp: {
          state: "running",
          error: null,
          details: {
            mode: "managed",
            status: "connected",
            last_request_type: "handshake",
          },
        },
        background_tasks: {
          active_worker_slots: 2,
          queued_count: 3,
          running_count: 1,
          terminal_count: 5,
          default_concurrency: 4,
          provider_concurrency: { "opencode-go": 2 },
          model_concurrency: { "opencode-go/glm-5.1": 1 },
          status_counts: { queued: 3, running: 1, completed: 5 },
        },
      }),
    } as Response);

    const snapshot = await RuntimeClient.getStatus();

    expect(fetchMock).toHaveBeenCalledWith("/api/status");
    expect(snapshot.mcp.details?.servers).toEqual([
      {
        server: "demo",
        status: "failed",
        workspace_root: "/workspace",
        stage: "startup",
        error: "MCP[demo]: failed to initialize server",
        command: ["demo"],
        retry_available: true,
      },
    ]);
    expect(snapshot.acp?.details?.last_request_type).toBe("handshake");
    expect(snapshot.background_tasks).toEqual({
      active_worker_slots: 2,
      queued_count: 3,
      running_count: 1,
      terminal_count: 5,
      default_concurrency: 4,
      provider_concurrency: { "opencode-go": 2 },
      model_concurrency: { "opencode-go/glm-5.1": 1 },
      status_counts: { queued: 3, running: 1, completed: 5 },
    });
  });

  it("parses streamed SSE chunks and preserves backend tool status display payloads", async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            'data: {"kind":"event","session":{"session":{"id":"session-1"},"status":"running","turn":1,"metadata":{}},"event":{"session_id":"session-1","sequence":1,"event_type":"runtime.tool_started","source":"runtime","payload":{"tool":"read_file","tool_call_id":"call-1","display":{"kind":"context","title":"Read","summary":"Read README.md","args":["README.md"],"copyable":{"path":"README.md"}},"tool_status":{"tool_name":"read_file","invocation_id":"call-1","phase":"running","status":"running","label":"Read README.md","display":{"kind":"context","title":"Read","summary":"Read README.md","args":["README.md"],"copyable":{"path":"README.md"}}}},"tool_status":{"tool_name":"read_file","invocation_id":"call-1","phase":"running","status":"running","label":"Read README.md","display":{"kind":"context","title":"Read","summary":"Read README.md","args":["README.md"],"copyable":{"path":"README.md"}}}},"output":null}\n\n',
          ),
        );
        controller.enqueue(
          encoder.encode(
            'data: {"kind":"output","session":{"session":{"id":"session-1"},"status":"completed","turn":1,"metadata":{}},"event":null,"output":"done"}\n\n',
          ),
        );
        controller.close();
      },
    });

    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      body,
    } as Response);

    const chunks = [];
    for await (const chunk of RuntimeClient.runStream({
      prompt: "read README.md",
    })) {
      chunks.push(chunk);
    }

    expect(chunks).toHaveLength(2);
    expect(chunks[0].event?.payload.tool_status).toEqual({
      tool_name: "read_file",
      invocation_id: "call-1",
      phase: "running",
      status: "running",
      label: "Read README.md",
      display: {
        kind: "context",
        title: "Read",
        summary: "Read README.md",
        args: ["README.md"],
        copyable: { path: "README.md" },
      },
    });
    expect(chunks[0].event?.payload.display).toEqual({
      kind: "context",
      title: "Read",
      summary: "Read README.md",
      args: ["README.md"],
      copyable: { path: "README.md" },
    });
    expect(chunks[1].output).toBe("done");
  });

  it("preserves failed shell display metadata and details from streamed events", async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            'data: {"kind":"event","session":{"session":{"id":"session-1"},"status":"failed","turn":1,"metadata":{}},"event":{"session_id":"session-1","sequence":2,"event_type":"runtime.tool_completed","source":"runtime","payload":{"tool":"shell_exec","tool_call_id":"shell-1","status":"error","arguments":{"command":"npm test"},"error":"process failed","data":{"command":"npm test","exit_code":2,"stderr":"boom"},"display":{"kind":"shell","title":"Shell","summary":"Run tests","args":["npm test"],"copyable":{"command":"npm test","output":"boom"}},"tool_status":{"tool_name":"shell_exec","invocation_id":"shell-1","phase":"completed","status":"failed","display":{"kind":"shell","title":"Shell","summary":"Run tests","args":["npm test"],"copyable":{"command":"npm test","output":"boom"}}}}},"output":null}\n\n',
          ),
        );
        controller.close();
      },
    });

    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      body,
    } as Response);

    const chunks = [];
    for await (const chunk of RuntimeClient.runStream({
      prompt: "run tests",
    })) {
      chunks.push(chunk);
    }

    expect(chunks).toHaveLength(1);
    expect(chunks[0].event?.payload.tool_status).toMatchObject({
      invocation_id: "shell-1",
      tool_name: "shell_exec",
      status: "failed",
      display: {
        kind: "shell",
        summary: "Run tests",
        copyable: { command: "npm test", output: "boom" },
      },
    });
    expect(chunks[0].event?.payload.error).toBe("process failed");
  });

  it("preserves structured backend error payloads", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 400,
      statusText: "Bad Request",
      clone() {
        return this;
      },
      json: async () => ({ error: "prompt must be a non-empty string" }),
    } as Response);

    await expect(RuntimeClient.listSessions()).rejects.toThrow(
      "Failed to list sessions: prompt must be a non-empty string",
    );
  });

  it("loads review diffs with path-safe nested file URLs", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({
        root: "/workspace",
        path: "src/app.ts",
        state: "changed",
        diff: "diff --git a/src/app.ts b/src/app.ts",
      }),
    } as Response);

    await RuntimeClient.getReviewDiff("src/app.ts");

    expect(fetchMock).toHaveBeenCalledWith("/api/review/diff/src/app.ts");
    expect(fetchMock).not.toHaveBeenCalledWith("/api/review/diff/src%2Fapp.ts");
  });

  it("escapes special filename characters without encoding path separators", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({
        root: "/workspace",
        path: "src/app file #1.ts",
        state: "clean",
        diff: null,
      }),
    } as Response);

    await RuntimeClient.getReviewDiff("src/app file #1.ts");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/review/diff/src/app%20file%20%231.ts",
    );
  });

  it("recovers after malformed SSE data and fragmented chunks", async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(": ignored\r\n"));
        controller.enqueue(encoder.encode("data: {bad json}\n\n"));
        controller.enqueue(
          encoder.encode(
            'data: {"kind":"output","session":{"session":{"id":"session-1"},"status":"completed","turn":1,"metadata":{}},',
          ),
        );
        controller.enqueue(
          encoder.encode('"event":null,"output":"split done"}'),
        );
        controller.close();
      },
    });
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      body,
    } as Response);

    const chunks = [];
    for await (const chunk of RuntimeClient.runStream({ prompt: "read" })) {
      chunks.push(chunk);
    }

    expect(chunks).toHaveLength(1);
    expect(chunks[0].output).toBe("split done");
  });

  it("answers questions and loads runtime operations endpoints", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = String(input);
        if (url.endsWith("/question")) {
          expect(init?.method).toBe("POST");
          expect(init?.body).toBe(
            JSON.stringify({
              request_id: "question-1",
              responses: [{ header: "Direction", answers: ["left"] }],
            }),
          );
          return {
            ok: true,
            json: async () => ({
              session: {
                session: { id: "session-1" },
                status: "completed",
                turn: 1,
                metadata: {},
              },
              events: [],
              output: "answered",
            }),
          } as Response;
        }
        if (url === "/api/notifications") {
          return { ok: true, json: async () => [] } as Response;
        }
        if (url === "/api/tasks") {
          return { ok: true, json: async () => [] } as Response;
        }
        if (url.endsWith("/debug")) {
          return {
            ok: true,
            json: async () => ({
              session: {
                session: { id: "session-1" },
                status: "completed",
                turn: 1,
                metadata: {},
              },
              prompt: "read",
              persisted_status: "completed",
              current_status: "completed",
              active: false,
              resumable: false,
              replayable: true,
              terminal: true,
            }),
          } as Response;
        }
        throw new Error(`unexpected URL: ${url}`);
      });

    const answer = await RuntimeClient.answerQuestion(
      "session-1",
      "question-1",
      [{ header: "Direction", answers: ["left"] }],
    );
    const notifications = await RuntimeClient.listNotifications();
    const tasks = await RuntimeClient.listBackgroundTasks();
    const debug = await RuntimeClient.getSessionDebug("session-1");

    expect(answer.output).toBe("answered");
    expect(notifications).toEqual([]);
    expect(tasks).toEqual([]);
    expect(debug.prompt).toBe("read");
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });
});
