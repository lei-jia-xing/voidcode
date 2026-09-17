/**
 * Canonical reasoning-effort levels accepted by the runtime backend.
 * The backend strictly rejects any value outside this list; keep this
 * array as the single frontend source of truth for effort selectors.
 */
export const REASONING_EFFORT_LEVELS = [
  "off",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
] as const;

export type ReasoningEffortLevel = (typeof REASONING_EFFORT_LEVELS)[number];

/**
 * `off` is a disable intent, not a reasoning level: the backend maps it to the
 * model's own disable form (or to the lowest level the model supports). Models
 * report their reasoning levels through `supported_effort_levels`, which never
 * contains `off`.
 */
const REASONING_EFFORT_DISABLE = "off";

const CANONICAL_REASONING_EFFORT_LEVELS: readonly string[] =
  REASONING_EFFORT_LEVELS.filter((level) => level !== REASONING_EFFORT_DISABLE);

/** Catalog capability fields the effort selector needs, as served by `/models` or `provider inspect`. */
export interface ReasoningEffortCapabilityMetadata {
  supports_reasoning_effort?: boolean | null;
  default_reasoning_effort?: string | null;
  supported_effort_levels?: readonly string[] | null;
}

/**
 * Levels to offer for a model: the model's own catalog levels when it has them,
 * otherwise the canonical list. `off` is always offered first because every
 * reasoning-capable model can be told not to reason.
 */
export function reasoningEffortLevelsForModel(
  metadata?: ReasoningEffortCapabilityMetadata | null,
): string[] {
  const supported = (metadata?.supported_effort_levels ?? []).filter(
    (level) => typeof level === "string" && level.length > 0,
  );
  const levels =
    supported.length > 0 ? supported : CANONICAL_REASONING_EFFORT_LEVELS;
  return [REASONING_EFFORT_DISABLE, ...levels];
}

/**
 * Default level for a model: its catalog default when the model offers it,
 * otherwise the cheapest level that still reasons. `off` is never the implicit
 * default - a user who never touches the selector should not silently disable
 * reasoning.
 */
export function defaultReasoningEffortForModel(
  metadata?: ReasoningEffortCapabilityMetadata | null,
): string {
  const levels = reasoningEffortLevelsForModel(metadata);
  const preferred = metadata?.default_reasoning_effort?.trim();
  if (preferred && levels.includes(preferred)) {
    return preferred;
  }
  return levels.find((level) => level !== REASONING_EFFORT_DISABLE) ?? "off";
}
