import { describe, expect, it } from "vitest";

import { modelChangePresentation } from "./permissionPresentation";

const BASE = {
  modelId: "claude-sonnet-5.5",
  displayName: "Claude Sonnet 5.5",
  previousModelId: "claude-haiku-4.5",
  previousDisplayName: "Claude Haiku 4.5",
  changerName: "Bob Editor",
  surface: "web",
};

describe("a model change card", () => {
  it("leads with the new model and reads old → new, who and where", () => {
    expect(modelChangePresentation({ ...BASE, effort: "high", previousEffort: "high" })).toEqual({
      title: "Model set to Claude Sonnet 5.5",
      change: "Claude Haiku 4.5 → Claude Sonnet 5.5",
      attribution: "Changed on web by Bob Editor",
    });
  });

  it("carries the effort beside each model only when it moved too", () => {
    expect(modelChangePresentation({ ...BASE, effort: "xhigh", previousEffort: "low" }).change).toBe(
      "Claude Haiku 4.5 · Low → Claude Sonnet 5.5 · Extra high",
    );
  });

  it("leads with the effort when only the effort moved", () => {
    expect(
      modelChangePresentation({
        ...BASE,
        previousModelId: BASE.modelId,
        previousDisplayName: BASE.displayName,
        effort: "xhigh",
        previousEffort: "medium",
        surface: "slack",
      }),
    ).toEqual({
      title: "Effort set to Extra high",
      change: "Medium → Extra high",
      attribution: "Changed in Slack by Bob Editor",
    });
  });

  it("falls back to the model id when the row names no display name, and leaves out who it does not know", () => {
    expect(modelChangePresentation({ modelId: "gpt-5.5", previousModelId: "claude-haiku-4.5" })).toEqual({
      title: "Model set to gpt-5.5",
      change: "claude-haiku-4.5 → gpt-5.5",
      attribution: "Changed",
    });
  });
});
