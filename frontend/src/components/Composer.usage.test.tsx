import "../i18n";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Composer } from "./Composer";
import type { AgentSummary } from "../lib/runtime/types";

// The composer's context line is where the session's provider usage is rendered, so
// the priced/unpriced distinction has to hold there: an unpriced model reports no
// cost rather than reading as free.
const agent: AgentSummary = {
  id: "default",
  label: "Default",
  configured: true,
  selectable: true,
  fallback_chain: [],
};

function renderUsage(costUsd: number | null): string {
  render(
    <Composer
      disabled={false}
      isRunning={false}
      agentPresets={[agent]}
      sessionContextUsage={{
        usedTokens: 24_000,
        contextWindow: 200_000,
        totalTokens: 1_500_000,
        cacheHitRate: 0.5,
        costUsd,
      }}
      onSubmit={() => {}}
    />,
  );
  return screen.getByText(/total/).textContent ?? "";
}

describe("composer session usage line", () => {
  it("shows the session's cost next to its token totals", () => {
    const label = renderUsage(1.503);

    expect(label).toContain("1.5M total");
    expect(label).toContain("cache 50%");
    expect(label).toContain("$1.50 spent");
  });

  it("omits the cost when the model has no shipped rates", () => {
    const label = renderUsage(null);

    expect(label).toContain("1.5M total");
    expect(label).not.toContain("$");
  });
});
