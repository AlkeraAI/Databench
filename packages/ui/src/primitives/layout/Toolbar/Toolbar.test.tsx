import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Toolbar } from "./Toolbar";

afterEach(cleanup);

/** Wait past the hook's rAF-debounced measure tick. */
const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

function root(): HTMLElement {
  const el = document.querySelector<HTMLElement>(".alk-toolbar");
  if (!el) throw new Error("no toolbar");
  return el;
}

describe("Toolbar", () => {
  it("renders the leading cluster and the trailing end cluster in order", () => {
    render(
      <Toolbar end={<input aria-label="Search" />}>
        <button type="button">Filter</button>
      </Toolbar>,
    );
    const start = root().querySelector(".alk-toolbar__start");
    const end = root().querySelector(".alk-toolbar__end");
    expect(start).toContainElement(screen.getByRole("button", { name: "Filter" }));
    expect(end).toContainElement(screen.getByRole("textbox", { name: "Search" }));
    // Start precedes end in DOM order (reading order matches visual order).
    expect(start!.nextElementSibling).toBe(end);
  });

  it("renders no empty cluster wrappers when a slot is omitted", () => {
    render(<Toolbar end={<input aria-label="Search" />} />);
    expect(root().querySelector(".alk-toolbar__start")).toBeNull();
    expect(root().querySelector(".alk-toolbar__end")).not.toBeNull();
    cleanup();
    render(
      <Toolbar>
        <button type="button">Filter</button>
      </Toolbar>,
    );
    expect(root().querySelector(".alk-toolbar__start")).not.toBeNull();
    expect(root().querySelector(".alk-toolbar__end")).toBeNull();
  });

  it("forwards className and DOM props to the root", () => {
    render(
      <Toolbar className="mine" aria-label="Catalog controls">
        x
      </Toolbar>,
    );
    expect(root().className).toContain("mine");
    expect(root()).toHaveAttribute("aria-label", "Catalog controls");
  });
});

// The two-row reflow contract: the wrapped CSS (full-row clusters, stretched controls, a full-width
// search) keys off `data-wrapped` on the root, set by a measured ResizeObserver comparison of the
// clusters' offsets. jsdom has no layout, so the detection runs through mocked offsets.
describe("Toolbar wrap contract (data-wrapped)", () => {
  it("wraps when the end cluster measures below the start, and rejoins", async () => {
    // A ResizeObserver whose callback the test can fire, standing in for a real resize.
    const callbacks: ResizeObserverCallback[] = [];
    class FiringResizeObserver {
      constructor(cb: ResizeObserverCallback) {
        callbacks.push(cb);
      }
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    const RealRO = window.ResizeObserver;
    window.ResizeObserver = FiringResizeObserver as unknown as typeof ResizeObserver;
    const mockOffsets = (el: HTMLElement, top: number, height: number) => {
      Object.defineProperty(el, "offsetTop", { configurable: true, get: () => top });
      Object.defineProperty(el, "offsetHeight", { configurable: true, get: () => height });
    };
    const fire = async () => {
      for (const cb of callbacks) cb([], new RealRO(() => {}));
      await frame();
    };
    try {
      render(
        <Toolbar end={<input aria-label="Search" />}>
          <button type="button">Filter</button>
        </Toolbar>,
      );
      const start = root().querySelector<HTMLElement>(".alk-toolbar__start")!;
      const end = root().querySelector<HTMLElement>(".alk-toolbar__end")!;

      // Wrapped geometry: the end cluster's top at/below the start cluster's bottom.
      mockOffsets(start, 0, 38);
      mockOffsets(end, 46, 38);
      await fire();
      expect(root()).toHaveAttribute("data-wrapped");

      // Back on one row (different heights, centered — a small offsetTop delta is NOT a wrap).
      mockOffsets(end, 3, 32);
      await fire();
      expect(root()).not.toHaveAttribute("data-wrapped");
    } finally {
      window.ResizeObserver = RealRO;
    }
  });
});
