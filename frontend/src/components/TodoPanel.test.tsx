import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TodoPanel } from "./TodoPanel";
import {
  deriveLatestTodoSnapshot,
  type TodoPanelSnapshot,
} from "./todoPanelModel";
import type { ChatMessage } from "../lib/runtime/event-parser";
import i18n from "../i18n";

function messageWithTodos(
  id: string,
  tasks: Record<string, unknown>[],
): ChatMessage {
  return {
    id,
    role: "assistant",
    content: "",
    thinking: [],
    tools: [
      {
        id: `${id}-todo`,
        name: "todo",
        status: "completed",
        arguments: {
          op: "init",
          list: [
            {
              phase: "Tasks",
              items: tasks.map((task) => String(task.content ?? "")),
            },
          ],
        },
        result: {
          data: { phases: [{ name: "Tasks", tasks }] },
        },
      },
    ],
    approval: null,
    status: "completed",
    sequence: 1,
  };
}

describe("TodoPanel", () => {
  it("renders todo content with status metadata", () => {
    const snapshot: TodoPanelSnapshot = {
      items: [
        {
          content: "Fix tool grouping",
          status: "in_progress",
        },
        {
          content: "Add tests",
          status: "pending",
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
  });

  it("derives the latest todo snapshot from phase results", () => {
    const snapshot = deriveLatestTodoSnapshot([
      messageWithTodos("old", [{ content: "Old item", status: "pending" }]),
      messageWithTodos("new", [
        { content: "Current item", status: "completed" },
      ]),
    ]);

    expect(snapshot?.items).toEqual([
      { content: "Current item", status: "completed", phase: "Tasks" },
    ]);
  });

  it("supports nested runtime phase result data", () => {
    const snapshot = deriveLatestTodoSnapshot([
      {
        ...messageWithTodos("nested", []),
        tools: [
          {
            id: "todo-nested",
            name: "todo",
            status: "completed",
            result: {
              data: {
                phases: [
                  {
                    name: "Nested",
                    tasks: [{ content: "Nested item", status: "pending" }],
                  },
                ],
              },
            },
          },
        ],
      },
    ]);

    expect(snapshot?.items[0]).toEqual({
      content: "Nested item",
      status: "pending",
      phase: "Nested",
    });
  });

  it("renders blocked phase tasks and their reason", () => {
    const snapshot = deriveLatestTodoSnapshot([
      messageWithTodos("blocked", [
        {
          content: "Wait for dependency",
          status: "blocked",
          blocker: "dependency",
        },
      ]),
    ]);

    expect(snapshot?.items).toEqual([
      {
        content: "Wait for dependency",
        status: "blocked",
        phase: "Tasks",
        blocker: "dependency",
      },
    ]);
  });

  it("uses redacted tool content without allowing an in-flight result to mask it", () => {
    const completed = messageWithTodos("completed", []);
    completed.tools[0].result = { data: {} };
    completed.tools[0].content =
      "Todo details are available in the runtime view.";
    const running = messageWithTodos("running", [
      { content: "stale", status: "pending" },
    ]);
    running.tools[0].status = "running";
    running.tools[0].result = undefined;

    expect(deriveLatestTodoSnapshot([completed, running])).toEqual({
      items: [
        {
          content: "Todo details are available in the runtime view.",
          status: "completed",
        },
      ],
    });
  });
  it("falls back to rendered content when phase tasks are redacted", () => {
    const redacted = messageWithTodos("redacted", []);
    redacted.tools[0].result = {
      data: { phases: [{ name: "Tasks", tasks: [{ status: "pending" }] }] },
    };
    redacted.tools[0].content = "The runtime todo list is available.";

    expect(deriveLatestTodoSnapshot([redacted])).toEqual({
      items: [
        { content: "The runtime todo list is available.", status: "completed" },
      ],
    });
  });

  it("ignores failed todo results but honors an explicit successful empty phase list", () => {
    const completed = messageWithTodos("completed", [
      { content: "Old", status: "pending" },
    ]);
    const failed = messageWithTodos("failed", [
      { content: "Failed", status: "pending" },
    ]);
    failed.tools[0].status = "failed";
    failed.tools[0].error = "rejected";
    expect(
      deriveLatestTodoSnapshot([completed, failed])?.items[0].content,
    ).toBe("Old");

    const cleared = messageWithTodos("cleared", []);
    cleared.tools[0].result = { data: { phases: [] } };
    expect(deriveLatestTodoSnapshot([completed, cleared])).toEqual({
      items: [],
    });
    const clearedPhase = messageWithTodos("cleared-phase", []);
    clearedPhase.tools[0].result = {
      data: { phases: [{ name: "Tasks", tasks: [] }] },
    };
    clearedPhase.tools[0].content = "stale rendered content";
    expect(deriveLatestTodoSnapshot([completed, clearedPhase])).toEqual({
      items: [],
    });
  });

  it("shows a blocked reason accessibly when expanded", () => {
    const snapshot: TodoPanelSnapshot = {
      items: [
        {
          content: "Wait for dependency",
          status: "blocked",
          blocker: "dependency",
        },
      ],
    };
    render(<TodoPanel snapshot={snapshot} />);
    fireEvent.click(
      screen.getByRole("button", { name: /show current todos/i }),
    );
    expect(
      screen.getByRole("note", { name: "Blocked: dependency" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Blocked: dependency")).toBeInTheDocument();
  });

  it("renders the blocked status in Chinese", async () => {
    await i18n.changeLanguage("zh-CN");
    try {
      render(
        <TodoPanel
          snapshot={{
            items: [
              { content: "等待依赖", status: "blocked", blocker: "依赖" },
            ],
          }}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: /展开当前 TODO/i }));
      expect(screen.getByText("已阻塞")).toBeInTheDocument();
      expect(screen.getByText("已阻塞：依赖")).toBeInTheDocument();
    } finally {
      await i18n.changeLanguage("en");
    }
  });
});
