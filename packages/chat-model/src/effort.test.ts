import { describe, expect, it } from "vitest";

import { effortLabel, middleEffort } from "./effort";

describe("middleEffort", () => {
  it.each([
    { efforts: ["low", "medium", "high"], expected: "medium" },
    { efforts: ["low", "medium", "high", "xhigh"], expected: "high" },
    { efforts: ["only"], expected: "only" },
    { efforts: [], expected: undefined },
  ])("picks the middle rung of $efforts", ({ efforts, expected }) => {
    expect(middleEffort(efforts)).toBe(expected);
  });
});

describe("effortLabel", () => {
  it.each([
    ["low", "Low"],
    ["medium", "Medium"],
    ["xhigh", "Extra high"],
    ["XHIGH", "Extra high"],
    ["max", "Max"],
    ["ultra", "Ultra"],
  ])("reads %s as %s", (value, label) => {
    expect(effortLabel(value)).toBe(label);
  });
});
