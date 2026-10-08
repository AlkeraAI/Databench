import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_TOAST_DURATION,
  TOAST_EXIT_FALLBACK_MS,
  ToastViewport,
  resolveToastDuration,
  useToasts,
} from "./Toast";

// A toast must always be dismissable: an invalid timer falls back to the default ONLY when there is
// no close button. The hook owns the queue; the viewport owns the lifecycle and emits `onDismiss`.

afterEach(cleanup);

describe("resolveToastDuration", () => {
  // Omitted is distinct from an explicit `null`, which is the "no timer" request.
  it.each([true, false])("an omitted duration uses the default (closable=%s)", (closable) => {
    expect(resolveToastDuration(undefined, closable)).toBe(DEFAULT_TOAST_DURATION);
  });

  // The predicate is `d > 0`, not integer-ness — a fractional or minimal positive is still valid.
  it.each([
    { duration: 4000, label: "typical" },
    { duration: 1, label: "one ms" },
    { duration: 0.5, label: "fractional ms" },
    { duration: Number.MIN_VALUE, label: "smallest positive double" },
  ] as const)("honours a positive duration ($label)", ({ duration }) => {
    expect(resolveToastDuration(duration, true)).toBe(duration);
    expect(resolveToastDuration(duration, false)).toBe(duration);
  });

  // Both columns per case, because the asymmetry is the rule: an invalid timer leaves the toast
  // sticky only when a close button exists, otherwise it would be stuck on screen forever.
  it.each([
    { duration: null, label: "explicit null" },
    { duration: 0, label: "zero" },
    { duration: -1, label: "negative" },
    { duration: -0.5, label: "negative fractional" },
    { duration: Number.NaN, label: "NaN" },
    { duration: Number.POSITIVE_INFINITY, label: "+Infinity" },
    { duration: Number.NEGATIVE_INFINITY, label: "-Infinity" },
  ] as const)("an invalid timer ($label) is sticky only when closable", ({ duration }) => {
    expect(resolveToastDuration(duration, true)).toBeNull();
    expect(resolveToastDuration(duration, false)).toBe(DEFAULT_TOAST_DURATION);
  });
});

describe("useToasts — the queue", () => {
  it("push appends in order and returns the monotonic id", () => {
    const { result } = renderHook(() => useToasts());
    let firstId = "";
    act(() => {
      firstId = result.current.push({ message: "one" });
    });
    expect(result.current.toasts).toHaveLength(1);
    expect(firstId).toBe("toast-0");
    let secondId = "";
    act(() => {
      secondId = result.current.push({ message: "two" });
    });
    expect(secondId).toBe("toast-1");
    expect(result.current.toasts.map((t) => t.id)).toEqual(["toast-0", "toast-1"]);
  });

  it("a supplied id does not consume the auto-counter", () => {
    const { result } = renderHook(() => useToasts());
    act(() => {
      result.current.push({ id: "manual", message: "m" });
    });
    let auto = "";
    act(() => {
      auto = result.current.push({ message: "auto" });
    });
    expect(auto).toBe("toast-0");
  });

  it("re-pushing an id de-dupes and moves it to the tail", () => {
    // De-dupe is "remove then append", so `max` (keep-the-newest) can't drop a re-pushed toast.
    const { result } = renderHook(() => useToasts());
    act(() => {
      result.current.push({ id: "a", message: "a1" });
    });
    act(() => {
      result.current.push({ id: "b", message: "b1" });
    });
    act(() => {
      result.current.push({ id: "a", message: "a2" });
    });
    expect(result.current.toasts.map((t) => t.id)).toEqual(["b", "a"]);
    expect(result.current.toasts.map((t) => t.message)).toEqual(["b1", "a2"]);
  });

  it("dismiss removes only the named toast; clear empties the queue", () => {
    const { result } = renderHook(() => useToasts());
    let idA = "";
    act(() => {
      idA = result.current.push({ message: "a" });
      result.current.push({ message: "b" });
    });
    act(() => result.current.dismiss(idA));
    expect(result.current.toasts.map((t) => t.message)).toEqual(["b"]);
    act(() => result.current.clear());
    expect(result.current.toasts).toHaveLength(0);
  });

  it("dismiss with an unknown id leaves the queue intact", () => {
    const { result } = renderHook(() => useToasts());
    act(() => {
      result.current.push({ message: "a" });
    });
    act(() => result.current.dismiss("nope"));
    expect(result.current.toasts.map((t) => t.message)).toEqual(["a"]);
  });
});

