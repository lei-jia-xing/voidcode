import { describe, expect, it } from "vitest";
import {
  providerContextTokens,
  providerCostUsd,
  providerTotalTokens,
} from "./providerUsage";

describe("provider context tokens", () => {
  it("does not double-count cache reads that are already inside input_tokens", () => {
    // 10_000 prompt tokens (9_000 of them cache reads, on every wire a component
    // of input) + 500 cache writes + 200 output -> 10_700, not 19_700.
    expect(
      providerContextTokens({
        provider_usage: {
          latest: {
            input_tokens: 10_000,
            cache_read_tokens: 9_000,
            cache_write_tokens: 500,
            output_tokens: 200,
          },
        },
      }),
    ).toBe(10_700);
    expect(providerContextTokens(undefined)).toBeNull();
  });

  it("applies the same inclusive input to the cumulative total", () => {
    expect(
      providerTotalTokens({
        provider_usage: {
          cumulative: {
            input_tokens: 10_000,
            cache_read_tokens: 9_000,
            cache_write_tokens: 500,
            output_tokens: 200,
          },
        },
      }),
    ).toBe(10_700);
  });
});

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
