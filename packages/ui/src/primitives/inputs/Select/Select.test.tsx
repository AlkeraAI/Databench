import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Select } from "./Select";
import { pushEsc } from "../../../hooks";
import type { ControlSize } from "../../sizes";

// Tests for the @alkera/ui Select — the custom listbox over a hidden native <select>.
//
// The visible chrome is a button trigger whose SIZE and PLACEHOLDER state are the seams a stylesheet
// (and this test) can see — carried on `data-size` / `data-placeholder`, NOT on a per-variant class.
// These pin that observable contract (the default `lg` emits no `data-size`; only a non-empty
// selection drops `data-placeholder`) plus the value round-trip through the native control that owns
// the value. They assert what a caller / AT / stylesheet observes, never markup internals.

const trigger = () => screen.getByRole("button");

afterEach(cleanup);

describe("Select — trigger size on data-size", () => {
  // The size axis lands on `data-size` exactly; the default `lg` omits the attribute so the base rule
  // (no `[data-size]`) applies — a per-size class would over-specify the same thing.
  it.each<[ControlSize, string | null]>([
    ["sm", "sm"],
    ["md", "md"],
    ["lg", null],
  ])("size=%s → data-size %s", (size, expected) => {
    render(
      <Select size={size} aria-label="Grade" defaultValue="a">
        <option value="a">Assay A</option>
      </Select>,
    );
    if (expected === null) expect(trigger()).not.toHaveAttribute("data-size");
    else expect(trigger()).toHaveAttribute("data-size", expected);
  });
});

describe("Select — placeholder state on data-placeholder", () => {
  // The trigger carries `data-placeholder` whenever the value resolves to no visible label. A real
  // selection drops it (pinned by the round-trip below). This is the seam the placeholder ink uses.
  it("carries data-placeholder while the value matches no option", () => {
    render(
      <Select aria-label="Grade" value="unset">
        <option value="a">Assay A</option>
        <option value="b">Assay B</option>
      </Select>,
    );
    expect(trigger()).toHaveAttribute("data-placeholder", "");
  });
});

describe("Select — value round-trip through the native control", () => {
  it("commits a pick to the hidden native select and fires onChange", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <Select aria-label="Grade" defaultValue="a" onChange={onChange}>
        <option value="a">Assay A</option>
        <option value="b">Assay B</option>
      </Select>,
    );
    await user.click(trigger());
    await user.click(screen.getByRole("option", { name: "Assay B" }));
    expect(onChange).toHaveBeenCalled();
    // The native <select> is the value source of truth; the pick is reflected on the closed trigger.
    expect(trigger()).toHaveTextContent("Assay B");
    expect(trigger()).not.toHaveAttribute("data-placeholder");
  });
});

describe("Select — Escape routes through the shared overlay stack", () => {
  // One press dismisses exactly ONE surface. The open listbox is a stack entry above any page
  // layer, so its Escape never also reaches a page-level back-out; the closed control holds no
  // entry at all, so it never eats the page's Escape. The trigger must not close on Escape
  // itself: a trigger-level close fell out of the stack before the press reached the stack's
  // document listener, and the same press dismissed the page layer too.
  const OPTIONS = (
    <>
      <option value="a">Assay A</option>
      <option value="b">Assay B</option>
    </>
  );

  it("an open listbox takes the Escape alone; the next press reaches the page layer", async () => {
    const user = userEvent.setup();
    const pageLayer = vi.fn();
    const unregister = pushEsc(pageLayer);
    try {
      render(
        <Select aria-label="Grade" defaultValue="a">
          {OPTIONS}
        </Select>,
      );
      await user.click(trigger());
      expect(trigger()).toHaveAttribute("aria-expanded", "true");
      await user.keyboard("{Escape}");
      // The listbox closed and consumed the press whole — the page layer beneath never fired.
      expect(trigger()).toHaveAttribute("aria-expanded", "false");
      expect(pageLayer).not.toHaveBeenCalled();
      // With nothing floating, the same key now belongs to the page layer.
      await user.keyboard("{Escape}");
      expect(pageLayer).toHaveBeenCalledTimes(1);
    } finally {
      unregister();
    }
  });

  it("a closed Select never eats the page's Escape", async () => {
    const user = userEvent.setup();
    const pageLayer = vi.fn();
    const unregister = pushEsc(pageLayer);
    try {
      render(
        <Select aria-label="Grade" defaultValue="a">
          {OPTIONS}
        </Select>,
      );
      trigger().focus();
      await user.keyboard("{Escape}");
      expect(pageLayer).toHaveBeenCalledTimes(1);
      expect(trigger()).toHaveAttribute("aria-expanded", "false");
    } finally {
      unregister();
    }
  });
});

