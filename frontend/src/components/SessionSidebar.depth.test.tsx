import "../i18n";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SessionSidebar } from "./SessionSidebar";
import type { StoredSessionSummary } from "../lib/runtime/types";

// The sidebar mirrors the terminal surfaces' fork forest: `GET /api/sessions`
// carries the runtime's `session_forest` depth per row, and the sidebar indents
// by it. Membership is unchanged -- these are the rows the list returned -- and
// a row without a depth (not in the forest) renders at depth 0.

function summary(
  id: string,
  depth: number | null | undefined,
  updatedAt: number,
): StoredSessionSummary {
  return {
    session: { id },
    status: "completed",
    turn: 1,
    prompt: `prompt for ${id}`,
    updated_at: updatedAt,
    title: id,
    depth,
  };
}

function renderSidebar(sessions: StoredSessionSummary[]) {
  const onSelectSession = vi.fn();
  render(
    <SessionSidebar
      workspaces={null}
      sessions={sessions}
      currentSessionId={null}
      sidebarWidth={344}
      sessionsStatus="success"
      sessionsError={null}
      isRunning={false}
      isReplayLoading={false}
      onSidebarWidthChange={() => {}}
      onSelectSession={onSelectSession}
      onOpenProjects={() => {}}
      onOpenSettings={() => {}}
    />,
  );
  return onSelectSession;
}

function rowPadding(sessionId: string): string {
  const button = screen.getByText(sessionId).closest("button");
  if (button === null) throw new Error(`no row for ${sessionId}`);
  return button.style.paddingLeft;
}

describe("session sidebar fork indentation", () => {
  it("indents a forked pair by depth in the order the list returned", () => {
    renderSidebar([
      summary("root", 0, 3),
      summary("child", 1, 2),
      summary("grandchild", 2, 1),
    ]);

    expect(rowPadding("root")).toBe("12px");
    expect(rowPadding("child")).toBe("24px");
    expect(rowPadding("grandchild")).toBe("36px");

    const rendered = screen
      .getAllByText(/^(root|child|grandchild)$/)
      .map((node) => node.textContent);
    expect(rendered).toEqual(["root", "child", "grandchild"]);
  });

  it("renders the wire order even when it is not updated_at-descending", () => {
    // The runtime emits the tree order (roots by recency, parents before their
    // children). Here the parent is the *older* row, so a client-side
    // `updated_at` sort would flip the pair and put the child above the parent.
    // The sidebar must render what the server sent.
    renderSidebar([summary("root", 0, 1), summary("child", 1, 2)]);

    const rendered = screen
      .getAllByText(/^(root|child)$/)
      .map((node) => node.textContent);
    expect(rendered).toEqual(["root", "child"]);
  });

  it("renders a row with an absent depth at depth 0", () => {
    renderSidebar([
      summary("no-depth", undefined, 2),
      summary("null-depth", null, 1),
    ]);

    expect(rowPadding("no-depth")).toBe("12px");
    expect(rowPadding("null-depth")).toBe("12px");
  });

  it("still selects the clicked session with its id", async () => {
    const onSelectSession = renderSidebar([
      summary("root", 0, 2),
      summary("child", 1, 1),
    ]);

    await userEvent.click(screen.getByText("child"));

    expect(onSelectSession).toHaveBeenCalledTimes(1);
    expect(onSelectSession).toHaveBeenCalledWith("child");
  });
});
