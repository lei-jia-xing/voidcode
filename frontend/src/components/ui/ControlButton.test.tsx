import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ControlButton } from "./ControlButton";
import {
  controlButtonClassName,
  type ControlButtonVariant,
} from "./controlButtonClassName";

const variants: ControlButtonVariant[] = [
  "primary",
  "secondary",
  "ghost",
  "danger",
  "confirm",
];

describe("ControlButton", () => {
  it("applies the shared control class and requested variant", () => {
    render(<ControlButton variant="primary">Run</ControlButton>);

    const button = screen.getByRole("button", { name: "Run" });
    expect(button).toHaveClass("vc-control", "vc-control--primary");
    expect(button).toHaveAttribute("type", "button");
  });

  it.each(variants)("supports the %s variant", (variant) => {
    expect(controlButtonClassName({ variant })).toContain(
      `vc-control--${variant}`,
    );
  });

  it("supports compact icon controls without dropping custom classes", () => {
    render(
      <ControlButton
        compact
        icon
        className="runtime-action"
        aria-label="Stop"
      />,
    );

    const button = screen.getByRole("button", { name: "Stop" });
    expect(button).toHaveClass(
      "vc-control--compact",
      "vc-control--icon",
      "runtime-action",
    );
  });
});
