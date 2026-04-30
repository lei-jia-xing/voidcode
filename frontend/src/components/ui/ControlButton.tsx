import type { ButtonHTMLAttributes } from "react";
import {
  controlButtonClassName,
  type ControlButtonVariant,
} from "./controlButtonClassName";

export type { ControlButtonVariant } from "./controlButtonClassName";

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
      className={controlButtonClassName({
        variant,
        compact,
        icon,
        className,
      })}
    />
  );
}
