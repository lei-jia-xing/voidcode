import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TodoPanel } from "./TodoPanel";
import {
  deriveLatestTodoSnapshot,
  type TodoPanelSnapshot,
} from "./todoPanelModel";
import type { ChatMessage } from "../lib/runtime/event-parser";
import "../i18n";

function messageWithTodos(
  id: string,
  todos: Record<string, unknown>[],
): ChatMessage {
  return {
    id,
    role: "assistant",
    content: "",
    thinking: [],
    tools: [
      {
        id: `${id}-todo`,
        name: "todo_write",
        status: "completed",
        arguments: { todos },
      },
    ],
    approval: null,
    status: "completed",
    sequence: 1,
  };
}

describe("TodoPanel", () => {
  it("renders todo content with status and priority metadata", () => {
    const snapshot: TodoPanelSnapshot = {
      items: [
        {
          content: "Fix tool grouping",
          status: "in_progress",
          priority: "high",
        },
        {
          content: "Add tests",
          status: "pending",
          priority: "medium",
        },
      ],
    };

    render(<TodoPanel snapshot={snapshot} />);

    expect(
      screen.getByRole("button", { name: /show current todos/i }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("0/2 done")).toBeInTheDocument();
    expect(screen.queryByText("Fix tool grouping")).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /show current todos/i }),
    );
    expect(screen.getByText("Fix tool grouping")).toBeInTheDocument();
    expect(screen.getByText("Add tests")).toBeInTheDocument();
    expect(screen.getByText("in progress")).toBeInTheDocument();
    expect(screen.getByText("high")).toBeInTheDocument();
  });

  it("derives the latest todo_write snapshot from chat messages", () => {
    const snapshot = deriveLatestTodoSnapshot([
      messageWithTodos("old", [
        { content: "Old item", status: "pending", priority: "low" },
      ]),
      messageWithTodos("new", [
        { content: "Current item", status: "completed", priority: "high" },
      ]),
    ]);

    expect(snapshot?.items).toEqual([
      { content: "Current item", status: "completed", priority: "high" },
    ]);
  });

  it("supports nested runtime result data", () => {
    const snapshot = deriveLatestTodoSnapshot([
      {
        ...messageWithTodos("nested", []),
        tools: [
          {
            id: "todo-nested",
            name: "todo_write",
            status: "completed",
            result: {
              data: {
                todos: [
                  {
                    content: "Nested item",
                    status: "pending",
                    priority: "medium",
                  },
                ],
              },
            },
          },
        ],
      },
    ]);

    expect(snapshot?.items[0]?.content).toBe("Nested item");
  });

  it("falls back to rendered tool content when structured todo content is redacted", () => {
    const snapshot = deriveLatestTodoSnapshot([
      {
        ...messageWithTodos("redacted", []),
        tools: [
          {
            id: "todo-redacted",
            name: "todo_write",
            status: "completed",
            result: {
              todos: [
                { content: "", status: "completed", priority: "high" },
                { content: "", status: "in_progress", priority: "high" },
              ],
            },
            content:
              "Updated 2 todos\n1. [completed/high] 阅读项目文档，了解整体架构\n2. [in_progress/high] 实现用户登录功能",
          },
        ],
      },
    ]);

    expect(snapshot?.items).toEqual([
      {
        content: "阅读项目文档，了解整体架构",
        status: "completed",
        priority: "high",
      },
      {
        content: "实现用户登录功能",
        status: "in_progress",
        priority: "high",
      },
    ]);
  });
});
