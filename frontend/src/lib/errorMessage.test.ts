import { describe, expect, it } from "vitest";
import { errorMessage } from "./errorMessage";

describe("errorMessage", () => {
  it.each([
    [new Error("boom"), "boom"],
    ["  plain failure  ", "plain failure"],
    [{ message: "object failure" }, "object failure"],
  ])("extracts a useful message from %o", (value, expected) => {
    const result = errorMessage(value);
    expect(result).toBe(expected);
    expect(result).not.toBeUndefined();
    expect(result.trim()).not.toBe("");
  });

  it.each([
    null,
    undefined,
    "",
    "   ",
    {},
    { message: "   " },
    { message: 42 },
  ])("returns a non-empty fallback for %o", (value) => {
    const result = errorMessage(value);
    expect(result).toBe("Unknown error");
    expect(result).not.toBeUndefined();
    expect(result.trim()).not.toBe("");
  });
});
