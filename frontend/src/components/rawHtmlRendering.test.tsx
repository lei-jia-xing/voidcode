import { render } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ChatThread } from "./ChatThread";
import { deriveChatMessages } from "../lib/runtime/event-parser";
import type { EventEnvelope } from "../lib/runtime/types";
import "../i18n";

// react-markdown is deliberately NOT mocked here: this file pins the real
// render path the app uses for model text and fetched page bodies. The web
// transcript must never turn a fetched page's raw HTML into live elements.
vi.mock("@chenglou/pretext", () => ({
  prepare: vi.fn((text: string) => ({ text })),
  layout: vi.fn(() => ({ height: 46, lineCount: 2 })),
}));

const RAW_HTML_BODY = [
  "Fetched page body:",
  "",
  "<script>window.__xss = true; alert('xss')</script>",
  '<div data-evil="1">EVIL-DIV</div>',
  '<img data-evil="2" src="x" onerror="window.__xss = true" />',
  "",
  "tail text",
].join("\n");

function fetchEvents(): EventEnvelope[] {
  return [
    {
      session_id: "session-1",
      sequence: 1,
      event_type: "runtime.request_received",
      source: "runtime",
      payload: { prompt: "fetch the page" },
    },
    {
      session_id: "session-1",
      sequence: 2,
      event_type: "graph.tool_request_created",
      source: "graph",
      payload: {
        tool: "web_fetch",
        tool_call_id: "call-fetch",
        arguments: { url: "https://example.test/page" },
        tool_status: {
          invocation_id: "call-fetch",
          tool_name: "web_fetch",
          phase: "completed",
          status: "completed",
          display: {
            kind: "web",
            title: "Fetch",
            summary: "https://example.test/page",
          },
        },
      },
    },
    {
      session_id: "session-1",
      sequence: 3,
      event_type: "runtime.tool_completed",
      source: "runtime",
      payload: {
        tool: "web_fetch",
        tool_call_id: "call-fetch",
        arguments: { url: "https://example.test/page" },
        url: "https://example.test/page",
        status: 200,
        content: RAW_HTML_BODY,
        status_ok: true,
        tool_status: {
          invocation_id: "call-fetch",
          tool_name: "web_fetch",
          phase: "completed",
          status: "completed",
          display: {
            kind: "web",
            title: "Fetch",
            summary: "https://example.test/page",
          },
        },
      },
    },
    {
      session_id: "session-1",
      sequence: 4,
      event_type: "graph.response_ready",
      source: "graph",
      payload: {
        output_preview: `Here is what the page said:\n\n${RAW_HTML_BODY}`,
      },
    },
  ];
}

describe("raw HTML in the transcript", () => {
  it("renders fetched raw HTML as visible text and creates no elements", () => {
    const messages = deriveChatMessages(fetchEvents(), null, "session-1");
    const { container } = render(
      <ChatThread
        messages={messages}
        isRunning={false}
        isWaitingApproval={false}
        isApprovalSubmitting={false}
        approvalError={null}
        onResolveApproval={vi.fn()}
      />,
    );

    // No element from the payload may exist anywhere in the transcript, by tag
    // name or by attribute: an executed `<script>`/`<img onerror>` or a live
    // `<div>` would show up here.
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[data-evil]")).toBeNull();
    expect((window as unknown as { __xss?: boolean }).__xss).toBeUndefined();

    // The literal text is what the reader sees instead.
    const text = container.textContent ?? "";
    expect(text).toContain(
      "<script>window.__xss = true; alert('xss')</script>",
    );
    expect(text).toContain('<div data-evil="1">EVIL-DIV</div>');
    expect(text).toContain(
      '<img data-evil="2" src="x" onerror="window.__xss = true" />',
    );
    expect(text).toContain("tail text");
  });
});
