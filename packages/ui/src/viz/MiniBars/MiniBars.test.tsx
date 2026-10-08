import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MiniBars } from "./MiniBars";

// MiniBars' on-hover tooltip — the bar-chart half of the shared viz-tooltip contract. The bar under
// the pointer must resolve to the right (value, index) using the DERIVED bar centres (not an even
// grid), the caller's content must show, and leaving must clear it. jsdom reports a 0×0 rect, so we
// stub a 100px-wide rect and drive pointer moves at chosen x's that land inside specific bars.

afterEach(cleanup);

const VALUES = [4, 8, 2, 6]; // 4 bars → with gap=4, bar centres land near 12%, 37%, 62%, 87%

const RECT = { left: 0, top: 0, right: 100, bottom: 40, width: 100, height: 40, x: 0, y: 0, toJSON() {} } as DOMRect;

function renderBars(values = VALUES) {
  const view = render(<MiniBars values={values} tooltip={(value, index) => `${value} @ ${index}`} />);
  const svg = view.container.querySelector("svg")!;
  svg.getBoundingClientRect = () => RECT;
  return { svg };
}

// jsdom's PointerEvent drops clientX; a MouseEvent-typed pointermove keeps it and React still fires.
function pointerMove(svg: Element, clientX: number) {
  act(() => {
    svg.dispatchEvent(new MouseEvent("pointermove", { clientX, clientY: 20, bubbles: true }));
  });
}

function tip(): HTMLElement | null {
  return screen.queryByRole("tooltip");
}

describe("MiniBars tone", () => {
  // `tone` sets the shared --alk-viz-color var INLINE on the svg root to the tone's solid token, so
  // every paint rule keyed off the var (bars, hover dot, tooltip capture) reads the toned series
  // colour. Each tone maps to its own token — pinned per tone so a collapsed map (every tone → brand)
  // fails.
  it.each([
    { tone: "brand", token: "var(--alkBrand)" },
    { tone: "neutral", token: "var(--alkCatNeutralSolid)" },
    { tone: "info", token: "var(--alkInfo)" },
    { tone: "success", token: "var(--alkSuccess)" },
    { tone: "warning", token: "var(--alkWarning)" },
    { tone: "danger", token: "var(--alkDanger)" },
  ] as const)("tone=$tone sets --alk-viz-color to $token", ({ tone, token }) => {
    const { container } = render(<MiniBars values={VALUES} tone={tone} />);
    const svg = container.querySelector("svg")!;
    expect(svg.style.getPropertyValue("--alk-viz-color")).toBe(token);
  });

  it("leaves --alk-viz-color unset without a tone (the ambient series colour flows as before)", () => {
    const { container } = render(<MiniBars values={VALUES} />);
    expect(container.querySelector("svg")!.style.getPropertyValue("--alk-viz-color")).toBe("");
  });

  it("tones the no-data baseline svg too, so an empty series stays in the same scope", () => {
    const { container } = render(<MiniBars values={[0, 0, 0]} tone="info" />);
    const svg = container.querySelector("svg")!;
    expect(svg).toHaveAttribute("data-state", "empty");
    expect(svg.style.getPropertyValue("--alk-viz-color")).toBe("var(--alkInfo)");
  });
});

describe("MiniBars tooltip", () => {
  it("shows no tooltip at rest", () => {
    renderBars();
    expect(tip()).toBeNull();
  });

  it("resolves the bar under the pointer via bar-centre geometry, not an even grid", () => {
    // x=10px sits inside the first bar (centre ~12%), x=90px inside the last (centre ~87%). An even
    // i/(n-1) grid would put samples at 0/33/66/100 and mis-resolve x=90 to bar 2, not bar 3.
    const { svg } = renderBars();
    pointerMove(svg, 10);
    expect(screen.getByText("4 @ 0")).toBeInTheDocument();
    pointerMove(svg, 90);
    expect(screen.getByText("6 @ 3")).toBeInTheDocument();
    expect(screen.queryByText("4 @ 0")).toBeNull();
  });

  it("clears the tooltip on pointer-leave", () => {
    vi.useFakeTimers();
    const { svg } = renderBars();
    pointerMove(svg, 40);
    expect(tip()).not.toBeNull();
    fireEvent.pointerLeave(svg);
    act(() => {
      vi.runAllTimers();
    });
    expect(tip()).toBeNull();
    vi.useRealTimers();
  });

  it("is inert with no tooltip renderer", () => {
    const { container } = render(<MiniBars values={VALUES} />);
    const svg = container.querySelector("svg")!;
    svg.getBoundingClientRect = () => RECT;
    pointerMove(svg, 40);
    expect(tip()).toBeNull();
  });

  it("shows no tooltip on an all-zero series even with a renderer (nothing to read)", () => {
    render(<MiniBars values={[0, 0, 0]} tooltip={() => "x"} />);
    expect(tip()).toBeNull();
  });
});
