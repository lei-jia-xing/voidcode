import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ReviewPanel } from "./ReviewPanel";
import "../i18n";

const baseProps = {
  isOpen: true,
  mode: "changes" as const,
  snapshot: {
    root: "/workspace",
    git: { state: "git_ready" as const, root: "/workspace" },
    changed_files: [{ path: "src/app.ts", change_type: "modified" as const }],
    tree: [],
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
  onModeChange: vi.fn(),
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

    fireEvent.pointerDown(screen.getByLabelText("Resize review panel"), {
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

    fireEvent.pointerDown(screen.getByLabelText("Resize review panel"), {
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
});
