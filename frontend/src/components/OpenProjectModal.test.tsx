import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { OpenProjectModal } from "./OpenProjectModal";
import "../i18n";

describe("OpenProjectModal", () => {
  const baseProps = {
    isOpen: true,
    onClose: vi.fn(),
    recentWorkspaces: [
      {
        path: "/recent",
        label: "Recent Project",
        available: true,
        current: false,
      },
    ],
    candidateWorkspaces: [
      {
        path: "/candidate",
        label: "Candidate Project",
        available: true,
        current: false,
      },
    ],
    workspacesStatus: "success",
    workspacesError: null,
    workspaceSwitchStatus: "idle",
    workspaceSwitchError: null,
    currentWorkspacePath: "/current",
    onSwitchWorkspace: vi.fn(() => Promise.resolve()),
  };

  it("closes modal on successful workspace switch", async () => {
    const onClose = vi.fn();
    const onSwitchWorkspace = vi.fn(() => Promise.resolve());
    const { rerender } = render(
      <OpenProjectModal
        {...baseProps}
        onClose={onClose}
        onSwitchWorkspace={onSwitchWorkspace}
      />,
    );

    fireEvent.click(screen.getByText("Recent Project"));
    await waitFor(() => expect(onSwitchWorkspace).toHaveBeenCalled());

    rerender(
      <OpenProjectModal
        {...baseProps}
        onClose={onClose}
        onSwitchWorkspace={onSwitchWorkspace}
        workspaceSwitchStatus="success"
      />,
    );

    expect(onClose).toHaveBeenCalled();
  });

  it("keeps modal open and shows error on failed workspace switch", async () => {
    const onClose = vi.fn();
    const onSwitchWorkspace = vi.fn(() => Promise.resolve());
    const { rerender } = render(
      <OpenProjectModal
        {...baseProps}
        onClose={onClose}
        onSwitchWorkspace={onSwitchWorkspace}
      />,
    );

    fireEvent.click(screen.getByText("Candidate Project"));
    await waitFor(() => expect(onSwitchWorkspace).toHaveBeenCalled());

    rerender(
      <OpenProjectModal
        {...baseProps}
        onClose={onClose}
        onSwitchWorkspace={onSwitchWorkspace}
        workspaceSwitchStatus="error"
        workspaceSwitchError="invalid workspace"
      />,
    );

    expect(onClose).not.toHaveBeenCalled();
    expect(
      screen.getByText("Failed to switch project: invalid workspace"),
    ).toBeInTheDocument();
  });

  it("opens an explicitly entered local folder path", async () => {
    const onSwitchWorkspace = vi.fn(() => Promise.resolve());
    render(
      <OpenProjectModal {...baseProps} onSwitchWorkspace={onSwitchWorkspace} />,
    );

    fireEvent.change(screen.getByPlaceholderText("Enter local folder path"), {
      target: { value: "/tmp/project" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Open folder" }));

    await waitFor(() =>
      expect(onSwitchWorkspace).toHaveBeenCalledWith("/tmp/project"),
    );
  });

  it("caps the visible recent workspace list to five entries", () => {
    const recentWorkspaces = Array.from({ length: 7 }, (_, index) => ({
      path: `/recent-${index}`,
      label: `Recent Project ${index}`,
      available: true,
      current: false,
    }));

    render(
      <OpenProjectModal {...baseProps} recentWorkspaces={recentWorkspaces} />,
    );

    expect(screen.getByText("Recent Project 0")).toBeInTheDocument();
    expect(screen.getByText("Recent Project 4")).toBeInTheDocument();
    expect(screen.queryByText("Recent Project 5")).not.toBeInTheDocument();
    expect(screen.queryByText("Recent Project 6")).not.toBeInTheDocument();
  });
});
