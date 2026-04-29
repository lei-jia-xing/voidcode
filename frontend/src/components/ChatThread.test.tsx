import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ChatThread } from "./ChatThread";
import { estimateStreamedTextHeight } from "../lib/runtime/text-layout";
import {
  deriveChatMessages,
  type ChatMessage,
} from "../lib/runtime/event-parser";
import type { EventEnvelope } from "../lib/runtime/types";
import "../i18n";

vi.mock("@chenglou/pretext", () => ({
  prepare: vi.fn((text: string) => ({ text })),
  layout: vi.fn(() => ({ height: 46, lineCount: 2 })),
}));

vi.mock("react-markdown", () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}));

vi.mock("remark-gfm", () => ({
  default: () => ({}),
}));

const baseProps = {
  messages: [] as ChatMessage[],
  isRunning: false,
  isWaitingApproval: false,
  isApprovalSubmitting: false,
  approvalError: null as string | null,
  onResolveApproval: vi.fn(),
  isWaitingQuestion: false,
  isQuestionSubmitting: false,
  questionError: null as string | null,
  onAnswerQuestion: vi.fn(),
};

describe("ChatThread", () => {
  it("renders welcome state without avatar graphics", () => {
    render(<ChatThread {...baseProps} />);

    expect(screen.getByText("VoidCode")).toBeInTheDocument();
    expect(
      screen.getByText("Start a conversation with your AI coding agent."),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/avatar/i)).not.toBeInTheDocument();
  });

  it("renders user messages without role label or avatar", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "user",
            content: "hello",
            thinking: [],
            tools: [],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.queryByText("You")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/avatar/i)).not.toBeInTheDocument();
  });

  it("renders assistant messages without role label or avatar", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "hi there",
            thinking: [],
            tools: [],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("hi there")).toBeInTheDocument();
    expect(
      screen.getByText("hi there").closest('[class*="border"]'),
    ).toBeNull();
    expect(screen.queryByText("Assistant")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/avatar/i)).not.toBeInTheDocument();
  });

  it("uses pretext to reserve streamed assistant text height", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "streaming markdown text",
            thinking: [],
            tools: [],
            approval: null,
            status: "in_progress",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(
      screen.getByText("streaming markdown text").parentElement,
    ).toHaveAttribute("data-pretext-estimated-height", "46");
  });

  it("does not reserve pretext height for completed assistant text", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "completed markdown text",
            thinking: [],
            tools: [],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(
      screen.getByText("completed markdown text").parentElement,
    ).not.toHaveAttribute("data-pretext-estimated-height");
  });

  it("exposes a pretext-backed text height estimator", () => {
    expect(estimateStreamedTextHeight("hello world")).toBe(46);
    expect(estimateStreamedTextHeight("   ")).toBe(0);
  });

  it("renders loading placeholder without avatar", () => {
    render(
      <ChatThread
        {...baseProps}
        isRunning
        messages={[
          {
            id: "msg-1",
            role: "user",
            content: "prompt",
            thinking: [],
            tools: [],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("Thinking...")).toBeInTheDocument();
    expect(screen.queryByText("Assistant")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/avatar/i)).not.toBeInTheDocument();
  });

  it("renders approval error without avatar", () => {
    render(<ChatThread {...baseProps} approvalError="something went wrong" />);

    expect(
      screen.getByText("Approval failed: something went wrong"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/avatar/i)).not.toBeInTheDocument();
  });

  it("renders approval card with allow/deny buttons", () => {
    const onResolve = vi.fn();
    render(
      <ChatThread
        {...baseProps}
        isWaitingApproval
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [],
            status: "waiting",
            approval: {
              tool: "write_file",
              targetSummary: "note.txt",
              requestId: "approval-1",
            },
            sequence: 1,
          },
        ]}
        onResolveApproval={onResolve}
      />,
    );

    expect(screen.getByText("Approval Required")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Allow" }));
    expect(onResolve).toHaveBeenCalledWith("allow");
  });

  it("renders question card and submits answers", () => {
    const onAnswer = vi.fn();
    render(
      <ChatThread
        {...baseProps}
        isWaitingQuestion
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [],
            status: "waiting",
            approval: null,
            question: {
              requestId: "question-1",
              tool: "question",
              prompts: [
                {
                  header: "Direction",
                  question: "Which path should I take?",
                  multiple: false,
                  options: [
                    { label: "simple", description: "Use the simple path" },
                  ],
                },
              ],
            },
            sequence: 1,
          },
        ]}
        onAnswerQuestion={onAnswer}
      />,
    );

    fireEvent.click(screen.getByLabelText(/simple/));
    fireEvent.click(screen.getByRole("button", { name: "Submit Answer" }));

    expect(onAnswer).toHaveBeenCalledWith([
      { header: "Direction", answers: ["simple"] },
    ]);
  });

  it("renders thinking block when reasoning exists", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: ["step 1", "step 2"],
            thinkingStartedAt: 1000,
            thinkingUpdatedAt: 2600,
            tools: [],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("Thinking")).toBeInTheDocument();
    expect(screen.getByText("(1.6s)")).toBeInTheDocument();
    expect(screen.queryByText(/steps/)).not.toBeInTheDocument();
  });

  it("renders structured tool activities", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "Done\n<tool>\ndo not show this\n</tool>",
            thinking: [],
            tools: [
              {
                id: "read-1",
                name: "read_file",
                status: "completed",
                arguments: { path: "src/app.ts", offset: 10, limit: 20 },
              },
              {
                id: "write-1",
                name: "write_file",
                status: "completed",
                arguments: { path: "src/app.ts", content: "new" },
                result: {
                  path: "src/app.ts",
                  byte_count: 3,
                  diff: "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -0,0 +1 @@\n+new",
                },
              },
              {
                id: "shell-1",
                name: "shell_exec",
                status: "completed",
                arguments: { command: "pytest tests/unit" },
                result: { exit_code: 0, stdout: "1 passed", stderr: "" },
              },
            ],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("Read")).toBeInTheDocument();
    expect(screen.getByText("src/app.ts")).toBeInTheDocument();
    expect(screen.getByText(/offset=10/)).toBeInTheDocument();
    expect(screen.getByText(/limit=20/)).toBeInTheDocument();
    expect(screen.getByText("Write")).toBeInTheDocument();
    expect(screen.getByText("src/app.ts · 3 bytes")).toBeInTheDocument();
    expect(screen.getByText(/\+new/)).toBeInTheDocument();
    expect(screen.getByText("Shell")).toBeInTheDocument();
    expect(screen.getByText("pytest tests/unit")).toBeInTheDocument();
    expect(screen.queryByText("$ pytest tests/unit")).not.toBeInTheDocument();
    expect(screen.queryByText("1 passed")).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /show details for shell/i }),
    );
    expect(screen.getByText(/\$ pytest tests\/unit/)).toBeInTheDocument();
    expect(screen.getByText(/1 passed/)).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
    expect(screen.queryByText(/do not show this/)).not.toBeInTheDocument();
  });

  it("preserves legitimate tool tags while removing standalone internal tool blocks", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content:
              "Before\n<tool>inline xml stays</tool>\n<tool>\ninternal payload\n</tool>\n```xml\n<tool>code stays</tool>\n```\nAfter",
            thinking: [],
            tools: [],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText(/Before/)).toBeInTheDocument();
    expect(screen.getByText(/inline xml stays/)).toBeInTheDocument();
    expect(screen.getByText(/code stays/)).toBeInTheDocument();
    expect(screen.getByText(/After/)).toBeInTheDocument();
    expect(screen.queryByText(/internal payload/)).not.toBeInTheDocument();
  });

  it("renders skill loading as a first-class tool activity", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [
              {
                id: "skill-1",
                name: "skill",
                status: "completed",
                arguments: {
                  name: "review-work",
                  user_message: "check changes",
                },
                result: {
                  skill: {
                    name: "review-work",
                    description: "Review completed implementation work",
                    source_path:
                      "/home/user/.config/opencode/skills/review-work/SKILL.md",
                  },
                  user_message: "check changes",
                },
              },
            ],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("Loaded skill")).toBeInTheDocument();
    expect(screen.getByText("review-work")).toBeInTheDocument();
    expect(
      screen.getByText("Review completed implementation work"),
    ).toBeInTheDocument();
    expect(screen.getByText(/SKILL\.md/)).toBeInTheDocument();
    expect(screen.getByText("check changes")).toBeInTheDocument();
  });

  it("renders unknown tools as curated summaries without raw results", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [
              {
                id: "tool-1",
                name: "mcp_custom_tool",
                status: "completed",
                arguments: { server: "local", action: "inspect" },
                result: { status: "ok", rows: 2 },
              },
            ],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("mcp_custom_tool")).toBeInTheDocument();
    expect(screen.getByText(/server=local/)).toBeInTheDocument();
    expect(screen.getByText(/action=inspect/)).toBeInTheDocument();
    expect(screen.queryByText("Arguments")).not.toBeInTheDocument();
    expect(screen.queryByText("Result")).not.toBeInTheDocument();
    expect(screen.queryByText(/"rows": 2/)).not.toBeInTheDocument();
  });

  it("renders delegated subagent task activity", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [
              {
                id: "task-1",
                name: "task",
                status: "completed",
                arguments: {
                  category: "visual-engineering",
                  run_in_background: true,
                  load_skills: ["frontend-ui-ux"],
                  description: "Inspect UI",
                },
                result: {
                  task_id: "bg_123",
                  child_session_id: "ses_child",
                  status: "running",
                  requested_category: "visual-engineering",
                  load_skills: ["frontend-ui-ux"],
                },
              },
            ],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("Started subagent")).toBeInTheDocument();
    expect(
      screen.getByText("visual-engineering · background"),
    ).toBeInTheDocument();
    expect(screen.getByText("Inspect UI")).toBeInTheDocument();
    expect(screen.getByText("bg_123")).toBeInTheDocument();
    expect(screen.getByText("ses_child")).toBeInTheDocument();
    expect(screen.getByText("frontend-ui-ux")).toBeInTheDocument();
  });

  it("renders todo updates as a progress list", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [
              {
                id: "todo-1",
                name: "todo_write",
                status: "completed",
                arguments: {
                  todos: [
                    {
                      content: "Audit tool UI",
                      status: "completed",
                      priority: "high",
                    },
                    {
                      content: "Ship observability",
                      status: "in_progress",
                      priority: "medium",
                    },
                  ],
                },
                result: {
                  todos: [
                    {
                      content: "Audit tool UI",
                      status: "completed",
                      priority: "high",
                    },
                    {
                      content: "Ship observability",
                      status: "in_progress",
                      priority: "medium",
                    },
                  ],
                  summary: {
                    total: 2,
                    pending: 0,
                    in_progress: 1,
                    completed: 1,
                    cancelled: 0,
                  },
                },
              },
            ],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("Updated todos")).toBeInTheDocument();
    expect(screen.queryByText("Audit tool UI")).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /show details for updated todos/i }),
    );
    expect(screen.getByText("Audit tool UI")).toBeInTheDocument();
    expect(screen.getByText("Ship observability")).toBeInTheDocument();
    expect(screen.getByText("completed · high")).toBeInTheDocument();
    expect(screen.getByText("in_progress · medium")).toBeInTheDocument();
    expect(screen.getByText(/in_progress=1/)).toBeInTheDocument();
  });
});

