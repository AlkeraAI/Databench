import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useMediaQuery } from "./useMediaQuery";

// useMediaQuery is a public export, so its own contract is tested directly: it reads the live query
// on first paint, re-renders when the query flips (the `change` subscription — the only branch with
// logic), pins to false when disabled, and removes its listener on unmount. We drive a controllable
// matchMedia stub that stores the registered listener so a test can flip the match and fire it.

/** A matchMedia stub whose match state a test can flip and dispatch. `installMatchMedia` returns the
 *  handle that owns the current listeners + a `set(matches)` that flips and notifies them. */
function installMatchMedia(initial: boolean) {
  let matches = initial;
  const listeners = new Set<() => void>();
  const mql = {
    get matches() {
      return matches;
    },
    media: "",
    onchange: null,
    addEventListener: (_: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
    addListener: (cb: () => void) => listeners.add(cb),
    removeListener: (cb: () => void) => listeners.delete(cb),
    dispatchEvent: () => false,
  };
  window.matchMedia = ((query: string) => {
    mql.media = query;
    return mql;
  }) as unknown as typeof window.matchMedia;
  return {
    set(next: boolean) {
      matches = next;
      act(() => listeners.forEach((cb) => cb()));
    },
    get listenerCount() {
      return listeners.size;
    },
  };
}

/** A probe component that renders the hook's boolean so the test asserts through the DOM. */
function Probe({ query, enabled }: { query: string; enabled?: boolean }) {
  const matches = useMediaQuery(query, enabled);
  return <span data-testid="out">{matches ? "yes" : "no"}</span>;
}

const out = () => screen.getByTestId("out").textContent;

afterEach(cleanup);

describe("useMediaQuery", () => {
  it("reads the live query on first paint", () => {
    installMatchMedia(true);
    render(<Probe query="(max-width: 500px)" />);
    expect(out()).toBe("yes");
  });

  it("re-renders when the query flips true→false and back", () => {
    const mq = installMatchMedia(true);
    render(<Probe query="(max-width: 500px)" />);
    expect(out()).toBe("yes");
    mq.set(false);
    expect(out()).toBe("no");
    mq.set(true);
    expect(out()).toBe("yes");
  });

  it("pins to false and registers no listener when disabled", () => {
    const mq = installMatchMedia(true);
    render(<Probe query="(max-width: 500px)" enabled={false} />);
    expect(out()).toBe("no");
    expect(mq.listenerCount).toBe(0);
  });

  it("removes its listener on unmount (no leak)", () => {
    const mq = installMatchMedia(false);
    const view = render(<Probe query="(max-width: 500px)" />);
    expect(mq.listenerCount).toBe(1);
    view.unmount();
    expect(mq.listenerCount).toBe(0);
  });
});

describe("useMediaQuery — SSR / no window", () => {
  it("returns false without throwing when matchMedia is unavailable", () => {
    const original = window.matchMedia;
    // @ts-expect-error — deliberately remove the API to simulate the no-matchMedia environment.
    delete window.matchMedia;
    try {
      const spy = vi.spyOn(console, "error").mockImplementation(() => {});
      render(<Probe query="(max-width: 500px)" />);
      expect(out()).toBe("no");
      spy.mockRestore();
    } finally {
      window.matchMedia = original;
    }
  });
});
