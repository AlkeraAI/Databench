// A menu opens inside what can be seen of it, even in a pane about 530 px wide
// beside a chat. The placement is a pure function of the button, the list and
// the visible bounds, pinned here.

import { fireEvent, render, screen } from "@testing-library/react";

import { Menu, placeMenu, visibleBounds, type Box } from "./Menu";

const box = (left: number, top: number, right: number, bottom: number): Box => ({ left, top, right, bottom });
/** A pane 530 px wide, beside a chat, in a 1500 x 1000 window. */
const PANE = box(965, 64, 1495, 1000);
const ORIGIN = box(0, 0, 0, 0);

describe("placeMenu", () => {
  it("opens on its aligned side when that fits", () => {
    const at = placeMenu(box(1000, 140, 1060, 170), { width: 220, height: 120 }, PANE, "start", ORIGIN);
    expect(at).toMatchObject({ left: 1000, top: 174, above: false });
  });

  it("slides an end-aligned list back in rather than past the pane's left edge", () => {
    // The Panels button sits near the pane's left edge when the toolbar wraps.
    const at = placeMenu(box(980, 175, 1048, 200), { width: 220, height: 130 }, PANE, "end", ORIGIN);
    expect(at.left).toBe(PANE.left + 4);
    expect(at.left + 220).toBeLessThanOrEqual(PANE.right);
  });

  it("slides a start-aligned list back in rather than past the window's right edge", () => {
    const at = placeMenu(box(1370, 140, 1430, 170), { width: 320, height: 90 }, PANE, "start", ORIGIN);
    expect(at.left + 320).toBe(PANE.right - 4);
    expect(at.left).toBeGreaterThanOrEqual(PANE.left);
  });

  it("is never wider than it can be shown: a list wider than the bounds keeps its left edge in", () => {
    const at = placeMenu(box(1000, 140, 1060, 170), { width: 900, height: 90 }, PANE, "end", ORIGIN);
    expect(at.left).toBe(PANE.left + 4);
  });

  it("opens above its button when there is no room below and more above", () => {
    const at = placeMenu(box(1000, 900, 1060, 930), { width: 220, height: 200 }, PANE, "start", ORIGIN);
    expect(at.above).toBe(true);
    expect(at.top + 200).toBe(900 - 4);
    expect(at.maxHeight).toBeGreaterThanOrEqual(200);
  });

  it("stays below and caps its height when below still has more room than above", () => {
    const at = placeMenu(box(1000, 300, 1060, 330), { width: 220, height: 2000 }, PANE, "start", ORIGIN);
    expect(at.above).toBe(false);
    expect(at.top).toBe(334);
    expect(at.maxHeight).toBe(PANE.bottom - 4 - 334);
  });

  it("is placed relative to the box it is positioned against", () => {
    const at = placeMenu(box(1000, 140, 1060, 170), { width: 220, height: 120 }, PANE, "start", box(990, 130, 1070, 170));
    expect(at).toMatchObject({ left: 10, top: 44 });
  });
});

describe("visibleBounds", () => {
  it("is the window cut down by every ancestor that clips its overflow", () => {
    Object.assign(window, { innerWidth: 1500, innerHeight: 1000 });
    const { container } = render(
      <div data-testid="pane" style={{ overflow: "hidden" }}>
        <div style={{ overflowY: "auto" }} data-testid="scroller">
          <span data-testid="inside" />
        </div>
      </div>,
    );
    const pane = screen.getByTestId("pane");
    const scroller = screen.getByTestId("scroller");
    pane.getBoundingClientRect = () => ({ ...box(965, 64, 1495, 1000), width: 530, height: 936, x: 965, y: 64, toJSON: () => ({}) });
    scroller.getBoundingClientRect = () => ({ ...box(900, 120, 1400, 1200), width: 500, height: 1080, x: 900, y: 120, toJSON: () => ({}) });
    expect(visibleBounds(screen.getByTestId("inside"))).toEqual(box(965, 120, 1400, 1000));
    expect(container).toBeTruthy();
  });
});

describe("Menu", () => {
  it("places its list from the measured boxes before it shows it", () => {
    const { container } = render(
      <div style={{ overflow: "hidden" }} data-testid="pane">
        <Menu label="Panels" items={[{ id: "graph", label: "Graph" }]} onPick={() => {}} />
      </div>,
    );
    const pane = screen.getByTestId("pane");
    pane.getBoundingClientRect = () => ({ ...PANE, width: 530, height: 936, x: PANE.left, y: PANE.top, toJSON: () => ({}) });
    const button = screen.getByRole("button", { name: "Panels" });
    button.getBoundingClientRect = () => ({ ...box(980, 175, 1048, 200), width: 68, height: 25, x: 980, y: 175, toJSON: () => ({}) });
    const root = container.querySelector(".nb-menu") as HTMLElement;
    root.getBoundingClientRect = () => ({ ...box(980, 175, 1048, 200), width: 68, height: 25, x: 980, y: 175, toJSON: () => ({}) });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 220 });
    try {
      fireEvent.click(button);
      const list = screen.getByRole("menu", { name: "Panels" });
      // End-aligned, it would start at 1048 - 220 = 828: inside the chat. It
      // is slid back to the pane's left edge instead, relative to its menu.
      expect(list.style.left).toBe(`${PANE.left + 4 - 980}px`);
      expect(list.style.right).toBe("auto");
      expect(list).toHaveAttribute("data-placed", "true");
      expect(list.style.visibility).toBe("");
    } finally {
      delete (HTMLElement.prototype as { offsetWidth?: number }).offsetWidth;
    }
  });
});
