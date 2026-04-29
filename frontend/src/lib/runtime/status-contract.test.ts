import { describe, it, expect } from "vitest";
import { deriveChatMessages } from "./event-parser";
import { EventEnvelope } from "./types";

describe("Tool Status Contract", () => {
  it("renders backend-provided tool status and label", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Read the file" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "some.event.type.does.not.matter",
        source: "graph",
        payload: {
          tool_status: {
            invocation_id: "call_abc",
            tool_name: "read",
            phase: "running",
            status: "running",
            label: "Reading file...",
          },
        },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "another.event.type",
        source: "tool",
        payload: {
          tool_status: {
            invocation_id: "call_abc",
            tool_name: "read",
            phase: "completed",
            status: "completed",
            label: "Read 10 lines",
          },
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");
    expect(assistantMessage).toBeDefined();

    expect(assistantMessage!.tools).toHaveLength(1);
    const tool = assistantMessage!.tools[0];

    expect(tool.id).toBe("call_abc");
    expect(tool.name).toBe("read");
    expect(tool.label).toBe("Read 10 lines");
    expect(tool.status).toBe("completed");
  });

  it("tracks the stable tool-status payload shape without frontend heuristics", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Inspect file" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "runtime.tool_started",
        source: "runtime",
        payload: {
          tool_status: {
            invocation_id: "call_xyz",
            tool_name: "read_file",
            phase: "running",
            status: "running",
            label: "Reading file",
          },
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");

    expect(assistantMessage?.tools).toEqual([
      {
        id: "call_xyz",
        name: "read_file",
        label: "Reading file",
        status: "running",
      },
    ]);
  });

  it("derives pending question prompts for chat", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Ask the user" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "runtime.question_requested",
        source: "runtime",
        payload: {
          request_id: "question-1",
          tool: "question",
          question_count: 1,
          questions: [
            {
              header: "Direction",
              question: "Which path?",
              multiple: false,
              options: [{ label: "left", description: "Use left" }],
            },
          ],
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");

    expect(assistantMessage?.status).toBe("waiting");
    expect(assistantMessage?.question).toEqual({
      requestId: "question-1",
      tool: "question",
      prompts: [
        {
          header: "Direction",
          question: "Which path?",
          multiple: false,
          options: [{ label: "left", description: "Use left" }],
        },
      ],
    });
  });

  it("preserves structured tool arguments and results for activity cards", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Write the file" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "graph.tool_request_created",
        source: "graph",
        payload: {
          tool: "write_file",
          tool_call_id: "call_write",
          arguments: { path: "note.txt", content: "new" },
        },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "runtime.tool_completed",
        source: "tool",
        payload: {
          tool: "write_file",
          tool_call_id: "call_write",
          status: "ok",
          arguments: { path: "note.txt", content: "new" },
          path: "note.txt",
          byte_count: 3,
          diff: "--- a/note.txt\n+++ b/note.txt\n@@ -0,0 +1 @@\n+new",
          content: "Wrote file successfully: note.txt",
          error: null,
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");

    expect(assistantMessage?.tools).toHaveLength(1);
    expect(assistantMessage?.tools[0]).toMatchObject({
      id: "call_write",
      name: "write_file",
      status: "completed",
      arguments: { path: "note.txt", content: "new" },
      result: {
        path: "note.txt",
        byte_count: 3,
        diff: expect.stringContaining("+new"),
      },
      content: "Wrote file successfully: note.txt",
      error: null,
    });
  });

  it("treats runtime.tool_completed without explicit status as completed", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Read" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "runtime.tool_completed",
        source: "tool",
        payload: {
          tool: "read_file",
          tool_call_id: "call_read",
          path: "README.md",
          content: "contents",
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");

    expect(assistantMessage?.tools[0]).toMatchObject({
      id: "call_read",
      name: "read_file",
      status: "completed",
    });
  });

  it("records frontend receive time for reasoning duration when present", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Think" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "graph.provider_stream",
        source: "graph",
        payload: { channel: "reasoning", delta: "first" },
        received_at: 1000,
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "graph.provider_stream",
        source: "graph",
        payload: { channel: "reasoning", delta: "second" },
        received_at: 2500,
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");

    expect(assistantMessage?.thinkingStartedAt).toBe(1000);
    expect(assistantMessage?.thinkingUpdatedAt).toBe(2500);
  });
});
