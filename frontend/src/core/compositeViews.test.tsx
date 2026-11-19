import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { TableView } from "./views/TableView";
import { JsonView } from "./views/JsonView";
import { MediaView } from "./views/MediaView";
import { PluginViewRenderer } from "./views/PluginViewRenderer";
import type {
  JsonViewData,
  MediaViewData,
  PluginViewDescriptor,
  TableViewData,
} from "../lib/core/types";
import type { CoreClient } from "../lib/core/client";
import "../i18n";

const DISPLAYED_REVISION = "rev-displayed-1";

describe("TableView", () => {
  it("renders header cells, rows, and the truncation note with its reference", () => {
    const data: TableViewData = {
      columns: ["id", "name"],
      rows: [
        ["1", "ada"],
        ["2", "bob"],
      ],
      total_rows: 7,
      reference: {
        sha256: "c".repeat(64),
        bytes: 90,
        media_type: "application/json",
      },
      style: null,
    };
    render(<TableView data={data} />);
    const view = screen.getByTestId("table-view");
    expect(view.querySelectorAll("th")).toHaveLength(2);
    expect(view).toHaveTextContent("ada");
    expect(view).toHaveTextContent("bob");
    expect(view.textContent).toContain(
      `[showing 2/7 rows; full table: artifact://${"c".repeat(64)}]`,
    );
  });

  it("omits the note when the inline rows are complete", () => {
    render(<TableView data={{ columns: ["a"], rows: [["1"]], style: null }} />);
    expect(screen.getByTestId("table-view").textContent).not.toContain(
      "showing",
    );
  });
});

describe("JsonView", () => {
  it("renders the preview and the complete-data reference", () => {
    const data: JsonViewData = {
      preview: '{"a": 1}',
      reference: {
        sha256: "c".repeat(64),
        bytes: 12,
        media_type: "application/json",
      },
      style: null,
    };
    render(<JsonView data={data} />);
    const view = screen.getByTestId("json-view");
    expect(view).toHaveTextContent('{"a": 1}');
    expect(view.textContent).toContain(
      `[full JSON: artifact://${"c".repeat(64)}]`,
    );
  });
});

describe("MediaView", () => {
  it("renders the exact imageless placeholder", () => {
    const data: MediaViewData = {
      reference: {
        sha256: "a".repeat(64),
        bytes: 128,
        media_type: "image/png",
      },
      mime: "image/png",
      alt: "chart",
      style: null,
    };
    render(<MediaView data={data} />);
    expect(screen.getByTestId("media-view").textContent).toContain(
      `[Image: image/png, 128 bytes, artifact://${"a".repeat(64)}] chart`,
    );
  });

  it("labels audio explicitly", () => {
    render(
      <MediaView
        data={{
          reference: {
            sha256: "d".repeat(64),
            bytes: 10,
            media_type: "audio/wav",
          },
          mime: "audio/wav",
          alt: "clip",
          style: null,
        }}
      />,
    );
    expect(screen.getByTestId("media-view").textContent).toContain(
      `[Audio: audio/wav, 10 bytes, artifact://${"d".repeat(64)}] clip`,
    );
  });
});

describe("PluginViewRenderer composite kinds", () => {
  const mockClient = { invoke: vi.fn() } as unknown as CoreClient;
  const cases: Array<{
    view_type: PluginViewDescriptor["view_type"];
    data: PluginViewDescriptor["data"];
    testId: string;
  }> = [
    {
      view_type: "table",
      data: { columns: ["a"], rows: [["1"]], style: null },
      testId: "table-view",
    },
    {
      view_type: "json",
      data: { preview: "{}", style: null },
      testId: "json-view",
    },
    {
      view_type: "media",
      data: {
        reference: {
          sha256: "a".repeat(64),
          bytes: 1,
          media_type: "image/png",
        },
        mime: "image/png",
        alt: "x",
        style: null,
      },
      testId: "media-view",
    },
  ];

  it.each(cases)("dispatches $view_type", ({ view_type, data, testId }) => {
    render(
      <PluginViewRenderer
        descriptor={{
          owner: "test",
          name: "test_view",
          title: "Test",
          view_type,
          data,
        }}
        sessionId="s1"
        revisionId={DISPLAYED_REVISION}
        getClient={() => mockClient}
        catalog={[]}
      />,
    );
    expect(screen.getByTestId(testId)).toBeVisible();
  });
});