describe("Tool Card Display Contract", () => {
  function installClipboardMock() {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    return writeText;
  }

  it("does not render raw JSON arguments or results for generic tools", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [
              {
                id: "mcp-1",
                name: "mcp_unknown_tool",
                status: "completed",
                arguments: {
                  server: "local",
                  action: "inspect",
                  internalState: { nested: { deep: "secret" } },
                },
                result: {
                  status: "ok",
                  internalData: "should not leak",
                },
              },
            ],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    // RED: generic tools must not dump raw JSON in normal chat view.
    expect(screen.queryByText(/"internalState"/)).not.toBeInTheDocument();
    expect(screen.queryByText(/"internalData"/)).not.toBeInTheDocument();
    expect(screen.queryByText(/"deep"/)).not.toBeInTheDocument();
    expect(screen.queryByText(/"secret"/)).not.toBeInTheDocument();

    // Tool name must still be visible.
    expect(screen.getByText("mcp_unknown_tool")).toBeInTheDocument();
    expect(screen.getByText(/server=local/)).toBeInTheDocument();
    expect(screen.getByText(/action=inspect/)).toBeInTheDocument();
  });

  it("renders tool with display metadata label as primary summary", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [
              {
                id: "shell-1",
                name: "shell_exec",
                status: "completed",
                label: "Run unit tests",
                arguments: { command: "pytest tests/unit" },
                result: { exit_code: 0, stdout: "ALL PASSED", stderr: "" },
              },
            ],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("Run unit tests")).toBeInTheDocument();
    expect(screen.queryByText("pytest tests/unit")).not.toBeInTheDocument();
    expect(screen.queryByText("$ pytest tests/unit")).not.toBeInTheDocument();
    expect(screen.queryByText("ALL PASSED")).not.toBeInTheDocument();
  });

  it("supports lightweight shell row with a single expandable terminal block", () => {
    const { container } = render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [
              {
                id: "shell-1",
                name: "shell_exec",
                status: "completed",
                label: "Run lint",
                arguments: { command: "ruff check ." },
                result: {
                  exit_code: 0,
                  stdout: "All checks passed!",
                  stderr: "",
                },
              },
            ],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    const toggle = screen.getByRole("button", {
      name: /show details for shell/i,
    });
    expect(toggle).toBeVisible();
    expect(screen.getByText("Shell")).toBeInTheDocument();
    expect(screen.getByText("Run lint")).toBeInTheDocument();
    expect(toggle.closest('[class*="border"]')).toBeNull();
    expect(screen.queryByText("All checks passed!")).not.toBeInTheDocument();
    expect(
      container.querySelectorAll('[data-terminal-block="shell"]'),
    ).toHaveLength(0);
    fireEvent.click(toggle);
    expect(
      container.querySelectorAll('[data-terminal-block="shell"]'),
    ).toHaveLength(1);
    expect(screen.getByText(/\$ ruff check \./)).toBeInTheDocument();
    expect(screen.getByText(/All checks passed!/)).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /hide details for shell/i }),
    );
    expect(screen.queryByText("All checks passed!")).not.toBeInTheDocument();
  });

  it("copies shell command and output with accessible copied state", async () => {
    const writeText = installClipboardMock();
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [
              {
                id: "shell-1",
                name: "shell_exec",
                status: "completed",
                label: "Run tests",
                arguments: { command: "pytest tests/unit" },
                copyable: { command: "pytest tests/unit", output: "2 passed" },
                result: { exit_code: 0, stdout: "2 passed", stderr: "" },
              },
            ],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: /show details for shell/i }),
    );
    fireEvent.click(screen.getByRole("button", { name: /copy command/i }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith("pytest tests/unit"),
    );
    expect(await screen.findByText("Copied")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /copy output/i }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("2 passed"));
  });

  it("shows failed shell status with expandable stderr, error, and exit code", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [
              {
                id: "shell-1",
                name: "shell_exec",
                status: "failed",
                label: "Run failing command",
                arguments: { command: "npm test" },
                result: { exit_code: 2, stdout: "", stderr: "stderr boom" },
                error: "process failed",
              },
            ],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText(/failed with exit 2/)).toBeInTheDocument();
    expect(screen.queryByText("stderr boom")).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", {
        name: /show details for shell/i,
      }),
    );
    expect(screen.getAllByText(/exit 2/).length).toBeGreaterThan(0);
    expect(screen.getByText(/stderr boom/)).toBeInTheDocument();
    expect(screen.getByText(/process failed/)).toBeInTheDocument();
  });

  it("uses legacy fallback labels without exposing raw payloads", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [
              {
                id: "legacy-1",
                name: "mcp_legacy_tool",
                status: "completed",
                legacy: {
                  label: "Legacy inspect",
                  summary: "Legacy inspect",
                },
                arguments: {
                  path: "README.md",
                  internalState: { secret: true },
                },
                result: { internalData: "hidden" },
              },
            ],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("Legacy inspect")).toBeInTheDocument();
    expect(screen.getByText(/path=README.md/)).toBeInTheDocument();
    expect(screen.queryByText(/internalState/)).not.toBeInTheDocument();
    expect(screen.queryByText(/hidden/)).not.toBeInTheDocument();
  });

  it("groups multiple context tools into one compact disclosure", () => {
    const { container } = render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [
              {
                id: "read-1",
                name: "read_file",
                status: "completed",
                display: {
                  kind: "context",
                  title: "Read",
                  summary: "Read src/app.ts",
                  args: ["src/app.ts"],
                },
                arguments: {
                  path: "src/app.ts",
                  internalState: { secret: true },
                },
              },
              {
                id: "grep-1",
                name: "grep",
                status: "completed",
                display: {
                  kind: "context",
                  title: "Search",
                  summary: "Search TODO",
                  args: ["TODO"],
                },
                arguments: { pattern: "TODO", path: "src" },
              },
            ],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("Context")).toBeInTheDocument();
    expect(screen.getByText("2 context tools")).toBeInTheDocument();
    expect(
      container.querySelector('[data-tool-row="context-group"] button')
        ?.className,
    ).not.toContain("border");
    expect(screen.queryByText("Read src/app.ts")).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /show details for context/i }),
    );
    expect(screen.getByText("Read src/app.ts")).toBeInTheDocument();
    expect(screen.getByText("Search TODO")).toBeInTheDocument();
    expect(screen.queryByText(/secret/)).not.toBeInTheDocument();
  });

  it("hides todo_write when display metadata marks it hidden", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [
              {
                id: "todo-1",
                name: "todo_write",
                status: "completed",
                display: {
                  kind: "todo",
                  title: "Todo",
                  summary: "Updated todos",
                  hidden: true,
                },
                arguments: {
                  todos: [{ content: "Hidden todo", status: "completed" }],
                },
              },
            ],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.queryByText("Updated todos")).not.toBeInTheDocument();
    expect(screen.queryByText("Hidden todo")).not.toBeInTheDocument();
  });

  it("renders derived backend display metadata for failed shell, context, generic, and legacy tools", () => {
    const events: EventEnvelope[] = [
      {
        session_id: "session-1",
        sequence: 1,
        event_type: "runtime.request_received",
        source: "runtime",
        payload: { prompt: "inspect project" },
      },
      {
        session_id: "session-1",
        sequence: 2,
        event_type: "runtime.tool_completed",
        source: "runtime",
        payload: {
          tool: "read_file",
          tool_call_id: "read-1",
          status: "ok",
          arguments: { path: "README.md", internalState: { secret: true } },
          content: "# README",
          tool_status: {
            invocation_id: "read-1",
            tool_name: "read_file",
            status: "completed",
            display: {
              kind: "context",
              title: "Read",
              summary: "Read README.md",
              args: ["README.md"],
              copyable: { path: "README.md" },
            },
          },
        },
      },
      {
        session_id: "session-1",
        sequence: 3,
        event_type: "runtime.tool_completed",
        source: "runtime",
        payload: {
          tool: "grep",
          tool_call_id: "grep-1",
          status: "ok",
          arguments: { pattern: "TODO", path: "." },
          content: "src/app.ts:1: TODO",
          tool_status: {
            invocation_id: "grep-1",
            tool_name: "grep",
            status: "completed",
            display: {
              kind: "context",
              title: "Search",
              summary: "Search TODO",
              args: ["TODO", "."],
            },
          },
        },
      },
      {
        session_id: "session-1",
        sequence: 4,
        event_type: "runtime.tool_completed",
        source: "runtime",
        payload: {
          tool: "shell_exec",
          tool_call_id: "shell-1",
          status: "error",
          arguments: { command: "npm test" },
          data: { command: "npm test", exit_code: 2, stderr: "stderr boom" },
          error: "process failed",
          tool_status: {
            invocation_id: "shell-1",
            tool_name: "shell_exec",
            status: "failed",
            display: {
              kind: "shell",
              title: "Shell",
              summary: "Run failing tests",
              args: ["npm test"],
              copyable: { command: "npm test", output: "stderr boom" },
            },
          },
        },
      },
      {
        session_id: "session-1",
        sequence: 5,
        event_type: "runtime.tool_completed",
        source: "runtime",
        payload: {
          tool: "mcp_custom_tool",
          tool_call_id: "mcp-1",
          status: "ok",
          arguments: { action: "inspect", internalData: { token: "hidden" } },
          result: { rows: 2, internalState: "hidden" },
          tool_status: {
            invocation_id: "mcp-1",
            tool_name: "mcp_custom_tool",
            status: "completed",
            display: {
              kind: "generic",
              title: "mcp_custom_tool",
              summary: "Inspect custom MCP tool",
              args: ["action=inspect"],
            },
          },
        },
      },
      {
        session_id: "session-1",
        sequence: 6,
        event_type: "runtime.tool_completed",
        source: "tool",
        payload: {
          tool: "mcp_legacy_tool",
          tool_call_id: "legacy-1",
          target_summary: "Legacy inspect",
          arguments: { path: "legacy.txt", internalState: { secret: true } },
          result: { internalData: "hidden" },
        },
      },
      {
        session_id: "session-1",
        sequence: 7,
        event_type: "graph.response_ready",
        source: "graph",
        payload: { output: "Done" },
      },
    ];

    const messages = deriveChatMessages(events, null);
    const { container } = render(
      <ChatThread {...baseProps} messages={messages} />,
    );

    expect(screen.getByText("Context")).toBeInTheDocument();
    expect(screen.getByText("2 context tools")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /show details for context/i }),
    );
    expect(screen.getByText("Read README.md")).toBeInTheDocument();
    expect(screen.getByText("Search TODO")).toBeInTheDocument();

    expect(screen.getByText("Shell")).toBeInTheDocument();
    expect(
      screen.getByText(/Run failing tests · failed with exit 2/),
    ).toBeInTheDocument();
    expect(screen.queryByText("stderr boom")).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /show details for shell/i }),
    );
    expect(
      container.querySelectorAll('[data-terminal-block="shell"]'),
    ).toHaveLength(1);
    expect(screen.getByText(/\$ npm test/)).toBeInTheDocument();
    expect(screen.getByText(/stderr boom/)).toBeInTheDocument();
    expect(screen.getByText(/process failed/)).toBeInTheDocument();

    expect(screen.getByText("Inspect custom MCP tool")).toBeInTheDocument();
    expect(screen.getByText(/action=inspect/)).toBeInTheDocument();
    expect(
      screen.getByText("mcp_legacy_tool: Legacy inspect"),
    ).toBeInTheDocument();
    expect(screen.getByText(/path=legacy.txt/)).toBeInTheDocument();
    expect(screen.getByText("Done").closest('[class*="border"]')).toBeNull();
    expect(screen.queryByText(/internalData/)).not.toBeInTheDocument();
    expect(screen.queryByText(/internalState/)).not.toBeInTheDocument();
    expect(screen.queryByText(/token/)).not.toBeInTheDocument();
  });
});
