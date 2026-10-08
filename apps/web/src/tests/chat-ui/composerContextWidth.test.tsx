// The context read is on the rail at every width the rail is.
//
// It is the one number that says a compaction is coming, so a narrow rail
// (a 1440px laptop with the files pane open) tightens the cell instead of
// hiding the read behind `display: none`.
//
// jsdom lays nothing out and applies no stylesheet, so the two halves are
// pinned separately: the form the component picks from a measured rail width,
// and the stylesheet's own promise that the cell is never taken off the rail.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Composer, contextReadout, type ComposerProps } from "@alkera/ui";

const MODES = [{ value: "plan", label: "Plan" }];
const MODELS = [{ value: "model-one", label: "Model one" }];
const EFFORTS = [{ value: "low", label: "Low", bars: 1 as const }];

/** A 1440px window with the files pane open leaves the rail about here; a
 *  1920px one leaves it past the rung where the cell gets its full padding. */
const RAIL_AT_1440 = 430;
const RAIL_AT_1920 = 900;

/** The rail's measured width, which is what the component reads. `clientWidth`
 *  is 0 for every element in jsdom, so the rail's is answered here. */
let railWidth = 0;
let restoreClientWidth: (() => void) | null = null;

beforeEach(() => {
  const proto = HTMLElement.prototype;
  const original = Object.getOwnPropertyDescriptor(proto, "clientWidth");
  Object.defineProperty(proto, "clientWidth", {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains("chat-composer-rail") ? railWidth : 0;
    },
  });
  restoreClientWidth = () =>
    original
      ? Object.defineProperty(proto, "clientWidth", original)
      : Reflect.deleteProperty(proto, "clientWidth");
});

afterEach(() => {
  restoreClientWidth?.();
  restoreClientWidth = null;
  railWidth = 0;
});

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

describe("the context read across the widths the rail is drawn at", () => {
  it.each([
    ["a 1440px screen with the files pane open", RAIL_AT_1440, "compact"],
    ["a 1920px screen", RAIL_AT_1920, "full"],
  ])("states the share on %s", (_where, width, form) => {
    railWidth = width;
    renderComposer(contextReadout(61_439, 1_000_000));

    const readout = screen.getByLabelText("Context used: 61,439 of 1,000,000 tokens");
    // The share and its ring, at both forms: the tightened cell spends padding,
    // never the number.
    expect(readout).toHaveTextContent("6%");
    expect(readout.querySelector("svg")).not.toBeNull();
    expect(readout.getAttribute("data-context-form")).toBe(form);
  });
});

describe("the stylesheet the rail is drawn with", () => {
  // The bug was a stylesheet rule, not a missing element, so this is the half
  // jsdom cannot answer: re-introducing a `display: none` on the readout brings
  // it straight back.
  // vitest runs this project from `apps/web`.
  const css = readFileSync(
    resolve(process.cwd(), "../../packages/ui/src/chat/composer/composer.css"),
    "utf8",
  );

  it("never takes the context read off the rail", () => {
    const blocks = css.matchAll(/\.chat-composer-readout[^{}]*\{([^}]*)\}/g);
    const hidden = [...blocks].filter((block) => /display\s*:\s*none/.test(block[1]));
    expect(hidden).toEqual([]);
  });

  it("keeps the ring on the rail when the cell is squeezed", () => {
    expect(css).toMatch(/\[data-context-form="compact"\][^{}]*\{[^}]*min-width:\s*auto/);
  });
});
