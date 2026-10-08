import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { Select, type SelectTickSide } from "./Select";

// `tickSide` chooses which edge of a listbox row the selected check rides. Which side that is only
// shows up in two places a caller can observe. The row's child order (a mark before or after the
// label) and the `data-tick` hook the stylesheet keys on, so these read both. Everything else must
// come out identical on either side. Every row lookup here goes through its accessible name, so a
// name the marks leaked into would fail the lookup itself.

const trigger = () => screen.getByRole("button");
const row = (name: string) => screen.getByRole("option", { name });
const marks = (name: string) => [...row(name).querySelectorAll(".alk-option-mark")];
/** A mark box holding a glyph the test never supplied is the check. */
const isCheck = (mark: Element) => mark.querySelector("svg") != null && mark.querySelector("[data-testid]") == null;

const GLYPHS = {
  a: <svg data-testid="glyph-a" />,
  b: <svg data-testid="glyph-b" />,
};

afterEach(cleanup);

describe("Select — the default keeps the check in the leading column", () => {
  it("leads the selected row with the check and carries no data-tick", async () => {
    const user = userEvent.setup();
    render(
      <Select aria-label="Grade" defaultValue="a">
        <option value="a">Assay A</option>
        <option value="b">Assay B</option>
      </Select>,
    );
    await user.click(trigger());
    const selected = row("Assay A");
    // No attribute at all, so the base rule applies and today's rows are untouched.
    expect(selected).not.toHaveAttribute("data-tick");
    expect(marks("Assay A")).toHaveLength(1);
    expect(selected.firstElementChild).toHaveClass("alk-option-mark");
    expect(isCheck(selected.firstElementChild!)).toBe(true);
    // The unselected row keeps the column empty so both labels start at the same x.
    expect(marks("Assay B")).toHaveLength(1);
    expect(row("Assay B").firstElementChild!.querySelector("svg")).toBeNull();
  });
});

describe("Select — tickSide=right trails the check and flushes labels left", () => {
  it("drops the leading column and hangs the check off the row's end", async () => {
    const user = userEvent.setup();
    render(
      <Select aria-label="Grade" defaultValue="a" tickSide="right">
        <option value="a">Assay A</option>
        <option value="b">Assay B</option>
      </Select>,
    );
    await user.click(trigger());
    const selected = row("Assay A");
    expect(selected).toHaveAttribute("data-tick", "right");
    expect(selected.firstElementChild).toHaveClass("alk-select__option-label");
    expect(marks("Assay A")).toHaveLength(1);
    expect(isCheck(selected.lastElementChild!)).toBe(true);
    // Nothing at all on an unselected row, so its label starts where the selected one does.
    expect(marks("Assay B")).toHaveLength(0);
    expect(row("Assay B").firstElementChild).toHaveClass("alk-select__option-label");
  });

  it("moves the check onto the row that was picked", async () => {
    const user = userEvent.setup();
    render(
      <Select aria-label="Grade" defaultValue="a" tickSide="right">
        <option value="a">Assay A</option>
        <option value="b">Assay B</option>
      </Select>,
    );
    await user.click(trigger());
    await user.click(row("Assay B"));
    await user.click(trigger());
    expect(marks("Assay A")).toHaveLength(0);
    expect(isCheck(row("Assay B").lastElementChild!)).toBe(true);
  });
});

describe("Select — tickSide=right alongside optionIcons", () => {
  // `optionIcons` is a whole-listbox map, so the glyph column is reserved on every row and the check
  // trails on top of it. A selected row therefore shows both, which is where this deliberately parts
  // from DropdownItem (an icon there pins the tick left and the row trades its glyph for the check).
  it("keeps every row's glyph column and still trails the check", async () => {
    const user = userEvent.setup();
    render(
      <Select aria-label="Kind" defaultValue="a" tickSide="right" optionIcons={GLYPHS}>
        <option value="a">Assay A</option>
        <option value="b">Assay B</option>
        <option value="c">Assay C</option>
      </Select>,
    );
    await user.click(trigger());

    // Selected. The glyph holds its column and the check arrives at the far end.
    const selected = marks("Assay A");
    expect(selected).toHaveLength(2);
    expect(selected[0].querySelector("[data-testid='glyph-a']")).not.toBeNull();
    expect(selected[0]).toBe(row("Assay A").firstElementChild);
    expect(isCheck(selected[1])).toBe(true);
    expect(selected[1]).toBe(row("Assay A").lastElementChild);

    // Unselected with a glyph. One mark, leading, no check.
    expect(marks("Assay B")).toHaveLength(1);
    expect(marks("Assay B")[0].querySelector("[data-testid='glyph-b']")).not.toBeNull();

    // A value the map skips still holds the empty column, so its label doesn't slide left of the rest.
    expect(marks("Assay C")).toHaveLength(1);
    expect(marks("Assay C")[0].querySelector("svg")).toBeNull();
    expect(marks("Assay C")[0]).toBe(row("Assay C").firstElementChild);
  });

  it("collapses the column when the map fills no row", async () => {
    // The column earns its space only when something occupies it, which is the rule the component
    // documents and the reason a check-only listbox drops the column at all. A map that fills no
    // visible row occupies nothing, so an indent here buys the caller a blank gutter. Reaching it
    // takes no exotic caller, only a glyph map built from data that hasn't arrived yet.
    const user = userEvent.setup();
    render(
      <Select aria-label="Kind" defaultValue="a" tickSide="right" optionIcons={{}}>
        <option value="a">Assay A</option>
        <option value="b">Assay B</option>
      </Select>,
    );
    await user.click(trigger());
    expect(row("Assay B").firstElementChild).toHaveClass("alk-select__option-label");
    expect(marks("Assay B")).toHaveLength(0);
    expect(row("Assay A").firstElementChild).toHaveClass("alk-select__option-label");
    expect(marks("Assay A")).toHaveLength(1);
  });
});

