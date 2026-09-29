import { describe, expect, it } from "vitest";
import { buildSessionDisplayTitle } from "./sessionTitle";

describe("buildSessionDisplayTitle", () => {
  it("prefers a user-set title over the prompt", () => {
    expect(
      buildSessionDisplayTitle("implement the thing", "abcdef12", "My label"),
    ).toBe("My label");
  });

  it("falls back to the prompt when the title is null or blank", () => {
    expect(buildSessionDisplayTitle("read sample.txt", "abcdef12", null)).toBe(
      "read sample.txt",
    );
    expect(buildSessionDisplayTitle("read sample.txt", "abcdef12", "   ")).toBe(
      "read sample.txt",
    );
  });

  it("collapses whitespace in a set title so it renders on one line", () => {
    expect(
      buildSessionDisplayTitle("prompt", "abcdef12", "  two\n  words "),
    ).toBe("two words");
  });

  it("keeps an explicit title the server accepted, up to its own bound", () => {
    // 80 chars is longer than the derived-label ceiling (56) but within the
    // server's 120: the user's words must survive verbatim.
    const userTitle = "x".repeat(80);

    expect(buildSessionDisplayTitle("prompt", "abcdef12", userTitle)).toBe(
      userTitle,
    );
  });

  it("only cuts an explicit title beyond the server's own bound", () => {
    const overBound = "x".repeat(200);

    const result = buildSessionDisplayTitle("prompt", "abcdef12", overBound);

    // 120 kept + the ellipsis: the cut happens at the server's bound, not at 56.
    expect(result.length).toBe(121);
    expect(result.endsWith("…")).toBe(true);
  });

  it("falls back to the truncated session id when there is neither title nor prompt", () => {
    expect(buildSessionDisplayTitle(null, "abcdef123456")).toBe("abcdef12");
  });
});
