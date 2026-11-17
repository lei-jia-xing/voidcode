import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ToolResultCard } from "./CoreApp";
import type { CoreClient } from "../lib/core/client";
import type { TranscriptTool } from "../lib/core/types";

const PNG_SHA = "a".repeat(64);
const JPG_SHA = "b".repeat(64);
const TEXT_SHA = "c".repeat(64);

function compositeTool(overrides: {
  status?: "ok" | "error";
  error?: string | null;
  interrupted?: boolean;
  bounds?: Record<string, unknown>;
}): TranscriptTool {
  return {
    call: { tool_name: "mcp", arguments: {}, tool_call_id: "call-1" },
    result: {
      status: overrides.status ?? "ok",
      tool_name: "mcp",
      output: {
        type: "composite",
        parts: [
          { kind: "text", text: "explanation here" },
          {
            kind: "table",
            columns: ["a", "b"],
            rows: [["1", "2"]],
          },
          {
            kind: "media",
            reference: { sha256: PNG_SHA, bytes: 128, media_type: "image/png" },
            mime: "image/png",
            presentation: "first chart",
          },
          {
            kind: "media",
            reference: {
              sha256: JPG_SHA,
              bytes: 256,
              media_type: "image/jpeg",
            },
            mime: "image/jpeg",
            presentation: "second chart",
          },
          {
            kind: "json",
            reference: {
              sha256: TEXT_SHA,
              bytes: 12,
              media_type: "application/json",
            },
            preview: '{"ok": true}',
          },
        ],
        bounds: overrides.bounds ?? {},
      },
      body: null,
      error: overrides.error ?? null,
    },
    interrupted: overrides.interrupted ?? false,
    batch_sequence: 1,
    deferred: false,
    icon: null,
  } as unknown as TranscriptTool;
}

const t = (key: string) => key;

describe("ToolResultCard composite", () => {
  it("renders ordered blocks with exact degraded strings", () => {
    const client = {} as unknown as CoreClient;
    render(
      <ToolResultCard
        evSequence={7}
        tool={compositeTool({})}
        client={client}
        sessionId="s"
        revisionId="rev"
        coreStatus="ok"
        t={t}
      />,
    );
    const card = screen.getByTestId("event-7");
    expect(card.textContent).toContain("✔");
    const parts = screen.getByTestId("event-7-parts");
    const text = parts.textContent ?? "";
    expect(text.indexOf("explanation here")).toBeLessThan(
      text.indexOf("first chart"),
    );
    expect(text.indexOf("first chart")).toBeLessThan(
      text.indexOf("second chart"),
    );
    expect(text).toContain(
      `[Image: image/png, 128 bytes, artifact://${PNG_SHA}] first chart`,
    );
    expect(text).toContain('{"ok": true}');
    expect(card.textContent).not.toContain("✖");
  });

  it("shows ordered parts plus failure diagnosis, never as success", () => {
    const client = {} as unknown as CoreClient;
    render(
      <ToolResultCard
        evSequence={8}
        tool={compositeTool({ status: "error", error: "boom happened" })}
        client={client}
        sessionId="s"
        revisionId="rev"
        coreStatus="ok"
        t={t}
      />,
    );
    const card = screen.getByTestId("event-8");
    expect(card.textContent).toContain("✖");
    expect(card.textContent).not.toContain("✔");
    expect(card.textContent).toContain("explanation here");
    expect(card.textContent).toContain("boom happened");
  });

  it("shows cancelled partials as interrupted, never success", () => {
    const client = {} as unknown as CoreClient;
    render(
      <ToolResultCard
        evSequence={9}
        tool={compositeTool({ interrupted: true })}
        client={client}
        sessionId="s"
        revisionId="rev"
        coreStatus="ok"
        t={t}
      />,
    );
    const card = screen.getByTestId("event-9");
    expect(card.textContent).toContain("✖");
    expect(card.textContent).toContain("explanation here");
    expect(card.textContent).toContain("Interrupted");
  });

  it("keeps single-variant inline rendering and the truncated tag", () => {
    const client = {} as unknown as CoreClient;
    const tool = {
      call: { tool_name: "read", arguments: {}, tool_call_id: null },
      result: {
        status: "ok",
        tool_name: "read",
        output: {
          type: "text",
          text: "inline body",
          bounds: { truncated: true },
        },
        body: null,
      },
      interrupted: false,
      batch_sequence: 1,
      deferred: false,
      icon: null,
    } as unknown as TranscriptTool;
    render(
      <ToolResultCard
        evSequence={10}
        tool={tool}
        client={client}
        sessionId="s"
        revisionId="rev"
        coreStatus="ok"
        t={t}
      />,
    );
    expect(screen.getByTestId("event-10").textContent).toContain("inline body");
    expect(screen.queryByTestId("event-10-parts")).toBeNull();
    expect(screen.getByTestId("event-10-truncated-tag")).toBeVisible();
  });

  it("lists part sources in order and pages the selected source bytes", async () => {
    const full = new TextEncoder().encode("0123456789ab");
    const readArtifact = vi.fn(async (sha256: string, { offset = 0 } = {}) => {
      if (sha256 === TEXT_SHA) {
        const data = full.slice(offset, offset + 8);
        return {
          artifact: {
            sha256,
            bytes: full.length,
            media_type: "application/json",
          },
          offset,
          data,
          eof: offset + data.byteLength >= full.length,
        };
      }
      const bytes = sha256 === PNG_SHA ? 128 : 256;
      const media_type = sha256 === PNG_SHA ? "image/png" : "image/jpeg";
      const data = new Uint8Array(Math.min(8, bytes - offset));
      return {
        artifact: { sha256, bytes, media_type },
        offset,
        data,
        eof: offset + data.byteLength >= bytes,
      };
    });
    const client = { readArtifact } as unknown as CoreClient;
    render(
      <ToolResultCard
        evSequence={11}
        tool={compositeTool({})}
        client={client}
        sessionId="s"
        revisionId="rev"
        coreStatus="ok"
        t={t}
      />,
    );
    fireEvent.click(screen.getByTestId("event-11-toggle-output"));
    const viewer = await screen.findByTestId("event-11-full-output");
    const options = within(viewer)
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(options.slice(0, 3)).toEqual([
      "Part 2 (media)",
      "Part 3 (media)",
      "Part 4 (json)",
    ]);
    const select = within(viewer).getByRole("combobox") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "2" } });
    const content = await within(viewer).findByTestId("artifact-page-content");
    expect(content.textContent).toBe("01234567");
    expect(readArtifact).toHaveBeenCalledWith(
      TEXT_SHA,
      expect.objectContaining({ offset: 0 }),
    );
    fireEvent.click(within(viewer).getByRole("button", { name: "Next" }));
    await screen.findByText("89ab");
    const offsets = readArtifact.mock.calls
      .filter((call) => call[0] === TEXT_SHA)
      .map((call) => (call[1] as { offset: number }).offset);
    expect(offsets).toEqual([0, 8]);
  });
});
