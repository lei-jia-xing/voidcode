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

  it("renders runtime.tool_completed without tool_status", () => {
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
          status: "ok",
          arguments: { path: "README.md" },
          path: "README.md",
          content: "contents",
        },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const assistantMessage = messages.find((m) => m.role === "assistant");

    expect(assistantMessage?.tools).toHaveLength(1);
    expect(assistantMessage?.tools[0]).toMatchObject({
      id: "call_read",
      name: "read",
      status: "completed",
      arguments: { path: "README.md" },
      content: "contents",
    });
    expect(assistantMessage?.parts).toEqual([
      { kind: "tool", sequence: 2, toolKey: "call_read" },
    ]);
  });
  it("correlates raw graph request, started, and completed events", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Read a file" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "graph.tool_request_created",
        source: "graph",
        payload: {
          tool: "read",
          tool_call_id: "call_raw",
          arguments: { path: "README.md" },
        },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "runtime.tool_started",
        source: "runtime",
        payload: {
          tool: "read",
          tool_call_id: "call_raw",
          execution_intent: {
            tool_call_id: "call_raw",
            tool_name: "read",
            arguments: { path: "README.md", line_start: 1 },
          },
        },
      },
      {
        session_id: "test",
        sequence: 4,
        event_type: "runtime.tool_completed",
        source: "tool",
        payload: {
          tool: "read",
          tool_call_id: "call_raw",
          status: "ok",
          arguments: {},
          content: "file contents",
          data: { path: "README.md" },
        },
      },
    ];

    const assistantMessage = deriveChatMessages(events, null).find(
      (message) => message.role === "assistant",
    );

    expect(assistantMessage?.tools).toHaveLength(1);
    expect(assistantMessage?.tools[0]).toMatchObject({
      id: "call_raw",
      name: "read",
      status: "completed",
      arguments: { path: "README.md", line_start: 1 },
      content: "file contents",
    });
    expect(assistantMessage?.parts).toEqual([
      { kind: "tool", sequence: 2, toolKey: "call_raw" },
    ]);
  });
  it("ignores tool-channel provider text while retaining one lifecycle tool", () => {
    const rawToolCall = '{"name":"read","arguments":{"path":"README.md"}}';
    const events: EventEnvelope[] = [
      {
        session_id: "test",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "Read a file" },
      },
      {
        session_id: "test",
        sequence: 2,
        event_type: "graph.provider_stream",
        source: "graph",
        payload: { channel: "tool", kind: "content", text: rawToolCall },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "graph.tool_request_created",
        source: "graph",
        payload: {
          tool: "read",
          tool_call_id: "call-tool-channel",
          arguments: { path: "README.md" },
          tool_status: {
            invocation_id: "call-tool-channel",
            tool_name: "read",
            phase: "running",
            status: "running",
            display: {
              kind: "context",
              title: "Read",
              summary: "Read README.md",
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
          tool: "read",
          tool_call_id: "call-tool-channel",
          status: "ok",
          arguments: { path: "README.md" },
          content: "file contents",
          tool_status: {
            invocation_id: "call-tool-channel",
            tool_name: "read",
            phase: "completed",
            status: "completed",
            display: {
              kind: "context",
              title: "Read",
              summary: "Read README.md",
            },
          },
        },
      },
    ];

    const assistantMessage = deriveChatMessages(events, null).find(
      (message) => message.role === "assistant",
    );

    expect(assistantMessage?.content).not.toContain(rawToolCall);
    expect(assistantMessage?.tools).toHaveLength(1);
    expect(assistantMessage?.parts).toEqual([
      { kind: "tool", sequence: 3, toolKey: "call-tool-channel" },
    ]);
  });
  it("binds a generated start id to a pending raw graph request", () => {
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
        event_type: "graph.tool_request_created",
        source: "graph",
        payload: {
          tool: "read",
          arguments: { path: "README.md", line_start: 1 },
        },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "runtime.tool_started",
        source: "runtime",
        payload: {
          tool: "read",
          tool_call_id: "runtime-tool-read",
          execution_intent: {
            arguments: { path: "README.md", line_start: 1 },
          },
        },
      },
      {
        session_id: "test",
        sequence: 4,
        event_type: "runtime.tool_completed",
        source: "tool",
        payload: {
          tool: "read",
          tool_call_id: "runtime-tool-read",
          status: "ok",
          content: "contents",
        },
      },
    ];

    const assistantMessage = deriveChatMessages(events, null).find(
      (message) => message.role === "assistant",
    );
    expect(assistantMessage?.tools).toHaveLength(1);
    expect(assistantMessage?.tools[0]).toMatchObject({
      id: "runtime-tool-read",
      name: "read",
      status: "completed",
      arguments: { path: "README.md", line_start: 1 },
    });
    expect(assistantMessage?.parts).toEqual([
      { kind: "tool", sequence: 2, toolKey: "read#2" },
    ]);
  });
  it("does not merge an identified call into an unidentified sibling", () => {
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
          tool: "read",
          execution_intent: { arguments: { path: "a.txt" } },
        },
      },
      {
        session_id: "test",
        sequence: 3,
        event_type: "runtime.tool_started",
        source: "runtime",
        payload: {
          tool: "read",
          tool_call_id: "call-b",
          arguments: { path: "b.txt" },
        },
      },
      {
        session_id: "test",
        sequence: 4,
        event_type: "runtime.tool_completed",
        source: "tool",
        payload: {
          tool: "read",
          tool_call_id: "call-b",
          status: "ok",
          content: "b contents",
        },
      },
      {
        session_id: "test",
        sequence: 5,
        event_type: "runtime.tool_completed",
        source: "tool",
        payload: {
          tool: "read",
          tool_call_id: "runtime-tool-read",
          arguments: { path: "a.txt" },
          status: "ok",
          content: "a contents",
        },
      },
    ];

    const assistantMessage = deriveChatMessages(events, null).find(
      (message) => message.role === "assistant",
    );
    expect(assistantMessage?.tools).toEqual([
      expect.objectContaining({
        id: "runtime-tool-read",
        name: "read",
        status: "completed",
        arguments: { path: "a.txt" },
        content: "a contents",
      }),
      expect.objectContaining({
        id: "call-b",
        name: "read",
        status: "completed",
        arguments: { path: "b.txt" },
        content: "b contents",
      }),
    ]);
    expect(assistantMessage?.parts).toEqual([
      { kind: "tool", sequence: 2, toolKey: "read#2" },
      { kind: "tool", sequence: 3, toolKey: "call-b" },
    ]);
  });

  it("prefers tool_status identity and display over raw fields", () => {
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
        event_type: "runtime.tool_started",
        source: "runtime",
        payload: {
          tool: "raw_read",
          tool_call_id: "raw-id",
          arguments: { path: "raw.txt" },
          execution_intent: { arguments: { path: "intent.txt" } },
          display: {
            kind: "context",
            title: "Raw Read",
            summary: "Raw display",
          },
          tool_status: {
            invocation_id: "status-id",
            tool_name: "status_read",
            status: "running",
            display: {
              kind: "context",
              title: "Status Read",
              summary: "Status display",
            },
          },
        },
      },
    ];

    const assistantMessage = deriveChatMessages(events, null).find(
      (message) => message.role === "assistant",
    );
    expect(assistantMessage?.tools[0]).toMatchObject({
      id: "status-id",
      name: "status_read",
      status: "running",
      summary: "Status display",
      display: { summary: "Status display" },
      arguments: { path: "raw.txt" },
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
    expect(assistantMessage?.thinking).toEqual(["first", "second"]);
  });
});

