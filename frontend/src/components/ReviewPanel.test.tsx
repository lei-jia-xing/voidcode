import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ReviewPanel } from "./ReviewPanel";
import "../i18n";

const baseProps = {
  isOpen: true,
  surface: "code-review" as const,
  snapshot: {
    root: "/workspace",
    git: { state: "git_ready" as const, root: "/workspace" },
    changed_files: [{ path: "src/app.ts", change_type: "modified" as const }],
    tree: [
      {
        kind: "file" as const,
        name: "app.ts",
        path: "src/app.ts",
        changed: true,
        children: [],
      },
    ],
  },
  status: "success" as const,
  error: null,
  selectedPath: "src/app.ts",
  diff: {
    root: "/workspace",
    path: "src/app.ts",
    state: "changed" as const,
    diff: "--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-const value = 'old'\n+const value = 'new'",
  },
  diffStatus: "success" as const,
  diffError: null,
  onClose: vi.fn(),
  onRefresh: vi.fn(),
  onSelectPath: vi.fn(),
};

describe("ReviewPanel", () => {
  it("renders wrapped preformatted diff text", () => {
    render(<ReviewPanel {...baseProps} />);

    const diff = screen.getByText(/const value = 'new'/);
    expect(diff).toHaveClass("whitespace-pre-wrap");
    expect(diff).toHaveClass("break-words");
    expect(diff).toHaveClass("overflow-x-hidden");
  });

  it("allows resizing the review panel from the left edge", () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1200,
    });
    const { container } = render(<ReviewPanel {...baseProps} />);
    const panel = container.querySelector("aside");
    expect(panel).toHaveStyle({ width: "384px" });

    fireEvent.pointerDown(screen.getByLabelText("Resize code review panel"), {
      clientX: 500,
    });
    fireEvent.pointerMove(window, { clientX: 300 });
    fireEvent.pointerUp(window);

    expect(panel).toHaveStyle({ width: "900px" });
  });

  it("restores existing body resize styles after dragging", () => {
    document.body.style.cursor = "wait";
    document.body.style.userSelect = "text";
    render(<ReviewPanel {...baseProps} />);

    fireEvent.pointerDown(screen.getByLabelText("Resize code review panel"), {
      clientX: 700,
    });
    expect(document.body.style.cursor).toBe("col-resize");
    expect(document.body.style.userSelect).toBe("none");

    fireEvent.pointerUp(window);

    expect(document.body.style.cursor).toBe("wait");
    expect(document.body.style.userSelect).toBe("text");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  });

  it("selects nested file tree paths without altering the path", () => {
    const onSelectPath = vi.fn();
    render(
      <ReviewPanel
        {...baseProps}
        surface="file-tree"
        selectedPath={null}
        onSelectPath={onSelectPath}
        snapshot={{
          ...baseProps.snapshot,
          tree: [
            {
              kind: "directory" as const,
              name: "src",
              path: "src",
              changed: false,
              children: [
                {
                  kind: "file" as const,
                  name: "app file #1.ts",
                  path: "src/app file #1.ts",
                  changed: false,
                  children: [],
                },
              ],
            },
          ],
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "app file #1.ts" }));

    expect(onSelectPath).toHaveBeenCalledWith("src/app file #1.ts");
  });

  it("renders file tree as navigation without diff review or internal mode tabs", () => {
    render(<ReviewPanel {...baseProps} surface="file-tree" />);

    expect(
      screen.getByRole("complementary", { name: "File Tree" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "app.ts" })).toBeInTheDocument();
    expect(screen.queryByText(/const value = 'new'/)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Changes" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Files" }),
    ).not.toBeInTheDocument();
  });

  it("renders code review as changed files plus diff without full file tree mode", () => {
    render(<ReviewPanel {...baseProps} />);

    expect(
      screen.getByRole("complementary", { name: "Code Review" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "M src/app.ts" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/const value = 'new'/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "app.ts" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Changes" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Files" }),
    ).not.toBeInTheDocument();
  });
});
