import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
// fireEvent.pointerLeave needs no coordinates, so it's fine for leave; pointer moves go through a
// MouseEvent (see pointerMove) because jsdom drops clientX from a PointerEvent.
import { afterEach, describe, expect, it, vi } from "vitest";

import { Sparkline } from "./Sparkline";

// The Sparkline's on-hover tooltip. We assert observable behavior: moving the pointer over the plot
// shows the CALLER's content for the sample nearest the pointer, and leaving clears it. Geometry is
// resolved against the element's DOM rect, which jsdom reports as 0×0 — so we stub a real 100px-wide
// rect and drive pointer events at chosen x-coordinates.

afterEach(cleanup);

const POINTS = [10, 20, 30, 40, 50]; // 5 samples → edges at 0, 25, 50, 75, 100px in a 100px plot

const RECT = { left: 0, top: 0, right: 100, bottom: 40, width: 100, height: 40, x: 0, y: 0, toJSON() {} } as DOMRect;

/** Render a Sparkline whose tooltip renders "<value> @ <index>", with its rect stubbed to 100×40. */
function renderSpark(points = POINTS) {
  const view = render(
    <Sparkline points={points} tooltip={(value, index) => `${value} @ ${index}`} />,
  );
  const svg = view.container.querySelector("svg")!;
  svg.getBoundingClientRect = () => RECT;
  return { svg };
}

// jsdom's PointerEvent constructor drops clientX/clientY, but MouseEvent keeps them and React listens
// for the native `pointermove` all the same — so we dispatch a MouseEvent-typed pointermove.
function pointerMove(svg: Element, clientX: number) {
  act(() => {
    svg.dispatchEvent(new MouseEvent("pointermove", { clientX, clientY: 20, bubbles: true }));
  });
}

/** Fire a pointermove at `clientX` over the svg, flushing the presence timers so the tip mounts. */
function hoverAt(svg: Element, clientX: number) {
  pointerMove(svg, clientX);
  act(() => {
    vi.runOnlyPendingTimers();
  });
}

function tip(): HTMLElement | null {
  return screen.queryByRole("tooltip");
}

describe("Sparkline tooltip", () => {
  it("shows no tooltip at rest", () => {
    renderSpark();
    expect(tip()).toBeNull();
  });

  it("shows the caller's content for the sample nearest the pointer", () => {
    vi.useFakeTimers();
    const { svg } = renderSpark();
    // x=50px is the middle sample (index 2), whose value is 30.
    hoverAt(svg, 50);
    expect(within(tip()!).getByText("30 @ 2")).toBeInTheDocument();
    vi.useRealTimers();
  });

  it("updates the content as the pointer moves to a different sample", () => {
    vi.useFakeTimers();
    const { svg } = renderSpark();
    hoverAt(svg, 0); // first sample: value 10, index 0
    expect(screen.getByText("10 @ 0")).toBeInTheDocument();
    hoverAt(svg, 100); // last sample: value 50, index 4
    expect(screen.getByText("50 @ 4")).toBeInTheDocument();
    expect(screen.queryByText("10 @ 0")).toBeNull();
    vi.useRealTimers();
  });

  it("clears the tooltip on pointer-leave", () => {
    vi.useFakeTimers();
    const { svg } = renderSpark();
    hoverAt(svg, 50);
    expect(tip()).not.toBeNull();

    fireEvent.pointerLeave(svg);
    // usePresence keeps the node through its exit; under reduced-motion (the test harness reports it)
    // it unmounts on the next tick.
    act(() => {
      vi.runAllTimers();
    });
    expect(tip()).toBeNull();
    vi.useRealTimers();
  });

  describe("the painted box", () => {
    // The stroke is drawn in screen space and centred on the sample, so a plot that ran edge to
    // edge had its first and last samples half-clipped by the svg's own box. In a stat card that
    // read as a vertical stub pinned to the card's edge, past the padding every other element in
    // the card respects.
    const xs = (d: string): number[] =>
      [...d.matchAll(/[ML](-?[\d.]+),(-?[\d.]+)/g)].map((m) => Number(m[1]));

    const box = (svg: SVGSVGElement) => {
      const [minX, , width] = svg.getAttribute("viewBox")!.split(" ").map(Number);
      return { minX, maxX: minX + width };
    };

    it.each([
      ["a plotted series", POINTS],
      ["an empty series", [] as number[]],
    ])("keeps every drawn x strictly inside the viewBox on %s", (_label, points) => {
      const { container } = render(<Sparkline points={points} />);
      const svg = container.querySelector("svg")!;
      const { minX, maxX } = box(svg);
      const drawn = [...svg.querySelectorAll("path")].flatMap((p) => xs(p.getAttribute("d")!));

      expect(drawn.length).toBeGreaterThan(0);
      expect(Math.min(...drawn)).toBeGreaterThan(minX);
      expect(Math.max(...drawn)).toBeLessThan(maxX);
    });

    it("puts the hover marker on the sample's own x within that box", () => {
      // The gutter is part of the element's width, so a marker positioned on the plot's 0-100
      // fractions alone would sit off the line it marks.
      vi.useFakeTimers();
      const { svg } = renderSpark();
      // The last sample: its plot x of 100 sits at 101/102 of a 100px-wide element.
      hoverAt(svg, 99);
      expect(screen.getByText("50 @ 4")).toBeInTheDocument();
      vi.useRealTimers();
    });
  });

  it("is inert when no tooltip renderer is given (a static spark shows nothing on hover)", () => {
    const { container } = render(<Sparkline points={POINTS} />);
    const svg = container.querySelector("svg")!;
    svg.getBoundingClientRect = () => RECT;
    pointerMove(svg, 50);
    expect(tip()).toBeNull();
  });

  it("shows no tooltip on an empty series even with a renderer", () => {
    render(<Sparkline points={[]} tooltip={() => "x"} />);
    // The no-data branch renders a static baseline; there is nothing to hover.
    expect(tip()).toBeNull();
  });

  it.each([
    ["a flat non-zero series", [5, 5, 5], "5 @ 1", 50],
    ["a two-sample series", [3, 9], "9 @ 1", 100],
  ] as const)("still resolves the sample on %s", (_label, points, expected, x) => {
    // A flat series has a zero value range; the marker-y math must not divide by zero and leave the
    // tooltip unresolvable. A 2-sample series is the minimum interactive width.
    vi.useFakeTimers();
    const { svg } = renderSpark([...points]);
    hoverAt(svg, x);
    expect(screen.getByText(expected)).toBeInTheDocument();
    vi.useRealTimers();
  });
});