describe("ToastViewport — rendering", () => {
  // `data-tone` is the seam the icon-ink CSS keys off. Danger is the one assertive tone.
  it.each([
    { tone: "brand", role: "status" },
    { tone: "info", role: "status" },
    { tone: "success", role: "status" },
    { tone: "warning", role: "status" },
    { tone: "danger", role: "alert" },
  ] as const)("the $tone tone announces via role=$role", ({ tone, role }) => {
    render(<ToastViewport toasts={[{ id: "x", tone, message: "toned" }]} onDismiss={() => {}} />);
    const main = document.querySelector(".alk-toast__main");
    expect(main).toHaveAttribute("data-tone", tone);
    expect(main).toHaveAttribute("role", role);
    expect(main?.querySelector(".alk-toast__icon svg")).not.toBeNull();
  });

  it("defaults to the info tone when none is given", () => {
    render(<ToastViewport toasts={[{ id: "x", message: "plain" }]} onDismiss={() => {}} />);
    expect(document.querySelector(".alk-toast__main")).toHaveAttribute("data-tone", "info");
  });

  it("renders nothing when the queue is empty", () => {
    const { container } = render(<ToastViewport toasts={[]} onDismiss={() => {}} />);
    expect(container.querySelector(".alk-toast-viewport")).toBeNull();
    expect(document.querySelector(".alk-toast-viewport")).toBeNull();
  });

  it("renders the message, a close button, and the given position", () => {
    render(
      <ToastViewport
        position="top-center"
        toasts={[{ id: "x", message: "Member invited" }]}
        onDismiss={() => {}}
      />,
    );
    expect(screen.getByText("Member invited")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
    expect(document.querySelector(".alk-toast-viewport")).toHaveAttribute("data-position", "top-center");
  });

  it("omits the close button when closable is false", () => {
    render(<ToastViewport toasts={[{ id: "x", message: "No close", closable: false }]} onDismiss={() => {}} />);
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
  });

  it("caps the visible count at max, keeping the newest", () => {
    render(
      <ToastViewport
        max={2}
        toasts={[
          { id: "a", message: "first" },
          { id: "b", message: "second" },
          { id: "c", message: "third" },
        ]}
        onDismiss={() => {}}
      />,
    );
    expect(screen.queryByText("first")).toBeNull();
    expect(screen.getByText("second")).toBeInTheDocument();
    expect(screen.getByText("third")).toBeInTheDocument();
  });

  // A top-pinned stack reverses the list so the newest paints over the one behind it; a bottom stack
  // keeps queue order.
  it.each([
    { position: "bottom-right", order: ["older", "newer"] },
    { position: "top-right", order: ["newer", "older"] },
  ] as const)("a $position stack paints $order", ({ position, order }) => {
    render(
      <ToastViewport
        position={position}
        toasts={[
          { id: "a", message: "older" },
          { id: "b", message: "newer" },
        ]}
        onDismiss={() => {}}
      />,
    );
    const texts = Array.from(document.querySelectorAll(".alk-toast")).map((n) => n.textContent);
    expect(texts[0]).toContain(order[0]);
    expect(texts[1]).toContain(order[1]);
  });
});

describe("ToastViewport — lifecycle", () => {
  // The viewport is controlled: its observable output is the `onDismiss(id)` call and its timing.
  // The suite's test-setup reports prefers-reduced-motion as matching, so exit removal collapses to 0.
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("dismisses once at the deadline, not a tick before", () => {
    const duration = 3000;
    const onDismiss = vi.fn();
    render(<ToastViewport toasts={[{ id: "x", message: "Bye soon", duration }]} onDismiss={onDismiss} />);
    act(() => vi.advanceTimersByTime(duration - 1));
    expect(onDismiss).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    act(() => vi.runOnlyPendingTimers());
    expect(onDismiss).toHaveBeenCalledWith("x");
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("never dismisses a sticky toast across a long advance", () => {
    const onDismiss = vi.fn();
    render(<ToastViewport toasts={[{ id: "x", message: "Stay", duration: null }]} onDismiss={onDismiss} />);
    act(() => vi.advanceTimersByTime(DEFAULT_TOAST_DURATION * 3));
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("auto-dismisses the undismissable combo (no timer, no close)", () => {
    const onDismiss = vi.fn();
    render(
      <ToastViewport
        toasts={[{ id: "x", message: "Cannot get stuck", duration: null, closable: false }]}
        onDismiss={onDismiss}
      />,
    );
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
    act(() => vi.advanceTimersByTime(DEFAULT_TOAST_DURATION - 1));
    expect(onDismiss).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    act(() => vi.runOnlyPendingTimers());
    expect(onDismiss).toHaveBeenCalledWith("x");
  });

  it("closing early dismisses once and cancels the auto-timer", () => {
    const duration = 3000;
    const onDismiss = vi.fn();
    render(<ToastViewport toasts={[{ id: "x", message: "Click then wait", duration }]} onDismiss={onDismiss} />);
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    });
    act(() => vi.runOnlyPendingTimers());
    expect(onDismiss).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(DEFAULT_TOAST_DURATION));
    act(() => vi.runOnlyPendingTimers());
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  // Hover pauses the auto-dismiss and resumes from what REMAINED, never a full restart.
  // EXPECTED_REMAINING is derived so the test pins the arithmetic the implementation must do.
  const HOVER_DURATION = 3000;
  const PRE_PAUSE_ELAPSED = 2000;
  const EXPECTED_REMAINING = HOVER_DURATION - PRE_PAUSE_ELAPSED;

  function renderTimedToast(overrides: Record<string, unknown> = {}) {
    const onDismiss = vi.fn();
    render(
      <ToastViewport
        toasts={[{ id: "x", message: "Hover me", duration: HOVER_DURATION, ...overrides }]}
        onDismiss={onDismiss}
      />,
    );
    const toast = document.querySelector(".alk-toast")!;
    return { onDismiss, toast };
  }

  it("hover pauses, and leaving resumes from the banked remainder", () => {
    // Firing at exactly EXPECTED_REMAINING rules out both a full restart and a too-early resume.
    const { onDismiss, toast } = renderTimedToast();
    act(() => vi.advanceTimersByTime(PRE_PAUSE_ELAPSED));
    act(() => fireEvent.mouseEnter(toast));
    act(() => vi.advanceTimersByTime(HOVER_DURATION * 5));
    expect(onDismiss).not.toHaveBeenCalled();
    act(() => fireEvent.mouseLeave(toast));
    act(() => vi.advanceTimersByTime(EXPECTED_REMAINING - 1));
    expect(onDismiss).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    act(() => vi.runOnlyPendingTimers());
    expect(onDismiss).toHaveBeenCalledWith("x");
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("banks elapsed across multiple hover cycles", () => {
    // Three bursts must sum to HOVER_DURATION; a reset on any leave would fire early or never.
    const { onDismiss, toast } = renderTimedToast();
    const BURST = HOVER_DURATION / 3;
    act(() => vi.advanceTimersByTime(BURST));
    act(() => fireEvent.mouseEnter(toast));
    act(() => vi.advanceTimersByTime(HOVER_DURATION * 4));
    act(() => fireEvent.mouseLeave(toast));
    expect(onDismiss).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(BURST));
    act(() => fireEvent.mouseEnter(toast));
    act(() => vi.advanceTimersByTime(HOVER_DURATION * 4));
    act(() => fireEvent.mouseLeave(toast));
    expect(onDismiss).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(BURST - 1));
    expect(onDismiss).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    act(() => vi.runOnlyPendingTimers());
    expect(onDismiss).toHaveBeenCalledWith("x");
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("a sticky toast never starts a countdown on hover", () => {
    // Catches a resume that defaults a missing remainder to DEFAULT_TOAST_DURATION.
    const { onDismiss, toast } = renderTimedToast({ duration: null });
    act(() => fireEvent.mouseEnter(toast));
    act(() => vi.advanceTimersByTime(DEFAULT_TOAST_DURATION * 3));
    act(() => fireEvent.mouseLeave(toast));
    act(() => vi.advanceTimersByTime(DEFAULT_TOAST_DURATION * 3));
    act(() => vi.runOnlyPendingTimers());
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("a 1ms remainder still resumes and fires on leave", () => {
    // A falsy/<=0 remainder check would strand the toast here.
    const { onDismiss, toast } = renderTimedToast();
    act(() => vi.advanceTimersByTime(HOVER_DURATION - 1));
    act(() => fireEvent.mouseEnter(toast));
    act(() => vi.advanceTimersByTime(HOVER_DURATION * 3));
    expect(onDismiss).not.toHaveBeenCalled();
    act(() => fireEvent.mouseLeave(toast));
    act(() => vi.advanceTimersByTime(1));
    act(() => vi.runOnlyPendingTimers());
    expect(onDismiss).toHaveBeenCalledWith("x");
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("a re-pushed toast restarts its auto-dismiss timer", () => {
    // The Saving... -> Saved pattern: without the per-push remount the update would inherit the
    // original clock and vanish on the old schedule.
    function RepushHarness() {
      const { toasts, push, dismiss } = useToasts();
      return (
        <>
          <button type="button" onClick={() => push({ id: "x", message: "Saving", duration: 3000 })}>
            save
          </button>
          <button type="button" onClick={() => push({ id: "x", message: "Saved", duration: 3000 })}>
            done
          </button>
          <ToastViewport toasts={toasts} onDismiss={dismiss} />
        </>
      );
    }
    render(<RepushHarness />);
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "save" }));
    });
    expect(screen.getByText("Saving")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(2500));
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "done" }));
    });
    expect(screen.getByText("Saved")).toBeInTheDocument();
    // Past the ORIGINAL deadline (2500 + 1000 > 3000) the updated toast is still up.
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByText("Saved")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(2000));
    act(() => vi.runOnlyPendingTimers());
    expect(screen.queryByText("Saved")).toBeNull();
  });

  // Every test above takes the instant-dismiss branch. These force motion ON: the unmount rides the
  // slot's grid-template-rows transitionend, with the fallback timer as the only other way out.
  // jsdom runs no CSS transitions, so the event is dispatched by hand.
  describe("motion-enabled exit", () => {
    beforeEach(() => {
      vi.spyOn(window, "matchMedia").mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }));
    });
    afterEach(() => vi.restoreAllMocks());

    function closeOneToast() {
      const onDismiss = vi.fn();
      render(<ToastViewport toasts={[{ id: "x", message: "Bye", duration: null }]} onDismiss={onDismiss} />);
      act(() => {
        fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
      });
      const slot = document.querySelector(".alk-toast-slot")!;
      return { onDismiss, slot };
    }

    function endTransition(el: Element, propertyName: string) {
      const ev = new Event("transitionend", { bubbles: true });
      Object.assign(ev, { propertyName });
      act(() => {
        el.dispatchEvent(ev);
      });
    }

    it("unmounts on the row collapse, and only on that transition", () => {
      const { onDismiss, slot } = closeOneToast();
      expect(onDismiss).not.toHaveBeenCalled();
      // The card's fade bubbling up must not unmount early.
      endTransition(slot.querySelector(".alk-toast")!, "opacity");
      endTransition(slot, "opacity");
      expect(onDismiss).not.toHaveBeenCalled();
      endTransition(slot, "grid-template-rows");
      expect(onDismiss).toHaveBeenCalledWith("x");
      expect(onDismiss).toHaveBeenCalledTimes(1);
    });

    it("falls back to the timer when no transition ends", () => {
      const { onDismiss } = closeOneToast();
      act(() => vi.advanceTimersByTime(TOAST_EXIT_FALLBACK_MS - 1));
      expect(onDismiss).not.toHaveBeenCalled();
      act(() => vi.advanceTimersByTime(1));
      expect(onDismiss).toHaveBeenCalledWith("x");
      expect(onDismiss).toHaveBeenCalledTimes(1);
    });

    it("a transitionend after the fallback does not dismiss twice", () => {
      const { onDismiss, slot } = closeOneToast();
      act(() => vi.advanceTimersByTime(TOAST_EXIT_FALLBACK_MS));
      endTransition(slot, "grid-template-rows");
      expect(onDismiss).toHaveBeenCalledTimes(1);
    });
  });
});
