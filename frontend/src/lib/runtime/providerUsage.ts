// Pure helpers for deriving token-usage figures from session provider_usage
// metadata. Kept in their own module so they can be unit-tested directly
// (exporting non-component functions from App.tsx would trip the
// react-refresh/only-export-components lint rule).

function objectValue(
  source: Record<string, unknown> | undefined,
  key: string,
): Record<string, unknown> | undefined {
  const value = source?.[key];
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function numericValue(
  source: Record<string, unknown> | undefined,
  key: string,
): number | null {
  const value = source?.[key];
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

/**
 * Used tokens for the latest turn — the canonical context-token total:
 * `input_tokens + cache_write_tokens + output_tokens`. Cache reads are already
 * part of `input_tokens` on every wire, so summing `cache_read_tokens` again
 * would double-count cache hits; this turn's output joins because it becomes
 * history on the next request. `provider_usage_anchor_tokens` (backend) uses the
 * same expression for the compaction anchor.
 */
export function providerContextTokens(
  metadata: Record<string, unknown> | undefined,
): number | null {
  const providerUsage = objectValue(metadata, "provider_usage");
  const latest = objectValue(providerUsage, "latest");
  if (!latest) {
    return null;
  }
  const input = numericValue(latest, "input_tokens");
  const cacheWrite = numericValue(latest, "cache_write_tokens");
  const output = numericValue(latest, "output_tokens");
  return input !== null && cacheWrite !== null && output !== null
    ? input + cacheWrite + output
    : null;
}

/** Cumulative (whole-session) token total. */
export function providerTotalTokens(
  metadata: Record<string, unknown> | undefined,
): number | null {
  const providerUsage = objectValue(metadata, "provider_usage");
  const cumulative = objectValue(providerUsage, "cumulative");
  if (!cumulative) {
    return null;
  }
  // cumulative.input_tokens already includes cache_read_tokens, so only
  // cache_write_tokens is additive (consistent with providerContextTokens).
  const input = numericValue(cumulative, "input_tokens");
  const output = numericValue(cumulative, "output_tokens");
  const cacheWrite = numericValue(cumulative, "cache_write_tokens");
  return input !== null && output !== null && cacheWrite !== null
    ? input + output + cacheWrite
    : null;
}

/**
 * Cumulative session cost, or `null` unless the runtime can account for the
 * whole session.
 */
export function providerCostUsd(
  metadata: Record<string, unknown> | undefined,
): number | null {
  const cumulative = objectValue(
    objectValue(metadata, "provider_usage"),
    "cumulative",
  );
  const cost = cumulative?.cost_usd;
  return typeof cost === "number" && Number.isFinite(cost) && cost >= 0
    ? cost
    : null;
}

export function providerCacheHitRate(
  metadata: Record<string, unknown> | undefined,
): number | null {
  const cumulative = objectValue(
    objectValue(metadata, "provider_usage"),
    "cumulative",
  );
  const rate = cumulative?.cache_hit_rate;
  return typeof rate === "number" &&
    Number.isFinite(rate) &&
    rate >= 0 &&
    rate <= 1
    ? rate
    : null;
}
