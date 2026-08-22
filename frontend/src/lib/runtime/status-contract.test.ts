import { describe, it, expect } from "vitest";
import { deriveChatMessages } from "./event-parser";
import { EventEnvelope } from "./types";

describe("Tool Status Contract", () => {
  it("reconstructs every assistant turn from replayed completion events", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "First question" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "runtime.completed",
        source: "runtime",
        payload: { output: "First answer" },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Second question" },
      },
      {
        session_id: "test",
        sequence: 4,
        event_type: "graph.response_ready",
        source: "graph",
        payload: { output_preview: "Second answer" },
      },
    ];

    const messages = deriveChatMessages(events, "Second answer");

    expect(messages.map((message) => message.content)).toEqual([
      "First question",
      "First answer",
      "Second question",
      "Second answer",
    ]);
  });

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
            display: {
              kind: "context",
              title: "Read",
              summary: "Reading file...",
            },
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
            display: {
              kind: "context",
              title: "Read",
              summary: "Read 10 lines",
            },
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
            tool_name: "read",
            phase: "running",
            status: "running",
            label: "Reading file",
            display: {
              kind: "context",
              title: "Read",
              summary: "Reading file",
            },
          },
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");

    expect(assistantMessage?.tools).toMatchObject([
      {
        id: "call_xyz",
        name: "read",
        label: "Reading file",
        summary: "Reading file",
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
          tool: "write",
          tool_call_id: "call_write",
          arguments: { path: "note.txt", content: "new" },
          tool_status: {
            invocation_id: "call_write",
            tool_name: "write",
            phase: "running",
            status: "running",
            display: { kind: "file", title: "Write", summary: "note.txt" },
          },
        },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "runtime.tool_completed",
        source: "tool",
        payload: {
          tool: "write",
          tool_call_id: "call_write",
          status: "ok",
          arguments: { path: "note.txt", content: "new" },
          path: "note.txt",
          byte_count: 3,
          diff: "--- a/note.txt\n+++ b/note.txt\n@@ -0,0 +1 @@\n+new",
          content: "Wrote file successfully: note.txt",
          error: null,
          tool_status: {
            invocation_id: "call_write",
            tool_name: "write",
            phase: "completed",
            status: "completed",
            display: { kind: "file", title: "Write", summary: "note.txt" },
          },
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");

    expect(assistantMessage?.tools).toHaveLength(1);
    expect(assistantMessage?.tools[0]).toMatchObject({
      id: "call_write",
      name: "write",
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

  it("marks tool request rows as pending while blocked on approval", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Run shell" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "graph.tool_request_created",
        source: "graph",
        payload: {
          tool: "shell_exec",
          tool_call_id: "shell-approval-1",
          arguments: { command: "npm test" },
          tool_status: {
            invocation_id: "shell-approval-1",
            tool_name: "shell_exec",
            phase: "running",
            status: "running",
            display: { kind: "shell", title: "Shell", summary: "npm test" },
          },
        },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "runtime.approval_requested",
        source: "runtime",
        payload: {
          request_id: "approval-1",
          tool: "shell_exec",
          decision: "ask",
          arguments: { command: "npm test" },
          target_summary: "shell_exec",
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");

    expect(assistantMessage?.status).toBe("waiting");
    expect(assistantMessage?.tools[0]).toMatchObject({
      id: "shell-approval-1",
      name: "shell_exec",
      status: "pending",
    });
  });

  it("marks approval-blocked tool rows as failed when approval is denied", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Run shell" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "graph.tool_request_created",
        source: "graph",
        payload: {
          tool: "shell_exec",
          tool_call_id: "shell-deny-1",
          tool_status: {
            invocation_id: "shell-deny-1",
            tool_name: "shell_exec",
            phase: "running",
            status: "running",
            display: { kind: "shell", title: "Shell", summary: "rm -rf build" },
          },
          arguments: { command: "rm -rf build" },
        },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "runtime.approval_requested",
        source: "runtime",
        payload: {
          request_id: "approval-deny-1",
          tool: "shell_exec",
          decision: "ask",
          arguments: { command: "rm -rf build" },
          target_summary: "shell_exec",
        },
      },
      {
        session_id: "test",
        sequence: 4,
        event_type: "runtime.approval_resolved",
        source: "runtime",
        payload: { request_id: "approval-deny-1", decision: "deny" },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");

    expect(assistantMessage?.status).toBe("failed");
    expect(assistantMessage?.approval).toBeNull();
    expect(assistantMessage?.tools[0]).toMatchObject({
      id: "shell-deny-1",
      name: "shell_exec",
      status: "failed",
    });
  });

  it("ignores runtime.tool_completed without tool_status", () => {
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
          tool: "read",
          tool_call_id: "call_read",
          path: "README.md",
          content: "contents",
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");

    expect(assistantMessage?.tools).toEqual([]);
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
    expect(assistantMessage?.thinking).toEqual(["first", "second"]);
  });
});

describe("Tool Display Metadata Contract", () => {
  it("extracts label from display.summary when tool_status.label is absent", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Run command" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "runtime.tool_started",
        source: "runtime",
        payload: {
          tool: "shell_exec",
          tool_call_id: "call_sh",
          tool_status: {
            invocation_id: "call_sh",
            tool_name: "shell_exec",
            phase: "running",
            status: "running",
            display: {
              kind: "shell",
              title: "Shell",
              summary: "List directory contents",
              args: ["ls -la"],
              copyable: { command: "ls -la" },
            },
          },
        },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "runtime.tool_completed",
        source: "tool",
        payload: {
          tool: "shell_exec",
          tool_call_id: "call_sh",
          status: "ok",
          tool_status: {
            invocation_id: "call_sh",
            tool_name: "shell_exec",
            phase: "completed",
            status: "completed",
            display: {
              kind: "shell",
              title: "Shell",
              summary: "List directory contents",
              args: ["ls -la", "", { raw: true }],
              copyable: { command: "ls -la", output: "file1\nfile2\n" },
            },
          },
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");
    expect(assistantMessage).toBeDefined();
    expect(assistantMessage!.tools).toHaveLength(1);
    const tool = assistantMessage!.tools[0];

    expect(tool.id).toBe("call_sh");
    expect(tool.name).toBe("shell_exec");
    // RED: parser must derive label from display.summary when label is absent.
    expect(tool.label).toBe("List directory contents");
    expect(tool.summary).toBe("List directory contents");
    expect(tool.display).toEqual({
      kind: "shell",
      title: "Shell",
      summary: "List directory contents",
      args: ["ls -la"],
      copyable: { command: "ls -la", output: "file1\nfile2\n" },
    });
    expect(tool.copyable).toEqual({
      command: "ls -la",
      output: "file1\nfile2\n",
    });
    expect(tool.status).toBe("completed");
  });

  it("prefers explicit tool_status.label over display.summary", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Run" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "runtime.tool_started",
        source: "runtime",
        payload: {
          tool_status: {
            invocation_id: "call_xyz",
            tool_name: "read",
            phase: "running",
            status: "running",
            label: "Explicit label",
            display: {
              kind: "read",
              title: "Read",
              summary: "Display summary",
            },
          },
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");
    expect(assistantMessage?.tools[0]?.label).toBe("Explicit label");
    expect(assistantMessage?.tools[0]?.summary).toBe("Display summary");
    expect(assistantMessage?.tools[0]?.display).toEqual({
      kind: "read",
      title: "Read",
      summary: "Display summary",
    });
  });

  it("does not lose completed tool status when display metadata is present", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Search" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "runtime.tool_completed",
        source: "tool",
        payload: {
          tool: "grep",
          tool_call_id: "call_grep",
          status: "ok",
          arguments: { pattern: "TODO", path: "." },
          content: "src/app.ts:42: // TODO",
          error: null,
          tool_status: {
            invocation_id: "call_grep",
            tool_name: "grep",
            phase: "completed",
            status: "completed",
            label: "Found 1 match",
            display: {
              kind: "search",
              title: "Search",
              summary: "Found 1 match",
              args: ["TODO", "."],
              copyable: { path: "." },
            },
          },
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");

    expect(assistantMessage?.tools).toHaveLength(1);
    expect(assistantMessage!.tools[0].id).toBe("call_grep");
    expect(assistantMessage!.tools[0].name).toBe("grep");
    expect(assistantMessage!.tools[0].label).toBe("Found 1 match");
    expect(assistantMessage!.tools[0].summary).toBe("Found 1 match");
    expect(assistantMessage!.tools[0].display).toEqual({
      kind: "search",
      title: "Search",
      summary: "Found 1 match",
      args: ["TODO", "."],
      copyable: { path: "." },
    });
    expect(assistantMessage!.tools[0].arguments).toEqual({
      pattern: "TODO",
      path: ".",
    });
    expect(assistantMessage!.tools[0].result).toMatchObject({
      content: "src/app.ts:42: // TODO",
      error: null,
    });
    expect(assistantMessage!.tools[0].status).toBe("completed");
  });

  it("correlates interleaved same-name tool calls by distinct invocation ids", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Read two files" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "runtime.tool_started",
        source: "runtime",
        payload: {
          tool_status: {
            invocation_id: "read-a",
            tool_name: "read",
            phase: "running",
            status: "running",
            display: {
              kind: "context",
              title: "Read",
              summary: "Read a.txt",
              args: ["a.txt"],
              copyable: { path: "a.txt" },
            },
          },
        },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "runtime.tool_started",
        source: "runtime",
        payload: {
          tool_status: {
            invocation_id: "read-b",
            tool_name: "read",
            phase: "running",
            status: "running",
            display: {
              kind: "context",
              title: "Read",
              summary: "Read b.txt",
              args: ["b.txt"],
              copyable: { path: "b.txt" },
            },
          },
        },
      },
      {
        session_id: "test",
        sequence: 4,
        event_type: "runtime.tool_completed",
        source: "tool",
        payload: {
          content: "b contents",
          tool_status: {
            invocation_id: "read-b",
            tool_name: "read",
            phase: "completed",
            status: "completed",
            display: {
              kind: "context",
              title: "Read",
              summary: "Read b.txt",
              args: ["b.txt"],
              copyable: { path: "b.txt" },
            },
          },
        },
      },
      {
        session_id: "test",
        sequence: 5,
        event_type: "runtime.tool_completed",
        source: "tool",
        payload: {
          content: "a contents",
          tool_status: {
            invocation_id: "read-a",
            tool_name: "read",
            phase: "completed",
            status: "completed",
            display: {
              kind: "context",
              title: "Read",
              summary: "Read a.txt",
              args: ["a.txt"],
              copyable: { path: "a.txt" },
            },
          },
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");

    expect(assistantMessage?.tools).toHaveLength(2);
    expect(assistantMessage?.tools.map((tool) => tool.id)).toEqual([
      "read-a",
      "read-b",
    ]);
    expect(assistantMessage?.tools).toEqual([
      expect.objectContaining({
        id: "read-a",
        status: "completed",
        content: "a contents",
        display: expect.objectContaining({ summary: "Read a.txt" }),
        copyable: { path: "a.txt" },
      }),
      expect.objectContaining({
        id: "read-b",
        status: "completed",
        content: "b contents",
        display: expect.objectContaining({ summary: "Read b.txt" }),
        copyable: { path: "b.txt" },
      }),
    ]);
  });
});

describe("Interrupted Status Contract", () => {
  function requestEvent(sequence: number): EventEnvelope {
    return {
      session_id: "test",
      sequence,
      event_type: "runtime.request_received",
      source: "runtime",
      payload: { prompt: "Do the thing" },
    };
  }

  it("maps a genuine runtime.failed to a failed assistant message", () => {
    const events: EventEnvelope[] = [
      requestEvent(1),
      {
        session_id: "test",
        sequence: 2,
        event_type: "runtime.failed",
        source: "runtime",
        payload: { error: "permission denied" },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find(
      (message) => message.role === "assistant",
    );
    expect(assistantMessage?.status).toBe("failed");
  });
  it("preserves provider stream error text on the failed assistant message", () => {
    const events: EventEnvelope[] = [
      requestEvent(1),
      {
        session_id: "test",
        sequence: 2,
        event_type: "graph.provider_stream",
        source: "graph",
        payload: {
          channel: "error",
          kind: "error",
          error: "Provider authentication failed for deepseek.",
        },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "runtime.failed",
        source: "runtime",
        payload: { error: "provider retry exhausted" },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find(
      (message) => message.role === "assistant",
    );
    expect(assistantMessage).toMatchObject({
      status: "failed",
      error: "Provider authentication failed for deepseek.",
    });
  });

  it("maps a cancelled runtime.failed to interrupted, not failed", () => {
    const events: EventEnvelope[] = [
      requestEvent(1),
      {
        session_id: "test",
        sequence: 2,
        event_type: "runtime.failed",
        source: "runtime",
        payload: { cancelled: true, error: "provider stream cancelled" },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find(
      (message) => message.role === "assistant",
    );
    expect(assistantMessage?.status).toBe("interrupted");
    expect(assistantMessage?.status).not.toBe("failed");
  });

  it("maps an interrupted-kind runtime.failed to interrupted, not failed", () => {
    const events: EventEnvelope[] = [
      requestEvent(1),
      {
        session_id: "test",
        sequence: 2,
        event_type: "runtime.failed",
        source: "runtime",
        payload: { kind: "interrupted", error: "web user interrupt" },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find(
      (message) => message.role === "assistant",
    );
    expect(assistantMessage?.status).toBe("interrupted");
    expect(assistantMessage?.status).not.toBe("failed");
  });
});

describe("Live Stream Reasoning Contract", () => {
  it("deduplicates each turn's aggregated reasoning_part against only that turn's streamed deltas", () => {
    // Live wire order per turn (backend): streamed reasoning deltas (client
    // only), then one aggregated runtime.reasoning_part, then the turn-head
    // bookmarks (graph.loop_step / graph.model_turn), then tool events. The
    // `thinking` accumulator spans the whole assistant turn sequence, so the
    // aggregate must be deduplicated against the CURRENT turn's deltas only;
    // comparing against all accumulated thinking would miss for every turn
    // after the first and render each later thinking block doubled.
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "explore" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "graph.provider_stream",
        source: "graph",
        payload: {
          channel: "reasoning",
          kind: "delta",
          text: "first turn part ",
        },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "graph.provider_stream",
        source: "graph",
        payload: { channel: "reasoning", kind: "delta", text: "one" },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "runtime.reasoning_part",
        source: "runtime",
        payload: {
          type: "reasoning",
          text: "first turn part one",
          preview: "first turn part one",
          truncated: false,
          source: "provider_stream",
          visibility: "showable",
        },
      },
      {
        session_id: "test",
        sequence: 4,
        event_type: "graph.loop_step",
        source: "graph",
        payload: { step: 1, phase: "plan" },
      },
      {
        session_id: "test",
        sequence: 5,
        event_type: "graph.model_turn",
        source: "graph",
        payload: { turn: 1, mode: "provider" },
      },
      {
        session_id: "test",
        sequence: 6,
        event_type: "graph.tool_request_created",
        source: "graph",
        payload: {
          tool: "read",
          tool_status: {
            invocation_id: "call_1",
            tool_name: "read",
            phase: "running",
            status: "running",
            display: {
              kind: "context",
              title: "Read",
              summary: "Reading...",
            },
          },
        },
      },
      {
        session_id: "test",
        sequence: 7,
        event_type: "graph.provider_stream",
        source: "graph",
        payload: {
          channel: "reasoning",
          kind: "delta",
          text: "second turn part ",
        },
      },
      {
        session_id: "test",
        sequence: 7,
        event_type: "graph.provider_stream",
        source: "graph",
        payload: { channel: "reasoning", kind: "delta", text: "two" },
      },
      {
        session_id: "test",
        sequence: 8,
        event_type: "runtime.reasoning_part",
        source: "runtime",
        payload: {
          type: "reasoning",
          text: "second turn part two",
          preview: "second turn part two",
          truncated: false,
          source: "provider_stream",
          visibility: "showable",
        },
      },
      {
        session_id: "test",
        sequence: 9,
        event_type: "graph.loop_step",
        source: "graph",
        payload: { step: 2, phase: "plan" },
      },
      {
        session_id: "test",
        sequence: 10,
        event_type: "graph.model_turn",
        source: "graph",
        payload: { turn: 2, mode: "provider" },
      },
      {
        session_id: "test",
        sequence: 11,
        event_type: "graph.response_ready",
        source: "graph",
        payload: { output_preview: "final complete answer" },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find(
      (message) => message.role === "assistant",
    );
    expect(assistantMessage).toBeDefined();

    const reasoningParts = (assistantMessage!.parts ?? []).filter(
      (part) => part.kind === "reasoning",
    );
    expect(reasoningParts).toHaveLength(2);
    expect(
      reasoningParts.map((part) =>
        part.kind === "reasoning" ? part.text : "",
      ),
    ).toEqual(["first turn part one", "second turn part two"]);

    // The aggregated parts must not contain the streamed text twice.
    for (const part of reasoningParts) {
      if (part.kind !== "reasoning") continue;
      expect(part.text).not.toContain(`${part.text}${part.text}`);
    }

    const textParts = (assistantMessage!.parts ?? []).filter(
      (part) => part.kind === "text",
    );
    expect(textParts).toHaveLength(1);
    expect(textParts[0].kind === "text" ? textParts[0].text : "").toBe(
      "final complete answer",
    );
    expect(assistantMessage!.content).toBe("final complete answer");
    expect(assistantMessage!.status).toBe("completed");
  });

  it("skips a truncated aggregated reasoning_part when the full text already streamed", () => {
    // When the persisted aggregate is a truncated prefix (payload.truncated),
    // the client already holds the full streamed text: appending the prefix
    // would duplicate the reasoning block.
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "explore" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "graph.provider_stream",
        source: "graph",
        payload: {
          channel: "reasoning",
          kind: "delta",
          text: "full chain of thought",
        },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "runtime.reasoning_part",
        source: "runtime",
        payload: {
          type: "reasoning",
          text: "full chain of",
          preview: "full chain of",
          truncated: true,
          source: "provider_stream",
          visibility: "showable",
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find(
      (message) => message.role === "assistant",
    );
    const reasoningParts = (assistantMessage!.parts ?? []).filter(
      (part) => part.kind === "reasoning",
    );
    expect(reasoningParts).toHaveLength(1);
    expect(
      reasoningParts[0].kind === "reasoning" && reasoningParts[0].text,
    ).toBe("full chain of thought");
  });
});
