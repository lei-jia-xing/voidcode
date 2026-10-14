import { afterEach, describe, expect, it, vi } from "vitest";

import { RuntimeClient, RuntimeClientError } from "./client";

describe("RuntimeClient integration contract", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("preserves event and output order across streamed frames", async () => {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'data: {"kind":"event","session":{"status":"running"},"event":{"session_id":"session-1","sequence":1,"event_type":"runtime.request_received","source":"runtime","payload":{}},"output":null}\n\n',
          ),
        );
        controller.enqueue(
          new TextEncoder().encode(
            'data: {"kind":"output","session":{"status":"completed"},"event":null,"output":"done"}\n\n',
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
    for await (const chunk of RuntimeClient.runStream({ prompt: "read" })) {
      chunks.push(chunk);
    }

    expect(chunks.map((chunk) => chunk.kind)).toEqual(["event", "output"]);
    expect(chunks[0].event?.event_type).toBe("runtime.request_received");
    expect(chunks[0].session?.status).toBe("running");
    expect(chunks[1].session?.status).toBe("completed");
    expect(chunks[1].output).toBe("done");
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

  it("carries the backend error code so callers can route on it", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 404,
      statusText: "Not Found",
      clone() {
        return this;
      },
      json: async () => ({
        error: "no delegated child context for session: child-1",
        code: "delegated_context_missing",
      }),
    } as Response);

    const failure: unknown = await RuntimeClient.getChildSessionContext(
      "child-1",
    ).catch((error: unknown) => error);

    if (!(failure instanceof RuntimeClientError)) {
      throw new Error(`expected a RuntimeClientError, got ${String(failure)}`);
    }
    expect(failure.status).toBe(404);
    expect(failure.code).toBe("delegated_context_missing");
    expect(failure.message).toBe(
      "Failed to load delegated child session context: no delegated child context for session: child-1 (delegated_context_missing)",
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

  it("surfaces checkout failures as RuntimeClientError", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 400,
      statusText: "Bad Request",
      clone: () => ({ json: async () => ({ error: "refused" }) }),
    } as Response);
    await expect(RuntimeClient.checkoutSession("s", 2)).rejects.toMatchObject({
      name: "RuntimeClientError",
      status: 400,
    });
  });
});
