import { describe, expect, it } from "vitest";
import {
  defaultReasoningEffortForModel,
  reasoningEffortLevelsForModel,
  REASONING_EFFORT_LEVELS,
} from "./reasoningEffort";

describe("reasoningEffortLevelsForModel", () => {
  it("offers the model's own levels plus the off disable intent", () => {
    expect(
      reasoningEffortLevelsForModel({
        supports_reasoning_effort: true,
        supported_effort_levels: ["low", "high", "max"],
      }),
    ).toEqual(["off", "low", "high", "max"]);
  });

  it("falls back to the canonical levels when the model declares none", () => {
    expect(
      reasoningEffortLevelsForModel({ supports_reasoning_effort: true }),
    ).toEqual([...REASONING_EFFORT_LEVELS]);
    expect(reasoningEffortLevelsForModel(undefined)).toEqual([
      ...REASONING_EFFORT_LEVELS,
    ]);
  });

  it("ignores empty level entries", () => {
    expect(
      reasoningEffortLevelsForModel({ supported_effort_levels: ["", "high"] }),
    ).toEqual(["off", "high"]);
  });
});

describe("defaultReasoningEffortForModel", () => {
  it("uses the model's declared default when the model offers it", () => {
    expect(
      defaultReasoningEffortForModel({
        default_reasoning_effort: "max",
        supported_effort_levels: ["low", "high", "max"],
      }),
    ).toBe("max");
  });

  it("defaults to the cheapest reasoning level rather than off", () => {
    expect(
      defaultReasoningEffortForModel({
        supported_effort_levels: ["low", "high", "max"],
      }),
    ).toBe("low");
    expect(defaultReasoningEffortForModel(undefined)).toBe("minimal");
  });

  it("falls back when the declared default is not a supported level", () => {
    expect(
      defaultReasoningEffortForModel({
        default_reasoning_effort: "xhigh",
        supported_effort_levels: ["low", "high"],
      }),
    ).toBe("low");
  });
});
