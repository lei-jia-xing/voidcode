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

    expect(assistantMessage?.tools).toMatchObject([
      {
        id: "call_xyz",
        name: "read_file",
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
    expect(assistantMessage?.thinking).not.toContain("first");
    expect(assistantMessage?.thinking).not.toContain("second");
    expect(assistantMessage?.thinking).toHaveLength(0);
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

  it("provides curated fallback for old tool_completed without tool_status", () => {
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
    expect(assistantMessage?.tools).toHaveLength(1);
    const tool = assistantMessage!.tools[0];

    expect(tool.id).toBe("call_read");
    expect(tool.name).toBe("read_file");
    expect(tool.status).toBe("completed");
    expect(tool.summary).toBe("Read: README.md");
    expect(tool.legacy).toEqual({
      label: "Read: README.md",
      summary: "Read: README.md",
    });

    // RED: legacy events without tool_status must not leak raw JSON as label.
    const label = tool.label;
    if (label !== undefined) {
      expect(label).not.toContain("{");
      expect(label).not.toContain('"tool"');
      expect(label).not.toContain("arguments");
    }
  });

  it("prefers explicit tool_status.label over display.summary for compatibility", () => {
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
            tool_name: "read_file",
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
            tool_name: "read_file",
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
            tool_name: "read_file",
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
            tool_name: "read_file",
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
