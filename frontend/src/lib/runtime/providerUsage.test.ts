import { describe, expect, it } from "vitest";
import { providerCostUsd } from "./providerUsage";

describe("provider cost", () => {
  it("reads the session total, preferring the cumulative figure", () => {
    expect(
      providerCostUsd({
        provider_usage: {
          latest: { cost_usd: 0.25 },
          cumulative: { cost_usd: 1.75 },
        },
      }),
    ).toBe(1.75);
  });

  it("separates an unpriced session from a free one", () => {
    // No shipped rates -> unknown, which the UI must omit rather than show as $0.
    expect(providerCostUsd({ provider_usage: { cumulative: {} } })).toBeNull();
    expect(providerCostUsd({ provider_usage: {} })).toBeNull();
    expect(providerCostUsd(undefined)).toBeNull();
    // A priced-to-zero model did cost nothing.
    expect(
      providerCostUsd({ provider_usage: { cumulative: { cost_usd: 0 } } }),
    ).toBe(0);
  });
});
