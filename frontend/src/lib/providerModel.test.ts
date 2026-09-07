import { describe, expect, it } from "vitest";
import {
  canonicalModelReference,
  displayModelName,
  modelBelongsToProvider,
} from "./providerModel";

describe("provider model references", () => {
  it.each([
    ["deepseek", "deepseek-v4-pro", "deepseek/deepseek-v4-pro"],
    ["zai", "nested/model", "zai/nested/model"],
    ["deepseek", "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro"],
  ])("canonicalizes %s/%s", (provider, model, expected) => {
    expect(canonicalModelReference(provider, model)).toBe(expected);
  });

  it.each([
    ["deepseek-v4-pro", "deepseek", "deepseek-v4-pro"],
    ["deepseek/deepseek-v4-pro", "deepseek", "deepseek-v4-pro"],
    ["nested/model", "zai", "nested/model"],
    ["deepseek/deepseek-v4-pro", null, "deepseek/deepseek-v4-pro"],
  ])("displays %s for provider %s", (model, provider, expected) => {
    expect(displayModelName(model, provider)).toBe(expected);
  });

  it.each([
    ["deepseek/deepseek-v4-pro", "deepseek", true],
    ["nested/model", "deepseek", false],
    ["", "deepseek", false],
  ])("checks whether %s belongs to %s", (model, provider, expected) => {
    expect(modelBelongsToProvider(model, provider)).toBe(expected);
  });
});
