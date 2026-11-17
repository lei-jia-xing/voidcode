import { describe, it, expect } from "vitest";
import type {
  ArtifactReference,
  ToolOutput,
  ToolResult,
} from "../lib/core/types";
import { artifactSources, toolOutputParts, toolPartText } from "./toolOutput";

const PNG_REF: ArtifactReference = {
  sha256: "a".repeat(64),
  bytes: 128,
  media_type: "image/png",
};
const JPG_REF: ArtifactReference = {
  sha256: "b".repeat(64),
  bytes: 256,
  media_type: "image/jpeg",
};
const JSON_REF: ArtifactReference = {
  sha256: "c".repeat(64),
  bytes: 12,
  media_type: "application/json",
};

function composite(): ToolOutput {
  return {
    type: "composite",
    parts: [
      { kind: "text", text: "explanation here" },
      {
        kind: "table",
        columns: ["a", "b"],
        rows: [
          ["1", "2"],
          ["3", "4"],
        ],
      },
      {
        kind: "media",
        reference: PNG_REF,
        mime: "image/png",
        presentation: "first chart",
      },
      {
        kind: "media",
        reference: JPG_REF,
        mime: "image/jpeg",
        presentation: "second chart",
      },
      { kind: "json", reference: JSON_REF, preview: '{"ok": true}' },
    ],
  };
}

describe("toolOutputParts single variants", () => {
  it("preserves text, attachment, and empty behavior", () => {
    expect(
      toolOutputParts({ type: "text", text: "body", presentation: "shown" }),
    ).toEqual(["shown"]);
    expect(toolOutputParts({ type: "text", text: "body" })).toEqual(["body"]);
    expect(
      toolOutputParts({
        type: "attachment",
        mime: "image/png",
        data_uri: null,
      }),
    ).toEqual(["[image/png]"]);
    expect(toolOutputParts({ type: "empty", presentation: "sum" })).toEqual([
      "sum",
    ]);
    expect(toolOutputParts({ type: "empty" })).toEqual([]);
  });

  it("marks unknown output types explicitly", () => {
    expect(
      toolOutputParts({ type: "mystery" } as unknown as ToolOutput),
    ).toEqual(['[unknown output type: "mystery"]']);
  });
});

describe("toolPartText", () => {
  it("renders ordered parts with exact degraded strings", () => {
    const output = composite();
    if (output.type !== "composite") throw new Error("fixture broke");
    const blocks = output.parts.map(toolPartText);
    expect(blocks[0]).toBe("explanation here");
    expect(blocks[1]).toBe("a\tb\n1\t2\n3\t4");
    expect(blocks[2]).toBe(
      `[Image: image/png, 128 bytes, artifact://${PNG_REF.sha256}] first chart`,
    );
    expect(blocks[3]).toBe(
      `[Image: image/jpeg, 256 bytes, artifact://${JPG_REF.sha256}] second chart`,
    );
    expect(blocks[4]).toBe(
      `{"ok": true}\n[full JSON: artifact://${JSON_REF.sha256}]`,
    );
  });

  it("labels audio and generic media explicitly", () => {
    expect(
      toolPartText({
        kind: "media",
        reference: {
          sha256: "d".repeat(64),
          bytes: 10,
          media_type: "audio/wav",
        },
        mime: "audio/wav",
        presentation: "clip",
      }),
    ).toBe(`[Audio: audio/wav, 10 bytes, artifact://${"d".repeat(64)}] clip`);
    expect(
      toolPartText({
        kind: "media",
        reference: {
          sha256: "e".repeat(64),
          bytes: 4,
          media_type: "application/octet-stream",
        },
        mime: "application/octet-stream",
        presentation: "blob",
      }),
    ).toBe(
      `[Media: application/octet-stream, 4 bytes, artifact://${"e".repeat(64)}] blob`,
    );
  });

  it("annotates text references, captions, and truncation", () => {
    expect(
      toolPartText({ kind: "text", text: "prefix", reference: JSON_REF }),
    ).toBe(`prefix\n[full text: artifact://${JSON_REF.sha256}]`);
    expect(
      toolPartText({
        kind: "table",
        columns: ["id"],
        rows: [["1"]],
        total_rows: 9,
        reference: JSON_REF,
        presentation: "Top rows",
      }),
    ).toBe(
      `Top rows\nid\n1\n[showing 1/9 rows; full table: artifact://${JSON_REF.sha256}]`,
    );
    expect(toolPartText({ kind: "table", columns: [], rows: [] })).toBe(
      "[table: missing columns]",
    );
    expect(toolPartText({ kind: "table", columns: ["a"] })).toBe("a");
  });

  it("marks unknown part kinds explicitly", () => {
    expect(
      toolPartText({ kind: "mystery" } as unknown as Parameters<
        typeof toolPartText
      >[0]),
    ).toBe('[unknown part kind: "mystery"]');
  });
});

describe("artifactSources", () => {
  function result(): ToolResult {
    return {
      status: "ok",
      tool_name: "mcp",
      output: composite(),
      body: null,
      result_reference: {
        sha256: "f".repeat(64),
        bytes: 99,
        media_type: "application/json",
      },
    };
  }

  it("discovers part references in order before the complete result", () => {
    const sources = artifactSources(result());
    expect(sources.map((source) => source.label)).toEqual([
      "Part 2 (media)",
      "Part 3 (media)",
      "Part 4 (json)",
      "Complete structured result JSON",
    ]);
    expect(sources.map((source) => source.sha256)).toEqual([
      PNG_REF.sha256,
      JPG_REF.sha256,
      JSON_REF.sha256,
      "f".repeat(64),
    ]);
    expect(sources[0].reference?.bytes).toBe(128);
  });

  it("dedups the top-level reference before parts", () => {
    const found = result();
    if (found.output.type !== "composite") throw new Error("fixture broke");
    found.output.bounds = { reference: PNG_REF };
    const sources = artifactSources(found);
    expect(sources[0].label).toBe("Original output source");
    expect(
      sources.filter((source) => source.sha256 === PNG_REF.sha256),
    ).toHaveLength(1);
  });

  it("still scans the body and ignores malformed references", () => {
    const found = result();
    found.body = {
      nested: [
        { sha256: "0".repeat(64), bytes: 7, media_type: "text/plain" },
        { sha256: "not-a-digest", bytes: 1, media_type: "text/plain" },
      ],
    } as unknown as ToolResult["body"];
    const sources = artifactSources(found);
    expect(sources.some((source) => source.sha256 === "0".repeat(64))).toBe(
      true,
    );
    expect(sources.some((source) => source.sha256 === "not-a-digest")).toBe(
      false,
    );
  });
});