describe("Select — per-option glyphs (optionIcons)", () => {
  // Each option's glyph rides the LEADING mark slot; under the default `tickSide` the check REPLACES
  // it on the selected row — one mark per row, never both, never a double indent. The closed trigger
  // leads with the selected option's glyph. This mirrors the Dropdown's menu rows (DropdownItem's
  // `icon`). The trailing arm of the contract lives in Select.tickSide.test.tsx.
  const icons = {
    a: <svg data-testid="glyph-a" />,
    b: <svg data-testid="glyph-b" />,
  };

  it("the selected row's check replaces its glyph in the mark slot", async () => {
    const user = userEvent.setup();
    render(
      <Select aria-label="Kind" defaultValue="a" optionIcons={icons}>
        <option value="a">Assay A</option>
        <option value="b">Assay B</option>
      </Select>,
    );
    await user.click(trigger());
    const markOf = (name: string) => screen.getByRole("option", { name }).querySelector(".alk-select__option-mark")!;
    // Selected: exactly one svg (the check), NOT the row's own glyph.
    expect(markOf("Assay A").querySelectorAll("svg")).toHaveLength(1);
    expect(markOf("Assay A").querySelector("[data-testid='glyph-a']")).toBeNull();
    // Unselected: the row's glyph fills the same slot.
    expect(markOf("Assay B").querySelectorAll("svg")).toHaveLength(1);
    expect(markOf("Assay B").querySelector("[data-testid='glyph-b']")).not.toBeNull();
  });

  it("the closed trigger leads with the selected glyph, re-led on a pick", async () => {
    const user = userEvent.setup();
    render(
      <Select aria-label="Kind" defaultValue="a" optionIcons={icons}>
        <option value="a">Assay A</option>
        <option value="b">Assay B</option>
      </Select>,
    );
    expect(trigger().querySelector(".alk-select__lead [data-testid='glyph-a']")).not.toBeNull();
    await user.click(trigger());
    await user.click(screen.getByRole("option", { name: "Assay B" }));
    expect(trigger().querySelector(".alk-select__lead [data-testid='glyph-b']")).not.toBeNull();
    expect(trigger().querySelector("[data-testid='glyph-a']")).toBeNull();
  });

  it("renders no lead slot without a matching glyph", () => {
    render(
      <Select aria-label="Kind" value="unset" onChange={() => {}} optionIcons={icons}>
        <option value="a">Assay A</option>
      </Select>,
    );
    expect(trigger().querySelector(".alk-select__lead")).toBeNull();
    cleanup();
    render(
      <Select aria-label="Kind" defaultValue="c" optionIcons={icons}>
        <option value="c">Assay C</option>
      </Select>,
    );
    expect(trigger().querySelector(".alk-select__lead")).toBeNull();
  });

  it("without optionIcons the rows stay check-or-empty", async () => {
    const user = userEvent.setup();
    render(
      <Select aria-label="Kind" defaultValue="a">
        <option value="a">Assay A</option>
        <option value="b">Assay B</option>
      </Select>,
    );
    expect(trigger().querySelector(".alk-select__lead")).toBeNull();
    await user.click(trigger());
    const markOf = (name: string) => screen.getByRole("option", { name }).querySelector(".alk-select__option-mark")!;
    expect(markOf("Assay A").querySelectorAll("svg")).toHaveLength(1);
    expect(markOf("Assay B").querySelectorAll("svg")).toHaveLength(0);
  });
});

describe("Select root props (rootClassName / rootStyle)", () => {
  // The root props target the field wrapper — and must NEVER be dropped: shelled (label present)
  // they ride the FieldShell wrapper; bare (no label/description/error, FieldShell renders children
  // unwrapped) they land on the control's own root, so a toolbar Select can take a flex basis without
  // a wrapper <div>.
  it("applies rootClassName + rootStyle to the field wrapper when shelled", () => {
    render(
      <Select label="Role" rootClassName="cell" rootStyle={{ flex: "0 0 140px" }} value="a" onChange={() => {}}>
        <option value="a">Admin</option>
      </Select>,
    );
    const shell = document.querySelector(".alk-field");
    expect(shell).toHaveClass("cell");
    expect((shell as HTMLElement).style.flex).toBe("0 0 140px");
  });

  it("applies rootClassName + rootStyle to the control's own root when bare", () => {
    render(
      <Select aria-label="Role" rootClassName="cell" rootStyle={{ flex: "0 0 140px" }} value="a" onChange={() => {}}>
        <option value="a">Admin</option>
      </Select>,
    );
    expect(document.querySelector(".alk-field")).toBeNull();
    const root = document.querySelector(".alk-select-root") as HTMLElement;
    expect(root).toHaveClass("cell");
    expect(root.style.flex).toBe("0 0 140px");
  });
});

describe("a closed select's trigger label", () => {
  it("shows the shorter label it is given for the chosen option, and the full row in the list", () => {
    render(
      <Select aria-label="Connection" defaultValue="w" triggerLabels={{ w: "warehouse" }}>
        <option value="d">DuckDB</option>
        <option value="w">warehouse · PostgreSQL · Ada</option>
      </Select>,
    );
    const trigger = screen.getByRole("button", { name: "Connection" });
    expect(trigger).toHaveTextContent(/^warehouse$/);
    fireEvent.click(trigger);
    expect(screen.getByRole("option", { name: "warehouse · PostgreSQL · Ada" })).toBeInTheDocument();
  });
});
