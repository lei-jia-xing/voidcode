import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import "../i18n";
import { DiagnosticList } from "./ContextPanel";

describe("DiagnosticList", () => {
  it("renders completely duplicate diagnostics without dropping either item", () => {
    const diagnostic = {
      code: "CTX001",
      message: "Context warning",
      severity: "warning",
    };

    render(<DiagnosticList diagnostics={[diagnostic, { ...diagnostic }]} />);

    expect(screen.getAllByText("Context warning")).toHaveLength(2);
  });
});
