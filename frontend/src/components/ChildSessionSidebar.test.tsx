import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ChildSessionSidebar } from "./ChildSessionSidebar";
import "../i18n";

const baseProps = {
  parentSessionId: "parent-session",
  tasks: [
    {
      task: { id: "task-1" },
      status: "completed",
      prompt: "Inspect the parser",
      session_id: "child-session",
      created_at: 1,
      updated_at: 2,
    },
  ],
  status: "success" as const,
  error: null,
  selectedTaskId: null,
  taskOutput: null,
  taskOutputStatus: "idle" as const,
  taskOutputError: null,
  onSelectParent: vi.fn(),
  onSelectTask: vi.fn(),
  onRefresh: vi.fn(),
};

describe("ChildSessionSidebar", () => {
  it("renders parent and child task entries", () => {
    render(<ChildSessionSidebar {...baseProps} />);

    expect(screen.getByText("Child Sessions")).toBeInTheDocument();
    expect(screen.getByText("Parent session")).toBeInTheDocument();
    expect(screen.getByText("Inspect the parser")).toBeInTheDocument();
  });

  it("selects child task and parent session", () => {
    const onSelectTask = vi.fn();
    const onSelectParent = vi.fn();
    render(
      <ChildSessionSidebar
        {...baseProps}
        selectedTaskId="task-1"
        onSelectTask={onSelectTask}
        onSelectParent={onSelectParent}
      />,
    );

    fireEvent.click(screen.getByText("Inspect the parser"));
    fireEvent.click(screen.getByText("Parent session"));

    expect(onSelectTask).toHaveBeenCalledWith("task-1");
    expect(onSelectParent).toHaveBeenCalled();
  });

  it("shows empty state when no child sessions exist", () => {
    render(<ChildSessionSidebar {...baseProps} tasks={[]} />);

    expect(screen.getByText("No child sessions yet.")).toBeInTheDocument();
  });

  it("invokes runtime task controls without selecting the task", async () => {
    const onCancelTask = vi.fn().mockResolvedValue(undefined);
    const onRetryTask = vi.fn().mockResolvedValue(undefined);
    const onSteerTask = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <ChildSessionSidebar
        {...baseProps}
        tasks={[{ ...baseProps.tasks[0], status: "running" }]}
        onCancelTask={onCancelTask}
        onRetryTask={onRetryTask}
        onSteerTask={onSteerTask}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancelTask).toHaveBeenCalledWith("task-1");
    expect(baseProps.onSelectTask).not.toHaveBeenCalled();

    rerender(
      <ChildSessionSidebar
        {...baseProps}
        tasks={[
          {
            ...baseProps.tasks[0],
            status: "idle",
            keep_alive: true,
          },
        ]}
        onSteerTask={onSteerTask}
      />,
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Send instruction" }),
      {
        target: { value: "continue checking" },
      },
    );
    fireEvent.submit(screen.getByRole("textbox", { name: "Send instruction" }));
    expect(onSteerTask).toHaveBeenCalledWith("task-1", "continue checking");
  });
});
