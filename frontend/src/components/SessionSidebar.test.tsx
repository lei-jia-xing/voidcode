import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import "../i18n";
import { SessionSidebar } from "./SessionSidebar";
import type { SessionSidebarProps } from "./SessionSidebar";

const MIN_SESSION_SIDEBAR_WIDTH = 244;
const MAX_SESSION_SIDEBAR_WIDTH = 520;

function getMaxSessionSidebarWidth(viewportWidth: number): number {
  return Math.min(MAX_SESSION_SIDEBAR_WIDTH, viewportWidth * 0.3 + 64);
}

function setViewportWidth(width: number) {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: width,
  });
  window.dispatchEvent(new Event("resize"));
}

const baseProps: SessionSidebarProps = {
  workspaces: {
    current: {
      path: "/workspace",
      label: "workspace",
      available: true,
      current: true,
      last_opened_at: 1,
    },
    recent: [],
    candidates: [],
  },
  sessions: [],
  currentSessionId: null,
  sidebarWidth: 344,
  sessionsStatus: "success",
  sessionsError: null,
  isRunning: false,
  isReplayLoading: false,
  onSidebarWidthChange: vi.fn(),
  onSelectSession: vi.fn(),
  onOpenProjects: vi.fn(),
  onOpenSettings: vi.fn(),
};

function renderSidebar(props: Partial<SessionSidebarProps> = {}) {
  const onSidebarWidthChange = vi.fn();
  const result = render(
    <SessionSidebar
      {...baseProps}
      {...props}
      onSidebarWidthChange={onSidebarWidthChange}
    />,
  );
  return { ...result, onSidebarWidthChange };
}

describe("SessionSidebar resizing", () => {
  afterEach(() => {
    setViewportWidth(1024);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    vi.restoreAllMocks();
  });

  it("uses the default expanded width token through the desktop CSS variable", () => {
    const { container } = renderSidebar();

    expect(container.querySelector("aside")).toHaveStyle({
      "--session-sidebar-width": "344px",
    });
  });

  it("clamps invalid persisted widths to the current viewport maximum", async () => {
    setViewportWidth(1200);
    const { onSidebarWidthChange } = renderSidebar({ sidebarWidth: 9999 });

    await waitFor(() =>
      expect(onSidebarWidthChange).toHaveBeenCalledWith(
        getMaxSessionSidebarWidth(1200),
      ),
    );
  });

  it("supports pointer dragging and restores document selection after resize", async () => {
    setViewportWidth(1440);
    const { onSidebarWidthChange } = renderSidebar();
    const handle = screen.getByRole("separator", {
      name: "Resize session sidebar",
    });

    fireEvent.pointerDown(handle, { clientX: 344 });

    await waitFor(() => expect(document.body.style.userSelect).toBe("none"));
    fireEvent.pointerMove(window, { clientX: 420 });
    expect(onSidebarWidthChange).toHaveBeenCalledWith(420);

    fireEvent.pointerUp(window);

    await waitFor(() => expect(document.body.style.userSelect).toBe(""));
    expect(document.body.style.cursor).toBe("");
  });

  it("supports ArrowLeft ArrowRight Home and End keyboard resizing", () => {
    setViewportWidth(1200);
    const { onSidebarWidthChange } = renderSidebar();
    const handle = screen.getByRole("separator", {
      name: "Resize session sidebar",
    });

    fireEvent.keyDown(handle, { key: "ArrowRight" });
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    fireEvent.keyDown(handle, { key: "Home" });
    fireEvent.keyDown(handle, { key: "End" });

    expect(onSidebarWidthChange).toHaveBeenCalledWith(360);
    expect(onSidebarWidthChange).toHaveBeenCalledWith(328);
    expect(onSidebarWidthChange).toHaveBeenCalledWith(
      MIN_SESSION_SIDEBAR_WIDTH,
    );
    expect(onSidebarWidthChange).toHaveBeenCalledWith(
      getMaxSessionSidebarWidth(1200),
    );
  });

  it("preserves collapsed rail behavior and hides the resize handle", () => {
    const { container } = renderSidebar({
      sidebarWidth: MAX_SESSION_SIDEBAR_WIDTH,
    });

    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));

    expect(screen.queryByRole("separator")).not.toBeInTheDocument();
    expect(container.querySelector("aside")).toHaveClass("w-16", "md:w-16");
  });
});
