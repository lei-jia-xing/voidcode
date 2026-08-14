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
