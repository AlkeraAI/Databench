import { describe, expect, it } from "vitest";

import { resolveModelChoice, type ChoiceModel } from "./modelChoice";

const catalog: ChoiceModel[] = [
  { id: "alpha", efforts: ["none", "low"], defaultEffort: "low" },
  { id: "beta", efforts: ["none", "low", "high"], defaultEffort: "high" },
];

describe("resolveModelChoice", () => {
  it("does not carry a paired seed effort across an overriding model selection", () => {
    expect(
      resolveModelChoice(
        catalog,
        { model: "alpha", effort: "low" },
        { model: "beta" },
      ),
    ).toEqual({
      model: "beta",
      efforts: ["none", "low", "high"],
      effort: "high",
      reasoningVisible: true,
    });
  });

  it("replaces a stale effort with the selected model default instead of retaining it", () => {
    expect(
      resolveModelChoice(
        catalog,
        { model: "alpha", effort: "low" },
        { model: "beta", effort: "ultra" },
      ),
    ).toEqual({
      model: "beta",
      efforts: ["none", "low", "high"],
      effort: "high",
      reasoningVisible: true,
    });
  });

  it("fails closed while a pinned model default is unresolved instead of inventing an effort", () => {
    expect(
      resolveModelChoice(
        [{ id: "pending", efforts: ["none", "low"], defaultEffort: undefined }],
        { model: "pending" },
        undefined,
      ),
    ).toEqual({
      model: "pending",
      efforts: ["none", "low"],
      effort: undefined,
      reasoningVisible: false,
    });
  });

  it("accepts paired explicit efforts before the catalog default resolves instead of hiding all reasoning", () => {
    const unresolved: ChoiceModel[] = [
      { id: "pending", efforts: ["none", "low"], defaultEffort: undefined },
    ];

    expect(
      resolveModelChoice(
        unresolved,
        { model: "pending" },
        { model: "pending", effort: "low" },
      ),
    ).toMatchObject({ effort: "low", reasoningVisible: true });
    expect(
      resolveModelChoice(
        unresolved,
        { model: "pending" },
        { model: "pending", effort: "none" },
      ),
    ).toMatchObject({ effort: "none", reasoningVisible: false });
  });
});
