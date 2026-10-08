import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { StatCard } from "./StatCard";

// Tests for the @alkera/ui StatCard (the KPI data card). The load-bearing invariant: the tall, bottom-pinned KPI layout is gated on a
// `viz` slot via the `data-has-viz` attribute — a viz-less card stays compact (its value sits right
// under its label, no height floor). Applying the floor unconditionally, or dropping the attribute,
// would open a large label-to-value gap on every viz-less card.

afterEach(cleanup);

describe("StatCard", () => {
  it("renders the note only when there is one", () => {
    const { container, rerender } = render(<StatCard label="Users" value="8" />);
    expect(container.querySelector(".alk-statcard__note")).toBeNull();

    rerender(<StatCard label="Organizations" value="42" note="Organizations on the platform" />);
    expect(container.querySelector(".alk-statcard__label")).toHaveTextContent("Organizations");
    expect(container.querySelector(".alk-statcard__value")).toHaveTextContent("42");
    expect(container.querySelector(".alk-statcard__note")).toHaveTextContent("Organizations on the platform");
  });

  // The tall, bottom-pinned KPI layout is gated on the viz slot. Marking a viz-less card would pin
  // its value to the bottom of the height floor and reopen the giant label-to-value gap.
  it("gates data-has-viz on the viz slot", () => {
    const { container, rerender } = render(<StatCard label="Spend" value="$0.08" />);
    expect(container.querySelector(".alk-statcard")).not.toHaveAttribute("data-has-viz");

    rerender(<StatCard label="Spend" value="$0.08" viz={<div data-testid="spark" />} />);
    const root = container.querySelector(".alk-statcard")!;
    expect(root).toHaveAttribute("data-has-viz");
    expect(root).toContainElement(screen.getByTestId("spark"));
  });

  it("renders the trend slot in the head row beside the label", () => {
    const { container } = render(<StatCard label="Spend" value="1" trend={<span>+2%</span>} />);
    expect(container.querySelector(".alk-statcard__head")).toHaveTextContent("+2%");
  });

  it("merges a caller className and forwards root props (the override contract)", () => {
    const { container } = render(<StatCard label="X" value="1" className="adm-stat" data-testid="kpi" />);
    const root = container.querySelector(".alk-statcard")!;
    expect(root).toHaveClass("alk-statcard", "adm-stat");
    expect(root).toHaveAttribute("data-testid", "kpi");
  });
});
