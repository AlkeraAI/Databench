import { useState } from "react";

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { SegmentedControl, type SegmentedOption } from "./SegmentedControl";

// A segmented control that sets a value in place (a unit beside an amount) is a radio group, not a
// tablist: announced as tabs, a GB / TB toggle on a page with its own tabs read as two more sections.

afterEach(cleanup);

const UNITS: SegmentedOption[] = [
  { key: "gb", label: "GB" },
  { key: "tb", label: "TB" },
  { key: "pb", label: "PB" },
];
const LABEL = "Unit for storage";

function Units({ initial }: { initial: string }) {
  const [value, setValue] = useState(initial);
  return <SegmentedControl semantics="radio" options={UNITS} value={value} onChange={setValue} label={LABEL} />;
}

const checked = () =>
  screen
    .getAllByRole("radio")
    .filter((r) => r.getAttribute("aria-checked") === "true")
    .map((r) => r.textContent);

describe("a segmented control with radio semantics", () => {
  it("is a named radio group of radios, with no tab anywhere", () => {
    render(<Units initial="tb" />);
    expect(screen.getByRole("radiogroup", { name: LABEL })).toBeInTheDocument();
    expect(screen.getAllByRole("radio").map((r) => r.textContent)).toEqual(["GB", "TB", "PB"]);
    expect(checked()).toEqual(["TB"]);
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
    // aria-selected belongs to tabs; a radio says aria-checked.
    for (const r of screen.getAllByRole("radio")) expect(r).not.toHaveAttribute("aria-selected");
  });

  it("is one tab stop, on the checked option", async () => {
    const user = userEvent.setup();
    render(
      <>
        <button type="button">before</button>
        <Units initial="tb" />
        <button type="button">after</button>
      </>,
    );
    screen.getByRole("button", { name: "before" }).focus();
    await user.tab();
    expect(screen.getByRole("radio", { name: "TB" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "after" })).toHaveFocus();
  });

  it("puts the tab stop on the first option when none is checked", () => {
    render(<SegmentedControl semantics="radio" options={UNITS} value="none" onChange={() => {}} label={LABEL} />);
    expect(screen.getAllByRole("radio").map((r) => r.tabIndex)).toEqual([0, -1, -1]);
  });

  it.each([
    ["{ArrowRight}", "tb", "PB"],
    ["{ArrowDown}", "tb", "PB"],
    ["{ArrowLeft}", "tb", "GB"],
    ["{ArrowUp}", "tb", "GB"],
    ["{ArrowRight}", "pb", "GB"],
    ["{ArrowLeft}", "gb", "PB"],
  ])("%s from %s moves to and picks %s, wrapping at the ends", async (key, initial, expected) => {
    const user = userEvent.setup();
    render(<Units initial={initial} />);
    const start = screen.getAllByRole("radio").find((r) => r.getAttribute("aria-checked") === "true")!;
    start.focus();
    await user.keyboard(key);
    expect(checked()).toEqual([expected]);
    expect(screen.getByRole("radio", { name: expected })).toHaveFocus();
  });

  it("ignores keys that are not arrows", async () => {
    const user = userEvent.setup();
    render(<Units initial="tb" />);
    screen.getByRole("radio", { name: "TB" }).focus();
    await user.keyboard("{Home}a");
    expect(checked()).toEqual(["TB"]);
  });

  it("stays a tablist by default, for a switch between views", () => {
    render(<SegmentedControl options={UNITS} value="gb" onChange={() => {}} label="View" />);
    expect(screen.getByRole("tablist", { name: "View" })).toBeInTheDocument();
    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(screen.queryByRole("radio")).toBeNull();
  });
});
