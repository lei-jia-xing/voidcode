import "../i18n";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Composer } from "./Composer";
import type { CommandSummary } from "../lib/runtime/types";

describe("composer slash command menu", () => {
  it("renders native commands provided by the commands catalog API", () => {
    const mockCommands: CommandSummary[] = [
      {
        name: "init",
        description:
          "create an AGENTS.md contributor guide for this repository",
        source: "native",
        enabled: true,
        hidden: false,
        subtask: false,
      },
      {
        name: "compact",
        description: "summarize and persist the current conversation context",
        source: "native",
        enabled: true,
        hidden: false,
        subtask: false,
      },
      {
        name: "custom",
        description: "custom prompt command",
        source: "project",
        enabled: true,
        hidden: false,
        subtask: false,
      },
    ];

    render(
      <Composer
        disabled={false}
        isRunning={false}
        commands={mockCommands}
        onSubmit={() => {}}
      />,
    );

    const textarea = screen.getByPlaceholderText(/Ask VoidCode/);
    // Type slash to trigger menu
    fireEvent.change(textarea, { target: { value: "/" } });

    // Verify only native commands from catalog are rendered
    expect(screen.getByText("/init")).toBeDefined();
    expect(screen.getByText("/compact")).toBeDefined();
    expect(screen.queryByText("/custom")).toBeNull();
    expect(screen.getByText(/create an AGENTS\.md/)).toBeDefined();
  });
});
