import "../i18n";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../lib/runtime/event-parser";
import { ChatThread } from "./ChatThread";

const inProgressMessage: ChatMessage = {
  id: "assistant-1",
  role: "assistant",
  content: "partial output",
  thinking: [],
  tools: [],
  approval: null,
  status: "in_progress",
  sequence: 2,
  parts: [{ kind: "text", sequence: 2, text: "partial output" }],
};

describe("ChatThread stream state", () => {
  it("does not imply a live response stream after EOF while runtime still runs", () => {
    const { container } = render(
      <ChatThread
        messages={[inProgressMessage]}
        sessionStatus="running"
        isRunning={true}
        isLiveStreamActive={false}
        isWaitingApproval={false}
        isApprovalSubmitting={false}
        approvalError={null}
        onResolveApproval={() => undefined}
      />,
    );

    expect(screen.queryByText("Responding...")).toBeNull();
    expect(container.querySelector(".animate-spin")).toBeNull();
  });
});