describe("Select — tickSide moves no behavior", () => {
  // Same keys, same announcements, same committed value on either side. The prop is presentation.
  it.each<SelectTickSide>(["left", "right"])("%s picks by keyboard and announces the same", async (tickSide) => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <Select aria-label="Grade" defaultValue="a" tickSide={tickSide} optionIcons={GLYPHS} onChange={onChange}>
        <option value="a">Assay A</option>
        <option value="b">Assay B</option>
      </Select>,
    );
    trigger().focus();
    await user.keyboard("{ArrowDown}");
    const listbox = screen.getByRole("listbox");
    expect(listbox).toHaveAttribute("aria-label", "Grade");
    // The panel opens pointing at the selected row, then Down walks and Enter commits.
    expect(listbox).toHaveAttribute("aria-activedescendant", row("Assay A").id);
    expect(row("Assay A")).toHaveAttribute("aria-selected", "true");
    expect(row("Assay B")).toHaveAttribute("aria-selected", "false");
    await user.keyboard("{ArrowDown}");
    expect(listbox).toHaveAttribute("aria-activedescendant", row("Assay B").id);
    await user.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(trigger()).toHaveTextContent("Assay B");
    await user.click(trigger());
    expect(row("Assay B")).toHaveAttribute("aria-selected", "true");
  });
});

describe("select.css — a long label gives way, the trailing check never does", () => {
  // The row's real cascade, read back from the stylesheet the app ships. A rule that misses its
  // element (or leaks onto the default row) is invisible to a markup assertion.
  const LONG = "Assay of the descent-weighted margin across every warehouse this account touches";

  /** Read a stylesheet next to the test. vitest's CSS pipeline empties `?raw` imports, so the test's
   *  own path is the anchor (see TextInput.collapsible.test.tsx). */
  function readCss(...segments: string[]): string {
    const testPath = expect.getState().testPath;
    if (!testPath) throw new Error("vitest testPath unavailable, cannot locate the stylesheet");
    return readFileSync(join(dirname(testPath), ...segments), "utf8");
  }

  let sheet: HTMLStyleElement;
  beforeAll(() => {
    sheet = document.createElement("style");
    sheet.textContent = readCss("..", "..", "base.css") + readCss("select.css");
    document.head.append(sheet);
  });
  afterAll(() => sheet.remove());

  const labelOf = (name: string) => row(name).querySelector(".alk-select__option-label") as HTMLElement;

  it("truncates the trailing variant's label and holds the check's box", async () => {
    const user = userEvent.setup();
    render(
      <Select aria-label="Grade" defaultValue="a" tickSide="right">
        <option value="a">{LONG}</option>
        <option value="b">Assay B</option>
      </Select>,
    );
    await user.click(trigger());
    const label = getComputedStyle(labelOf(LONG));
    expect(label.flex).toBe("1 1 auto");
    expect(label.minWidth).toBe("0");
    expect(label.overflow).toBe("clip");
    expect(label.textOverflow).toBe("ellipsis");
    expect(label.whiteSpace).toBe("nowrap");
    // The check is the fixed box the label shrinks against, and it stays at the row's end.
    const check = row(LONG).lastElementChild as HTMLElement;
    expect(isCheck(check)).toBe(true);
    expect(getComputedStyle(check).flexShrink).toBe("0");
    expect(marks(LONG)).toHaveLength(1);
  });

  it("leaves the default row's label untruncated", async () => {
    const user = userEvent.setup();
    render(
      <Select aria-label="Grade" defaultValue="a">
        <option value="a">{LONG}</option>
        <option value="b">Assay B</option>
      </Select>,
    );
    await user.click(trigger());
    const label = getComputedStyle(labelOf(LONG));
    expect(label.overflow).not.toBe("clip");
    expect(label.textOverflow).not.toBe("ellipsis");
  });
});
