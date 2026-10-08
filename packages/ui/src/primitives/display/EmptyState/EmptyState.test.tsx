import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { EmptyState } from "./EmptyState";

// Tests for the @alkera/ui EmptyState — the centred state plate a data view resolves into. Focus
// is the `size` ladder (lg full-view / md in-card / sm inline note) callers pick to weight the plate.
//
// They assert the OBSERVABLE contract: the size data-attribute is the load-bearing seam (it's what a
// stylesheet keys the min-height + type scale off), the default is `lg` (so an existing whole-view
// empty keeps its full plate and emits NO data-size), and the tone/role/details contract still holds.

afterEach(cleanup);

function plate(): HTMLElement {
  return document.querySelector(".alk-emptystate") as HTMLElement;
}

describe("EmptyState size ladder", () => {
  // The base rule IS the lg scale, so lg carries no attribute whether it is defaulted or explicit.
  // An emitted lg would silently shrink every existing whole-view empty.
  it.each([
    { size: undefined, attr: null },
    { size: "lg" as const, attr: null },
    { size: "md" as const, attr: "md" },
    { size: "sm" as const, attr: "sm" },
  ])("size=$size sets data-size=$attr", ({ size, attr }) => {
    render(<EmptyState size={size} title="Nothing here" />);
    expect(plate()).toHaveClass("alk-emptystate");
    if (attr) expect(plate()).toHaveAttribute("data-size", attr);
    else expect(plate()).not.toHaveAttribute("data-size");
  });
});

describe("EmptyState contract composes with size", () => {
  // An md alert still asserts (role=alert) AND carries the md scale — proving the size prop composes
  // with the tone/role contract rather than overriding it.
  it("an md alert keeps role=alert and the alert tone", () => {
    render(<EmptyState size="md" tone="alert" title="It broke" details="boom (500)" />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(plate()).toHaveAttribute("data-size", "md");
    expect(plate()).toHaveAttribute("data-tone", "alert");
    // The raw detail tucks under a disclosure, never headlining.
    expect(screen.getByText("boom (500)")).toBeInTheDocument();
  });

  it("renders the title, body, and action at any size", () => {
    render(
      <EmptyState
        size="sm"
        title="No matches"
        body="Try another subject."
        action={<button>Add entry</button>}
      />,
    );
    expect(screen.getByText("No matches")).toBeInTheDocument();
    expect(screen.getByText("Try another subject.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add entry" })).toBeInTheDocument();
  });
});
