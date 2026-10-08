import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SortHeader } from "./SortHeader";

// A real <button> whose accessible name carries the sort state, a caret that flips via its data-dir
// attribute, and the active/align axes as data-attributes. aria-sort belongs to the consumer's <th>,
// so it is deliberately not here.

afterEach(cleanup);

function caretEl(): HTMLElement {
  return document.querySelector(".alk-sortheader__caret") as HTMLElement;
}

describe("SortHeader sorting", () => {
  it("sorts on click without submitting a wrapping form", async () => {
    const onSort = vi.fn();
    render(<SortHeader onSort={onSort}>Credits</SortHeader>);
    const btn = screen.getByRole("button", { name: "Credits" });
    expect(btn).toHaveAttribute("type", "button");
    await userEvent.click(btn);
    expect(onSort).toHaveBeenCalledTimes(1);
  });
});

describe("SortHeader caret direction", () => {
  it("previews asc by default and flips with the direction", () => {
    const { rerender } = render(<SortHeader onSort={() => {}}>Credits</SortHeader>);
    expect(caretEl()).toHaveAttribute("data-dir", "asc");
    expect(caretEl()).toHaveAttribute("aria-hidden", "true");

    rerender(
      <SortHeader active direction="desc" onSort={() => {}}>
        Credits
      </SortHeader>,
    );
    expect(caretEl()).toHaveAttribute("data-dir", "desc");
  });
});

describe("SortHeader accessible name", () => {
  it.each([
    { direction: "asc" as const, state: /sorted ascending/i },
    { direction: "desc" as const, state: /sorted descending/i },
  ])("announces the $direction sort state", ({ direction, state }) => {
    render(
      <SortHeader active direction={direction} onSort={() => {}}>
        Credits
      </SortHeader>,
    );
    expect(screen.getByRole("button", { name: /credits/i })).toHaveAccessibleName(state);
  });

  // A screen reader must not hear a phantom "sorted" column.
  it("carries no marker or state text while inactive", () => {
    render(<SortHeader onSort={() => {}}>Credits</SortHeader>);
    const btn = screen.getByRole("button");
    expect(btn).not.toHaveAttribute("data-active");
    expect(btn).toHaveAccessibleName("Credits");
    expect(document.querySelector(".alk-sortheader__state")).toBeNull();
  });
});

describe("SortHeader align", () => {
  it("carries data-align only for a non-default alignment", () => {
    const { rerender } = render(<SortHeader onSort={() => {}}>Credits</SortHeader>);
    expect(screen.getByRole("button")).not.toHaveAttribute("data-align");
    rerender(
      <SortHeader align="end" onSort={() => {}}>
        Credits
      </SortHeader>,
    );
    expect(screen.getByRole("button")).toHaveAttribute("data-align", "end");
  });
});

describe("SortHeader className", () => {
  it("merges a caller className onto the root", () => {
    render(
      <SortHeader className="pg-custom" onSort={() => {}}>
        Credits
      </SortHeader>,
    );
    expect(screen.getByRole("button")).toHaveClass("alk-sortheader", "pg-custom");
  });
});
