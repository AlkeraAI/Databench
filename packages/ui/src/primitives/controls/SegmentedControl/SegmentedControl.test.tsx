import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SegmentedControl, type SegmentedOption } from "./SegmentedControl";

// Tests for the SegmentedControl `count` prop — an option can ride a trailing numeric tally, and the
// tally on the ACTIVE segment reads brand-tinted while inactive segments read neutral. The tint is a
// real, load-bearing state: it's the whole point of the count-on-a-segment contract (a scope switch
// that brightens the live scope's count). The Pill tone it selects (brand vs neutral) is the only
// observable carrier of that state, so asserting the tone class is asserting the contract, not an
// incidental implementation detail. The tablist/tab semantics + selection are covered as the frame the
// count rides on. Option keys/labels are bound to a named fixture so nothing is a copied literal.

afterEach(cleanup);

// One fixture, referenced by every test — no key/label literal is retyped into an assertion.
const DIRECT: SegmentedOption = { key: "direct", label: "Direct", count: 4 };
const ALL: SegmentedOption = { key: "all", label: "Including sub-teams", count: 12 };
const OPTIONS = [DIRECT, ALL] as const;
const LABEL = "Membership scope";

// The active/inactive tint is carried by the count Pill's `data-tone`. These are the two states the
// contract distinguishes — named here so the assertions read the contract, not a scattered literal.
const ACTIVE_COUNT_TONE = "brand";
const INACTIVE_COUNT_TONE = "neutral";

/** The count chip inside a given tab, by the tab's accessible name (its label). */
function countChipIn(tabName: string): HTMLElement {
  const tab = screen.getByRole("tab", { name: new RegExp(tabName) });
  return within(tab).getByText(String(tabCount(tabName))) as HTMLElement;
}

/** The count for a tab, from the fixture — so the expected number is derived, never copied. */
function tabCount(tabName: string): number {
  const opt = OPTIONS.find((o) => o.label === tabName);
  if (opt?.count == null) throw new Error(`no count for ${tabName}`);
  return opt.count;
}

describe("SegmentedControl count prop", () => {
  // The tint follows the selection rather than a position, which is the whole point of a count on a
  // scope switch. Asserted on both segments across a flip, so a constant-tone or first-segment mutant
  // fails on at least one.
  it("brand-tints whichever segment `value` selects", () => {
    const { rerender } = render(
      <SegmentedControl options={OPTIONS} value={DIRECT.key} onChange={vi.fn()} label={LABEL} />,
    );
    expect(countChipIn(DIRECT.label as string)).toHaveAttribute("data-tone", ACTIVE_COUNT_TONE);
    expect(countChipIn(ALL.label as string)).toHaveAttribute("data-tone", INACTIVE_COUNT_TONE);
    rerender(<SegmentedControl options={OPTIONS} value={ALL.key} onChange={vi.fn()} label={LABEL} />);
    expect(countChipIn(ALL.label as string)).toHaveAttribute("data-tone", ACTIVE_COUNT_TONE);
    expect(countChipIn(DIRECT.label as string)).toHaveAttribute("data-tone", INACTIVE_COUNT_TONE);
  });

  // The asymmetric / negative case: an option WITHOUT a count renders no tally at all. Catches a mutant
  // that renders a chip (e.g. "0" or an empty pill) for a countless option.
  it("renders no count chip for an option that carries no count", () => {
    const noCount: SegmentedOption = { key: "off", label: "No tally" };
    render(
      <SegmentedControl
        options={[DIRECT, noCount]}
        value={DIRECT.key}
        onChange={vi.fn()}
        label={LABEL}
      />,
    );
    const tab = screen.getByRole("tab", { name: /No tally/ });
    expect(within(tab).queryByText("0")).toBeNull();
    // No Pill of any kind lives in the countless tab.
    expect(tab.querySelector(".alk-pill")).toBeNull();
  });

  // A zero count is a real value that must still render (0 members is meaningful) — not dropped as
  // falsy. Catches a `count && <Pill>` truthiness bug that would swallow 0.
  it("renders a count chip for an explicit zero", () => {
    const zero: SegmentedOption = { key: "empty", label: "Empty", count: 0 };
    render(
      <SegmentedControl options={[zero, ALL]} value={zero.key} onChange={vi.fn()} label={LABEL} />,
    );
    const tab = screen.getByRole("tab", { name: /Empty/ });
    expect(within(tab).getByText(String(zero.count))).toBeInTheDocument();
  });
});

describe("SegmentedControl selection frame the count rides on", () => {
  it("marks the selected tab and fires onChange with the picked key", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<SegmentedControl options={OPTIONS} value={DIRECT.key} onChange={onChange} label={LABEL} />);

    const list = screen.getByRole("tablist", { name: LABEL });
    expect(list).toBeInTheDocument();
    // aria-selected tracks `value` — the selected tab is exactly the one whose key matches.
    expect(screen.getByRole("tab", { name: new RegExp(DIRECT.label as string) })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: new RegExp(ALL.label as string) })).toHaveAttribute("aria-selected", "false");

    await user.click(screen.getByRole("tab", { name: new RegExp(ALL.label as string) }));
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange).toHaveBeenCalledWith(ALL.key);
  });
});

describe("SegmentedControl tooltip option", () => {
  // A tooltip-bearing option renders through the Tooltip trigger wrapper, which must not cost the
  // button its tab semantics: the option is still a selectable tab, and once the tip shows (focus
  // opens it immediately) the tab is described by the tooltip node.
  it("keeps the tab role, gets described by the shown tip, and still selects on click", async () => {
    const onChange = vi.fn();
    const tipped: SegmentedOption = { key: "tipped", label: "Tipped", tooltip: "The longer explanation" };
    render(<SegmentedControl options={[DIRECT, tipped]} value={DIRECT.key} onChange={onChange} label={LABEL} />);

    const tab = screen.getByRole("tab", { name: /Tipped/ });
    expect(tab).toHaveAttribute("aria-selected", "false");

    fireEvent.focus(tab);
    const tip = await screen.findByRole("tooltip");
    expect(tip).toHaveTextContent("The longer explanation");
    expect(tab).toHaveAttribute("aria-describedby", tip.id);

    fireEvent.click(tab);
    expect(onChange).toHaveBeenCalledWith(tipped.key);
  });
});

describe("SegmentedControl shape + size seam", () => {
  // The shape/size variants drive the CSS through data-attributes on the root. The DEFAULT of each
  // axis (rect / lg) emits no attribute so the base rule applies — an emitted "rect"/"lg" attribute
  // would be a redundant selector the CSS doesn't target. Non-defaults are emitted verbatim.
  it.each([
    { axis: "shape", value: "rect", emitted: false },
    { axis: "shape", value: "pill", emitted: true },
    { axis: "size", value: "sm", emitted: true },
    { axis: "size", value: "md", emitted: true },
    { axis: "size", value: "lg", emitted: false },
  ])("emits data-$axis only for the non-default $value", ({ axis, value, emitted }) => {
    render(
      <SegmentedControl
        options={OPTIONS}
        value={DIRECT.key}
        onChange={vi.fn()}
        label={LABEL}
        {...{ [axis]: value }}
      />,
    );
    const list = screen.getByRole("tablist", { name: LABEL });
    if (emitted) expect(list).toHaveAttribute(`data-${axis}`, value);
    else expect(list).not.toHaveAttribute(`data-${axis}`);
  });
});
