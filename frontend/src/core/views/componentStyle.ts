import type { CSSProperties } from "react";
import type { ComponentStyle } from "../../lib/core/types";

/**
 * Minimal wire→CSS converter for the frozen SDK ComponentStyle contract:
 * `style: {tone, foreground?, background?}` on any of the 8 view data kinds.
 * `componentStyle(data)` styles the root; `componentFg(data)` hands the same
 * resolved foreground to default own-text descendants whose built-in
 * `text-*` classes would otherwise override the inherited color (fields
 * rows, status label/value/marker, choices options, diff path, form
 * labels, confirmation message). Descendants with independent fixed
 * semantics (allow/deny, diff +/- lines, error markers, selected/focus
 * states, native inputs) keep their classes. Unknown/missing input yields
 * `undefined` so built-in defaults (including notice level colors) render
 * untouched. Only validated `#RRGGBB`/named colors reach React — never raw
 * CSS/ANSI.
 */

const RGB = /^#[0-9a-fA-F]{6}$/;

/** 16 ANSI names → fixed sRGB approximations (appearance may vary by client). */
const ANSI: Record<string, string> = {
  black: "#000000",
  red: "#ef4444",
  green: "#22c55e",
  yellow: "#eab308",
  blue: "#3b82f6",
  magenta: "#d946ef",
  cyan: "#06b6d4",
  white: "#f8fafc",
  bright_black: "#9ca3af",
  bright_red: "#f87171",
  bright_green: "#4ade80",
  bright_yellow: "#facc15",
  bright_blue: "#60a5fa",
  bright_magenta: "#e879f9",
  bright_cyan: "#22d3ee",
  bright_white: "#ffffff",
};

const TONE_FG: Record<string, string> = {
  info: "#7dd3fc",
  success: "#6ee7b7",
  warning: "#fcd34d",
  error: "#fca5a5",
};

const TONE_BG: Record<string, string> = {
  info: "rgba(125, 211, 252, 0.08)",
  success: "rgba(110, 231, 183, 0.08)",
  warning: "rgba(252, 211, 77, 0.08)",
  error: "rgba(252, 165, 165, 0.08)",
};

/** Own-key lookup: `Record` maps inherit `Object.prototype` members. */
function own(map: Record<string, string>, key: string): string | undefined {
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : undefined;
}

function asColor(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  if (RGB.test(value)) return value;
  return own(ANSI, value);
}

/**
 * Narrow one view data object to its generated `ComponentStyle`, tolerating
 * malformed wire (defensive: SDK ingress rejects it first) by degrading to
 * the built-in default.
 */
function asWireStyle(data: { style?: unknown }): ComponentStyle | undefined {
  const raw = data.style;
  if (!raw || typeof raw !== "object") return undefined;
  const { tone, foreground, background } = raw as Record<string, unknown>;
  if (typeof tone !== "string") return undefined;
  if (
    foreground !== undefined &&
    foreground !== null &&
    typeof foreground !== "string"
  )
    return undefined;
  if (
    background !== undefined &&
    background !== null &&
    typeof background !== "string"
  )
    return undefined;
  return {
    tone: tone as ComponentStyle["tone"],
    foreground: (foreground as string | null | undefined) ?? null,
    background: (background as string | null | undefined) ?? null,
  };
}

/**
 * Single precedence resolution (explicit color > non-default tone, else
 * undefined so notice-level/client-default classes win); both exported
 * helpers read from it so root and descendants can never disagree. Unknown
 * tones degrade to default.
 */
function resolve(data: { style?: unknown }): { fg?: string; bg?: string } {
  const wire = asWireStyle(data);
  if (!wire) return {};
  const nonDefault = wire.tone !== "default" ? wire.tone : undefined;
  return {
    fg:
      asColor(wire.foreground) ??
      (nonDefault ? own(TONE_FG, nonDefault) : undefined),
    bg:
      asColor(wire.background) ??
      (nonDefault ? own(TONE_BG, nonDefault) : undefined),
  };
}

/** Resolved foreground for own-text descendants (see module doc). */
export function componentFg(data: {
  style?: unknown;
}): { color: string } | undefined {
  const fg = resolve(data).fg;
  return fg === undefined ? undefined : { color: fg };
}

/** Root inline styles for the owning component only. */
export function componentStyle(data: {
  style?: unknown;
}): CSSProperties | undefined {
  const { fg, bg } = resolve(data);
  if (fg === undefined && bg === undefined) return undefined;
  return {
    ...(fg !== undefined ? { color: fg, borderColor: fg } : {}),
    ...(bg !== undefined ? { backgroundColor: bg } : {}),
  };
}
