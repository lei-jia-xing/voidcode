import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ChatThread } from "./ChatThread";
import "../i18n";

vi.mock("react-markdown", () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}));

vi.mock("remark-gfm", () => ({
  default: () => ({}),
}));

describe("ChatThread", () => {
  const baseProps = {
    messages: [],
    isRunning: false,
    isWaitingApproval: false,
    isApprovalSubmitting: false,
    approvalError: null,
    onResolveApproval: vi.fn(),
    isWaitingQuestion: false,
    isQuestionSubmitting: false,
    questionError: null,
    onAnswerQuestion: vi.fn(),
  };

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
    expect(screen.queryByText("Assistant")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/avatar/i)).not.toBeInTheDocument();
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
    expect(screen.getAllByText("src/app.ts")).toHaveLength(2);
    expect(screen.getByText("[offset=10, limit=20]")).toBeInTheDocument();
    expect(screen.getByText("Wrote")).toBeInTheDocument();
    expect(screen.getByText(/\+new/)).toBeInTheDocument();
    expect(screen.getByText("Command")).toBeInTheDocument();
    expect(screen.getByText("$ pytest tests/unit")).toBeInTheDocument();
    expect(screen.getByText("1 passed")).toBeInTheDocument();
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

  it("renders unknown tools with arguments and results", () => {
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
    expect(screen.getByText("Arguments")).toBeInTheDocument();
    expect(screen.getByText(/"server": "local"/)).toBeInTheDocument();
    expect(screen.getByText("Result")).toBeInTheDocument();
    expect(screen.getByText(/"rows": 2/)).toBeInTheDocument();
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
    expect(screen.getByText("visual-engineering")).toBeInTheDocument();
    expect(screen.getByText("[background]")).toBeInTheDocument();
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
    expect(screen.getByText("Audit tool UI")).toBeInTheDocument();
    expect(screen.getByText("Ship observability")).toBeInTheDocument();
    expect(screen.getByText("completed · high")).toBeInTheDocument();
    expect(screen.getByText("in_progress · medium")).toBeInTheDocument();
    expect(screen.getByText(/in_progress=1/)).toBeInTheDocument();
  });
});
