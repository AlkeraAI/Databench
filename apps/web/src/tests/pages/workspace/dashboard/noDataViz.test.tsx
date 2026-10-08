import { render } from "@testing-library/react";
import { cleanup, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { MiniBars, Sparkline } from "@alkera/ui";

import { StatCard } from "@/pages/workspace/dashboard/components";
import type { Stat } from "@/pages/workspace/dashboard/model";

// Fix 5 — the no-data viz, verified on the RENDERED output (not the types). When a seat has activity
// but an empty catalog, the adapter passes [] for the bar/spark series; the viz must then render a
// muted "No data" baseline rather than a real-looking zero-height bar. These assert the accessible
// label flips to "No data" and that no value-bar/real-line is drawn — and that real series still
// render unchanged (the change is backward-compatible for existing consumers).

afterEach(cleanup);

describe("MiniBars no-data treatment", () => {
  it("renders a single muted baseline labelled No data for an empty series, with no value bars", () => {
    const { container } = render(<MiniBars values={[]} />);
    const svg = screen.getByRole("img", { name: "No data" });
    expect(svg.getAttribute("data-state")).toBe("empty");
    expect(container.querySelectorAll(".alk-bars__val")).toHaveLength(0);
    expect(container.querySelectorAll(".alk-bars__track")).toHaveLength(1);
  });

  it("collapses an all-zero series to the same no-data baseline (a zero bar is not data)", () => {
    const { container } = render(<MiniBars values={[0, 0, 0]} />);
    expect(screen.getByRole("img", { name: "No data" })).toBeInTheDocument();
    expect(container.querySelectorAll(".alk-bars__val")).toHaveLength(0);
  });

  it("renders one value bar per datum for a real series (backward-compatible)", () => {
    const { container } = render(<MiniBars values={[2, 1]} />);
    expect(screen.getByRole("img", { name: "Recent additions" })).toBeInTheDocument();
    expect(container.querySelectorAll(".alk-bars__val")).toHaveLength(2);
  });

  it("renders a single value as one centered column, not a full-width block", () => {
    const { container } = render(<MiniBars values={[5]} />);
    const val = container.querySelector(".alk-bars__val");
    expect(val).not.toBeNull();
    // A lone bar must be inset (centered) and narrower than the full 100-unit track, so it reads as
    // a bar rather than a solid filled box.
    expect(Number(val?.getAttribute("width"))).toBeLessThan(100);
    expect(Number(val?.getAttribute("x"))).toBeGreaterThan(0);
  });
});

describe("Sparkline no-data treatment", () => {
  it("renders a dashed muted baseline labelled No data for an empty series", () => {
    const { container } = render(<Sparkline points={[]} />);
    expect(screen.getByRole("img", { name: "No data" })).toBeInTheDocument();
    expect(container.querySelector(".alk-spark__line[data-state='empty']")).not.toBeNull();
  });

  // A single data point cannot draw a trend line, so it collapses to the No-data baseline rather
  // than a degenerate one-point line. A seat with exactly one day of activity hits this branch.
  it("collapses a single-point series to the No-data baseline", () => {
    const { container } = render(<Sparkline points={[5]} />);
    expect(screen.getByRole("img", { name: "No data" })).toBeInTheDocument();
    expect(container.querySelector(".alk-spark__line[data-state='empty']")).not.toBeNull();
  });

  // A multi-point all-zero series is "nothing measured", not a real flat-at-zero trend.
  it("collapses an all-zero multi-point series to the No-data baseline", () => {
    render(<Sparkline points={[0, 0, 0]} />);
    expect(screen.getByRole("img", { name: "No data" })).toBeInTheDocument();
  });

  it("renders the real area+line trend for a non-trivial series (backward-compatible)", () => {
    const { container } = render(<Sparkline points={[1, 4, 2]} />);
    expect(screen.getByRole("img", { name: "Trend, last period" })).toBeInTheDocument();
    expect(container.querySelector(".alk-spark__line[data-state='empty']")).toBeNull();
  });
});

describe("dashboard StatCard threads the empty series through to a No-data viz", () => {
  // The end-to-end fix-5 path: a bars stat with an empty series (what the adapter emits for an empty
  // catalog) must surface as the "No data" treatment on the card, never a phantom zero bar.
  it("shows the No-data viz for a bars stat with an empty series", () => {
    const s: Stat = {
      key: "catalog",
      label: "Catalog entries",
      value: "0",
      note: "shared datasets you can see",
      to: "/knowledge",
      viz: { kind: "bars", values: [], tip: "No datasets yet" },
    };
    render(
      <MemoryRouter>
        <StatCard s={s} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("img", { name: "No data" })).toBeInTheDocument();
  });
});
