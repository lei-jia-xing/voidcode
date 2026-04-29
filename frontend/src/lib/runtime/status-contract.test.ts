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
});
