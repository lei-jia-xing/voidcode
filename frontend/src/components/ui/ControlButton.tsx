import type { ButtonHTMLAttributes } from "react";

export type ControlButtonVariant =
  "primary" | "secondary" | "ghost" | "danger" | "confirm";

export interface ControlButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ControlButtonVariant;
  compact?: boolean;
  icon?: boolean;
}

export function ControlButton({
  variant = "secondary",
  compact = false,
  icon = false,
  className,
  type = "button",
  ...props
}: ControlButtonProps) {
  return (
    <button
      {...props}
      type={type}
      className={[
        "vc-control",
        `vc-control--${variant}`,
        compact ? "vc-control--compact" : null,
        icon ? "vc-control--icon" : null,
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    />
  );
}
