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
            tools: [],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("Thinking")).toBeInTheDocument();
    expect(screen.getByText("(2 steps)")).toBeInTheDocument();
  });

  it("renders tool indicators", () => {
    render(
      <ChatThread
        {...baseProps}
        messages={[
          {
            id: "msg-1",
            role: "assistant",
            content: "",
            thinking: [],
            tools: [{ name: "read_file", status: "completed" }],
            approval: null,
            status: "completed",
            sequence: 1,
          },
        ]}
      />,
    );

    expect(screen.getByText("read_file")).toBeInTheDocument();
  });
});
