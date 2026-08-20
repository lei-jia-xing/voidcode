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
): number {
  const value = source?.[key];
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : 0;
}

/**
 * Used tokens for the latest turn.
 *
 * Backend semantics: `input_tokens` is the total prompt input and already
 * includes `cache_read_tokens` (cached hits are a component of the prompt).
 * Only `cache_write_tokens` (prompt cache writes) is additive on top of the
 * input, so summing `cache_read_tokens` again would double-count cache hits.
 */
export function providerContextTokens(
  metadata: Record<string, unknown> | undefined,
): number | null {
  const providerUsage = objectValue(metadata, "provider_usage");
  const latest = objectValue(providerUsage, "latest");
  if (!latest) {
    return null;
  }
  const total =
    numericValue(latest, "input_tokens") +
    numericValue(latest, "cache_write_tokens");
  return total > 0 ? total : null;
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
  const total =
    numericValue(cumulative, "input_tokens") +
    numericValue(cumulative, "output_tokens") +
    numericValue(cumulative, "cache_write_tokens");
  return total > 0 ? total : null;
}

export function providerCacheHitRate(
  metadata: Record<string, unknown> | undefined,
): number | null {
  const providerUsage = objectValue(metadata, "provider_usage");
  const cumulative = objectValue(providerUsage, "cumulative");
  if (cumulative && typeof cumulative.cache_hit_rate === "number") {
    return cumulative.cache_hit_rate;
  }
  const latest = objectValue(providerUsage, "latest");
  if (latest && typeof latest.cache_hit_rate === "number") {
    return latest.cache_hit_rate;
  }
  return null;
}
