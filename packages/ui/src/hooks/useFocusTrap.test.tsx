import { useRef } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useFocusTrap } from "./useFocusTrap";

// rAF shim on fake timers so the deferred initial-focus frame is steppable.
beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => window.setTimeout(() => cb(performance.now()), 16));
  vi.stubGlobal("cancelAnimationFrame", (id: number) => window.clearTimeout(id));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function Harness({ active }: { active: boolean }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(dialogRef, { active, onClose: () => {} });
  return (
    <>
      <button data-testid="opener">Add entry</button>
      <div ref={dialogRef}>
        <button data-testid="inner">Save</button>
      </div>
    </>
  );
}

describe("useFocusTrap — focus without scrolling the page behind the overlay", () => {
  it("focuses the initial child and restores the opener with { preventScroll: true }", () => {
    const view = render(<Harness active={false} />);
    const opener = screen.getByTestId("opener") as HTMLButtonElement;
    const inner = screen.getByTestId("inner") as HTMLButtonElement;
    const openerFocus = vi.spyOn(opener, "focus");
    const innerFocus = vi.spyOn(inner, "focus");

    // The opener is what has focus when the surface opens — the trap captures it as the return target.
    act(() => opener.focus());
    openerFocus.mockClear();

    view.rerender(<Harness active={true} />);
    act(() => {
      vi.advanceTimersByTime(32);
    });

    // Initial focus lands on the first focusable child, suppressing the scroll-into-view.
    expect(innerFocus).toHaveBeenCalledTimes(1);
    expect(innerFocus).toHaveBeenCalledWith({ preventScroll: true });

    view.rerender(<Harness active={false} />);

    // Closing restores focus to the opener — also without yanking the viewport to it.
    expect(openerFocus).toHaveBeenCalledTimes(1);
    expect(openerFocus).toHaveBeenCalledWith({ preventScroll: true });
  });
});
