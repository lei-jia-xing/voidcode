import "../i18n";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Composer } from "./Composer";
import type { CommandSummary } from "../lib/runtime/types";

// Codex chat_composer.rs:3072-3094 parity: an unknown `/foo` never reaches the
// model — the composer shows the notice inline, keeps the draft, and blocks
// submit. `/`-in-name and bare `/` are literal text and pass through.
const nativeCommands: CommandSummary[] = [
  {
    name: "diff",
    description: "show the aggregate Git diff",
    source: "native",
    enabled: true,
    hidden: false,
    subtask: false,
  },
  {
    name: "status",
    description: "show session status",
    source: "native",
    enabled: true,
    hidden: false,
    subtask: false,
  },
];

function renderComposer(
  onSubmit: (message: string) => void,
  onNativeCommand?: (command: string) => Promise<boolean>,
) {
  render(
    <Composer
      disabled={false}
      isRunning={false}
      commands={nativeCommands}
      onSubmit={onSubmit}
      onNativeCommand={onNativeCommand}
    />,
  );
  return screen.getByPlaceholderText(/Ask VoidCode/) as HTMLTextAreaElement;
}

function submit(textarea: HTMLTextAreaElement, value: string) {
  // The Send button bypasses the slash-menu's Enter-to-complete capture, so
  // these tests exercise the submit guard rather than menu navigation.
  fireEvent.change(textarea, { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: /Send message/ }));
}

describe("composer unknown slash command guard", () => {
  it("blocks unknown /foo with a notice and preserves the draft", async () => {
    const onSubmit = vi.fn();
    const onNativeCommand = vi.fn(async () => false);
    const textarea = renderComposer(onSubmit, onNativeCommand);

    submit(textarea, "/foo some args");
    // Guard path is synchronous, but handleAction is async: flush a tick so
    // any stray submit would land before the not-called assertions.
    await vi.waitFor(() =>
      expect(
        screen.getByText(
          "Unrecognized command '/foo'. Type \"/\" for a list of supported commands.",
        ),
      ).toBeDefined(),
    );

    expect(onSubmit).not.toHaveBeenCalled();
    expect(onNativeCommand).not.toHaveBeenCalled();
    // Draft preserved exactly so the user can correct it.
    expect(textarea.value).toBe("/foo some args");
  });

  it("submits names containing / and bare / as literal text", async () => {
    const onSubmit = vi.fn();
    const onNativeCommand = vi.fn(async () => false);
    const textarea = renderComposer(onSubmit, onNativeCommand);

    submit(textarea, "/foo/bar");
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledWith("/foo/bar"));

    submit(textarea, "/");
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledWith("/"));
  });

  it("lets known native commands through to onNativeCommand", async () => {
    const onSubmit = vi.fn();
    const onNativeCommand = vi.fn(async () => true);
    const textarea = renderComposer(onSubmit, onNativeCommand);

    submit(textarea, "/diff");
    // handleSubmit awaits onNativeCommand before clearing the draft.
    await vi.waitFor(() =>
      expect(onNativeCommand).toHaveBeenCalledWith("/diff"),
    );

    expect(onSubmit).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(textarea.value).toBe(""));
  });
});
