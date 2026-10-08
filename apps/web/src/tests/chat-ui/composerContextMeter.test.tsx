// The context readout on the composer rail.
//
// Nothing in the chat said how full the model's window was, so a compaction
// arrived with no warning at all. The rail now states what the conversation
// carries. It states the RING only where a window is actually known: drawing a
// share of a size nobody published would be a fraction of a guess.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Composer, compactTokens, contextReadout, type ComposerProps } from "@alkera/ui";

const MODES = [{ value: "plan", label: "Plan" }];
const MODELS = [{ value: "model-one", label: "Model one" }];
const EFFORTS = [{ value: "low", label: "Low", bars: 1 as const }];

function renderComposer(overrides: Partial<ComposerProps> = {}): void {
  render(
    <div className="chat-root">
      <Composer
        modes={MODES}
        mode="plan"
        onModeChange={() => {}}
        models={MODELS}
        model="model-one"
        onModelChange={() => {}}
        efforts={EFFORTS}
        effort="low"
        onEffortChange={() => {}}
        slashCommands={[]}
        onSend={() => {}}
        {...overrides}
      />
    </div>,
  );
}

describe("the composer's context readout", () => {
  it("says nothing where the shell has no usage to report", () => {
    renderComposer();
    expect(screen.queryByLabelText(/Context used/)).not.toBeInTheDocument();
  });

  it("states the conversation's size, with the whole figure on the label", () => {
    renderComposer({ context: "72k", contextTitle: "71,854 tokens in this conversation" });

    const readout = screen.getByLabelText("Context used: 71,854 tokens in this conversation");
    expect(readout).toHaveTextContent("72k");
    // No window published, so no ring: the number stands on its own.
    expect(readout.querySelector("svg")).toBeNull();
  });

  it("draws the ring only when a window is known", () => {
    renderComposer({ context: "36%", contextPercent: 36, contextTitle: "72k of 200k tokens" });

    const readout = screen.getByLabelText("Context used: 72k of 200k tokens");
    expect(readout.querySelector("svg")).not.toBeNull();
  });
});

describe("token figures", () => {
  it.each([
    [0, "0"],
    [999, "999"],
    [1_005, "1.0k"],
    [9_949, "9.9k"],
    [21_005, "21k"],
    [71_854, "72k"],
  ])("renders %i as %s", (value, shown) => {
    expect(compactTokens(value)).toBe(shown);
  });
});

describe("what the readout says for a conversation", () => {
  it("reports nothing before a turn has reported usage", () => {
    expect(contextReadout(null)).toEqual({});
    expect(contextReadout(undefined, 200_000)).toEqual({});
  });

  it("spends the tokens against the model's window when the chat pinned one", () => {
    expect(contextReadout(71_854, 200_000)).toEqual({
      context: "36%",
      contextPercent: 36,
      contextTitle: "71,854 of 200,000 tokens",
    });
  });

  it("states the count alone when no window is known", () => {
    expect(contextReadout(71_854)).toEqual({
      context: "72k",
      contextTitle: "71,854 tokens in this conversation",
    });
    // A zero window is "the catalog did not say", not "a window of nothing".
    expect(contextReadout(71_854, 0).contextPercent).toBeUndefined();
  });

  it("never reports more than a full window", () => {
    expect(contextReadout(260_000, 200_000)).toMatchObject({ context: "100%", contextPercent: 100 });
  });

  it("renders the share it is given, ring and all", () => {
    renderComposer(contextReadout(71_854, 200_000));
    const readout = screen.getByLabelText("Context used: 71,854 of 200,000 tokens");
    expect(readout).toHaveTextContent("36%");
    expect(readout.querySelector("svg")).not.toBeNull();
  });
});
