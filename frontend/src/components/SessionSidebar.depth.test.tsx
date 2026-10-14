import "../i18n";
import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { SessionSidebar } from "./SessionSidebar";
import type { StoredSessionSummary } from "../lib/runtime/types";

function summary(id: string, updatedAt: number): StoredSessionSummary {
  return {
    session: { id },
    status: "completed",
    turn: 1,
    prompt: `prompt for ${id}`,
    updated_at: updatedAt,
    title: id,
  };
}

it("renders the wire order even when it is not updated_at-descending", () => {
  // The runtime emits the tree order (roots by recency, parents before their
  // children). Here the parent is the older row, so a client-side sort would
  // put the child above it. The sidebar must render what the server sent.
  render(
    <SessionSidebar
      workspaces={null}
      sessions={[summary("root", 1), summary("child", 2)]}
      currentSessionId={null}
      sidebarWidth={344}
      sessionsStatus="success"
      sessionsError={null}
      isRunning={false}
      isReplayLoading={false}
      onSidebarWidthChange={() => {}}
      onSelectSession={() => {}}
      onOpenProjects={() => {}}
      onOpenSettings={() => {}}
    />,
  );

  const rendered = screen
    .getAllByText(/^(root|child)$/)
    .map((node) => node.textContent);
  expect(rendered).toEqual(["root", "child"]);
});
