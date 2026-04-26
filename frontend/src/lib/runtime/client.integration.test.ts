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
  });

  it("parses streamed SSE chunks and preserves backend tool status payloads", async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            'data: {"kind":"event","session":{"session":{"id":"session-1"},"status":"running","turn":1,"metadata":{}},"event":{"session_id":"session-1","sequence":1,"event_type":"runtime.tool_started","source":"runtime","payload":{"tool_status":{"tool_name":"read_file","invocation_id":"call-1","phase":"running","status":"running","label":"Reading file"}},"tool_status":{"tool_name":"read_file","invocation_id":"call-1","phase":"running","status":"running","label":"Reading file"}},"output":null}\n\n',
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
      label: "Reading file",
    });
    expect(chunks[1].output).toBe("done");
  });
});