it("merges runtime.todo_updated into the existing todo tool card", () => {
  const events: EventEnvelope[] = [
    {
      session_id: "test",
      sequence: 1,
      event_type: "runtime.request_received",
      source: "runtime",
      payload: { prompt: "plan" },
    },
    {
      session_id: "test",
      sequence: 2,
      event_type: "runtime.tool_completed",
      source: "runtime",
      payload: {
        tool_status: {
          invocation_id: "todo-call",
          tool_name: "todo",
          phase: "completed",
          status: "completed",
          display: { kind: "todo", title: "Todo", summary: "Updated todos" },
        },
        phases: [
          {
            name: "Plan",
            tasks: [{ content: "Draft plan", status: "pending" }],
          },
        ],
      },
    },
    {
      session_id: "test",
      sequence: 3,
      event_type: "runtime.todo_updated",
      source: "runtime",
      payload: {
        phases: [
          {
            name: "Plan",
            tasks: [{ content: "Draft plan", status: "completed" }],
          },
        ],
        summary: {
          total: 1,
          pending: 0,
          in_progress: 0,
          completed: 1,
          abandoned: 0,
          blocked: 0,
          active: 0,
        },
      },
    },
  ];

  const messages = deriveChatMessages(events, null);
  const tools = messages[1]?.tools.filter((tool) => tool.name === "todo") ?? [];
  expect(tools).toHaveLength(1);
  expect(tools[0]?.result?.phases).toEqual([
    { name: "Plan", tasks: [{ content: "Draft plan", status: "completed" }] },
  ]);
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

describe("Shell tool progress contract", () => {
  const event = (
    sequence: number,
    event_type: string,
    payload: Record<string, unknown>,
  ): EventEnvelope => ({
    session_id: "test",
    sequence,
    event_type,
    source: "runtime",
    payload,
  });
  const start = event(2, "runtime.tool_started", {
    tool: "shell_exec",
    tool_call_id: "shell-1",
    arguments: { command: "printf hi" },
    tool_status: {
      invocation_id: "shell-1",
      tool_name: "shell_exec",
      phase: "running",
      status: "running",
      display: { kind: "shell", title: "Shell", summary: "printf hi" },
    },
  });

  it("appends separate streams, ignores duplicate/out-of-order chunks, and bounds preview", () => {
    const messages = deriveChatMessages(
      [
        event(1, "runtime.request_received", { prompt: "run" }),
        start,
        event(3, "runtime.tool_progress", {
          invocation_id: "shell-1",
          stream: "stdout",
          chunk: "one",
          ordinal: 1,
        }),
        event(4, "runtime.tool_progress", {
          invocation_id: "shell-1",
          stream: "stderr",
          chunk: "warn",
          ordinal: 2,
        }),
        event(5, "runtime.tool_progress", {
          invocation_id: "shell-1",
          stream: "stdout",
          chunk: "one",
          ordinal: 1,
        }),
        event(6, "runtime.tool_progress", {
          invocation_id: "shell-1",
          stream: "stdout",
          chunk: "old",
          ordinal: 0,
        }),
        event(7, "runtime.tool_progress", {
          invocation_id: "shell-1",
          stream: "stdout",
          chunk: "x".repeat(13_000),
          ordinal: 3,
        }),
      ],
      null,
    );
    const tool = messages[1].tools[0];
    expect(tool.liveOutput?.stdout).toHaveLength(12_000);
    expect(tool.liveOutput?.stdout.endsWith("x".repeat(12_000))).toBe(true);
    expect(tool.liveOutput?.stderr).toBe("warn");
  });

  it("reconciles completed data and rejects cancellation late progress", () => {
    const completed = event(4, "runtime.tool_completed", {
      tool_call_id: "shell-1",
      status: "ok",
      data: { stdout: "final", stderr: "" },
      tool_status: {
        invocation_id: "shell-1",
        tool_name: "shell_exec",
        phase: "completed",
        status: "completed",
        display: { kind: "shell", title: "Shell", summary: "done" },
      },
    });
    const messages = deriveChatMessages(
      [
        event(1, "runtime.request_received", { prompt: "run" }),
        start,
        event(3, "runtime.tool_progress", {
          invocation_id: "shell-1",
          stream: "stdout",
          chunk: "preview",
          ordinal: 1,
        }),
        completed,
        event(5, "runtime.tool_progress", {
          invocation_id: "shell-1",
          stream: "stdout",
          chunk: "late",
          ordinal: 2,
        }),
      ],
      null,
    );
    expect(messages[1].tools[0].result?.data).toEqual({
      stdout: "final",
      stderr: "",
    });
    expect(messages[1].tools[0].liveOutput?.stdout).toBe("preview");

    const cancelled = deriveChatMessages(
      [
        event(1, "runtime.request_received", { prompt: "run" }),
        start,
        event(3, "runtime.failed", { cancelled: true, error: "cancelled" }),
        event(4, "runtime.tool_progress", {
          invocation_id: "shell-1",
          stream: "stdout",
          chunk: "late",
          ordinal: 1,
        }),
      ],
      null,
    );
    expect(cancelled[1].tools[0].liveOutput).toBeUndefined();
  });
  it("marks live output degraded when backend reports dropped chunks", () => {
    const messages = deriveChatMessages(
      [
        event(1, "runtime.request_received", { prompt: "run" }),
        start,
        event(3, "runtime.tool_progress", {
          invocation_id: "shell-1",
          stream: "stdout",
          chunk: "before",
          ordinal: 1,
        }),
        event(4, "runtime.tool_progress", {
          invocation_id: "shell-1",
          stream: "stdout",
          gap: true,
          dropped_count: 2,
          loss_reason: "buffer_overflow",
          dropped_ordinal_start: 2,
          dropped_ordinal_end: 3,
        }),
      ],
      null,
    );
    expect(messages[1].tools[0].liveOutput).toMatchObject({
      stdout: "before",
      degraded: true,
    });
  });
  it("does not carry progress across requests that reuse an invocation id", () => {
    const secondStart = event(6, "runtime.tool_started", {
      tool: "shell_exec",
      tool_call_id: "shell-1",
      arguments: { command: "second" },
      tool_status: {
        invocation_id: "shell-1",
        tool_name: "shell_exec",
        phase: "running",
        status: "running",
        display: { kind: "shell", title: "Shell", summary: "second" },
      },
    });
    const messages = deriveChatMessages(
      [
        event(1, "runtime.request_received", { prompt: "first" }),
        start,
        event(3, "runtime.tool_progress", {
          invocation_id: "shell-1",
          stream: "stdout",
          chunk: "first output",
          ordinal: 1,
        }),
        event(4, "runtime.completed", { output: "done" }),
        event(5, "runtime.request_received", { prompt: "second" }),
        secondStart,
        event(7, "runtime.tool_progress", {
          invocation_id: "shell-1",
          stream: "stdout",
          chunk: "second output",
          ordinal: 1,
        }),
      ],
      null,
    );
    const secondAssistant = messages[3];
    expect(secondAssistant.tools[0].liveOutput?.stdout).toBe("second output");
  });
});

describe("Provider tool-call delta contract", () => {
  const event = (
    sequence: number,
    event_type: string,
    payload: Record<string, unknown>,
  ): EventEnvelope => ({
    session_id: "test",
    sequence,
    event_type,
    source: "graph",
    payload,
  });
  const base = event(1, "runtime.request_received", { prompt: "build" });

  it("tracks start, bounded deltas, and end without creating a result", () => {
    const messages = deriveChatMessages(
      [
        base,
        event(2, "graph.tool_call_start", {
          tool_call_id: "call-a",
          tool: "write",
        }),
        event(3, "graph.tool_call_delta", {
          tool_call_id: "call-a",
          arguments_delta: '{"path":',
          fragment_ordinal: 0,
        }),
        event(4, "graph.tool_call_end", { tool_call_id: "call-a" }),
      ],
      null,
    );
    const tool = messages[1].tools[0];
    expect(tool).toMatchObject({
      id: "call-a",
      name: "write",
      status: "running",
      partialArguments: '{"path":',
      argumentsStreamEnded: true,
    });
    expect(tool.result).toBeUndefined();
  });

  it("renders the backend canonical diff preview without creating an execution result", () => {
    const messages = deriveChatMessages(
      [
        base,
        event(2, "graph.tool_call_start", {
          tool_call_id: "call-diff",
          tool: "write",
          diff_preview: { path: "note.txt", diff: "@@ -1 +1 @@\n-old\n+new" },
        }),
      ],
      null,
    );
    expect(messages[1].tools[0].diffPreview).toEqual({
      path: "note.txt",
      diff: "@@ -1 +1 @@\n-old\n+new",
    });
    expect(messages[1].tools[0].result).toBeUndefined();
  });

  it("captures canonical diff previews from raw graph tool requests", () => {
    const messages = deriveChatMessages(
      [
        base,
        event(2, "graph.tool_request_created", {
          tool_call_id: "raw-diff",
          tool: "edit",
          diff_preview: { path: "src/app.ts", diff: "@@ -1 +1 @@\n-old\n+new" },
        }),
      ],
      null,
    );
    expect(messages[1].tools[0]).toMatchObject({
      id: "raw-diff",
      diffPreview: { path: "src/app.ts", diff: "@@ -1 +1 @@\n-old\n+new" },
    });
    expect(messages[1].tools[0].result).toBeUndefined();
  });

  it("keeps concurrent calls isolated and ignores duplicate or late deltas", () => {
    const messages = deriveChatMessages(
      [
        base,
        event(2, "graph.tool_call_start", { tool_call_id: "a", tool: "write" }),
        event(3, "graph.tool_call_start", { tool_call_id: "b", tool: "write" }),
        event(4, "graph.tool_call_delta", {
          tool_call_id: "a",
          arguments_delta: "A",
          fragment_ordinal: 1,
        }),
        event(5, "graph.tool_call_delta", {
          tool_call_id: "a",
          arguments_delta: "old",
          fragment_ordinal: 0,
        }),
        event(6, "graph.tool_call_delta", {
          tool_call_id: "a",
          arguments_delta: "A",
          fragment_ordinal: 1,
        }),
        event(7, "graph.tool_call_delta", {
          tool_call_id: "b",
          arguments_delta: "B",
          fragment_ordinal: 0,
        }),
      ],
      null,
    );
    expect(messages[1].tools.map((tool) => tool.partialArguments)).toEqual([
      "A",
      "B",
    ]);
  });

  it("tracks UTF-8 byte offsets independently from JavaScript character length", () => {
    const messages = deriveChatMessages(
      [
        base,
        event(2, "graph.tool_call_start", {
          tool_call_id: "shell",
          tool: "shell_exec",
        }),
        event(3, "runtime.tool_progress", {
          invocation_id: "shell",
          stream: "stdout",
          chunk: "é",
          offset: 0,
          ordinal: 1,
        }),
        event(4, "runtime.tool_progress", {
          invocation_id: "shell",
          stream: "stdout",
          chunk: "ok",
          offset: 2,
          ordinal: 2,
        }),
      ],
      null,
    );
    expect(messages[1].tools[0].liveOutput).toMatchObject({
      stdout: "éok",
      degraded: false,
    });
  });

  it("keeps canonical degraded preview reasons visible without creating a result", () => {
    const messages = deriveChatMessages(
      [
        base,
        event(2, "graph.tool_call_start", {
          tool_call_id: "call-degraded",
          tool: "write",
          diff_preview: {
            schema_version: 1,
            phase: "partial",
            live_only: true,
            status: "degraded",
            bounded: true,
            truncated: false,
            reason: "preview_callback_unavailable",
          },
        }),
      ],
      null,
    );
    expect(messages[1].tools[0].diffPreview).toMatchObject({
      degraded: true,
      error: "preview_callback_unavailable",
    });
    expect(messages[1].tools[0].result).toBeUndefined();
  });

  it("resets identity and bounds degraded fragments across requests", () => {
    const huge = "x".repeat(13_000);
    const messages = deriveChatMessages(
      [
        base,
        event(2, "graph.tool_call_start", {
          tool_call_id: "same",
          tool: "write",
        }),
        event(3, "graph.tool_call_delta", {
          tool_call_id: "same",
          arguments_delta: huge,
          fragment_ordinal: 0,
          gap: true,
        }),
        event(4, "runtime.completed", { output: "done" }),
        event(5, "runtime.request_received", { prompt: "again" }),
        event(6, "graph.tool_call_start", {
          tool_call_id: "same",
          tool: "read",
        }),
        event(7, "graph.tool_call_delta", {
          tool_call_id: "same",
          arguments_delta: "new",
          fragment_ordinal: 0,
        }),
      ],
      null,
    );
    expect(messages[1].tools[0].partialArguments).toHaveLength(12_000);
    expect(messages[1].tools[0].argumentsStreamDegraded).toBe(true);
    expect(messages[3].tools[0]).toMatchObject({
      name: "read",
      partialArguments: "new",
    });
  });
});
