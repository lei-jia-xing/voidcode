import type { ControlButtonProps } from "./ControlButton";

export type ControlButtonVariant =
  | "primary"
  | "secondary"
  | "ghost"
  | "danger"
  | "confirm";

export function controlButtonClassName({
  variant = "secondary",
  compact = false,
  icon = false,
  className,
}: Pick<ControlButtonProps, "variant" | "compact" | "icon" | "className">) {
  return [
    "vc-control",
    `vc-control--${variant}`,
    compact ? "vc-control--compact" : null,
    icon ? "vc-control--icon" : null,
    className,
  ]
    .filter(Boolean)
    .join(" ");
}
