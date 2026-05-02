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
});
