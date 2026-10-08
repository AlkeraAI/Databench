// What the browser remembers about a layout, and what it refuses to.
//
// Every case here is about a promise the hook makes to a caller that has no
// other way to check: a stored width is inside the range the page can draw, a
// store that throws costs the reader their layout and nothing else, and a key
// that varies — one per chat — cannot grow the record without end.

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  LRU_KEEP,
  SIZE_COMMIT_MS,
  clampSize,
  hasSize,
  readSize,
  usePersistedSize,
  writeSize,
  type SizeBounds,
} from "@/app/usePersistedSize";

const BOUNDS: SizeBounds = { min: 200, max: 600, size: 320 };

function raw(key: string): string | null {
  return window.localStorage.getItem(`alkera.size:${key}`);
}

function lru(namespace: string): string[] {
  const stored = window.localStorage.getItem(`alkera.size.lru:${namespace}`);
  return stored ? (JSON.parse(stored) as string[]) : [];
}

beforeEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});
afterEach(() => {
  // The throwing-store cases replace `localStorage` wholesale, so the stub comes
  // off before anything tries to use the real one again.
  vi.unstubAllGlobals();
  window.localStorage.clear();
  vi.useRealTimers();
});

describe("a width is only ever one the page can draw", () => {
  it("clamps a stored width into the range, in both directions", () => {
    writeSize("p", { collapsed: false, width: 5_000 }, BOUNDS);
    expect(readSize("p", BOUNDS).width).toBe(BOUNDS.max);
    writeSize("p", { collapsed: false, width: 1 }, BOUNDS);
    expect(readSize("p", BOUNDS).width).toBe(BOUNDS.min);
  });

  it("honours a narrowed ceiling — the window the reader is on now", () => {
    writeSize("p", { collapsed: false, width: 590 }, BOUNDS);
    // The same stored value, read on a window that can only give 400.
    expect(readSize("p", BOUNDS, { bounds: { max: 400 } }).width).toBe(400);
    // …and on the wide monitor again, the width the reader chose is still there.
    expect(readSize("p", BOUNDS).width).toBe(590);
  });

  it("keeps the floor when the viewport is narrower than the pane's minimum", () => {
    // min above max: the layout squeezes, the number never goes negative.
    expect(clampSize(50, { min: 200, max: 120, size: 320 })).toBe(200);
  });

  it("reads a value that is not a width at all as nothing stored", () => {
    for (const junk of ['{"width":"wide"}', '{"width":null}', "not json", '{"width":null}', "[]"]) {
      window.localStorage.setItem("alkera.size:p", junk);
      expect(readSize("p", BOUNDS).width).toBe(BOUNDS.size);
    }
  });

  it("stores the clamped width, not the one it was handed", () => {
    writeSize("p", { collapsed: false, width: 5_000 }, BOUNDS);
    expect(JSON.parse(raw("p")!)).toEqual({ collapsed: false, width: BOUNDS.max });
  });
});

describe("a store that will not answer", () => {
  it("reads as nothing stored rather than throwing", () => {
    const angry = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    };
    vi.stubGlobal("localStorage", angry as unknown as Storage);
    expect(() => writeSize("p", { collapsed: true, width: 400 }, BOUNDS)).not.toThrow();
    // A key this browser was never told about reads as nothing stored.
    expect(readSize("never-set", BOUNDS)).toEqual({ collapsed: false, width: BOUNDS.size });
    expect(hasSize("never-set")).toBe(false);
    // The width the reader just dragged to is held for this page session, so a
    // refused write does not snap the pane back on the very next read.
    expect(readSize("p", BOUNDS)).toEqual({ collapsed: true, width: 400 });
    expect(hasSize("p")).toBe(true);
  });

  it("still renders the component that asked for the size", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => undefined,
    } as unknown as Storage);
    function Pane() {
      const [size] = usePersistedSize("p", BOUNDS);
      return <p>{size.width}</p>;
    }
    render(<Pane />);
    expect(screen.getByText(String(BOUNDS.size))).toBeInTheDocument();
  });
});

describe("a key that varies cannot grow the record without end", () => {
  it("keeps the most recent keys and forgets the entries of the rest", () => {
    const bound = { namespace: "chat.pane:u1", keep: 3 };
    for (const id of ["a", "b", "c", "d"]) {
      writeSize(`chat.pane:u1:${id}`, { collapsed: false, width: 300 }, BOUNDS, { lru: bound });
    }
    expect(lru("chat.pane:u1")).toEqual([
      "chat.pane:u1:d",
      "chat.pane:u1:c",
      "chat.pane:u1:b",
    ]);
    // The evicted key's entry is gone too, not just its place in the index.
    expect(raw("chat.pane:u1:a")).toBeNull();
    expect(raw("chat.pane:u1:d")).not.toBeNull();
  });

  it("a key written again moves to the front instead of being evicted", () => {
    const bound = { namespace: "n", keep: 2 };
    writeSize("x", { collapsed: false, width: 300 }, BOUNDS, { lru: bound });
    writeSize("y", { collapsed: false, width: 300 }, BOUNDS, { lru: bound });
    writeSize("x", { collapsed: false, width: 310 }, BOUNDS, { lru: bound });
    writeSize("z", { collapsed: false, width: 300 }, BOUNDS, { lru: bound });
    // y was the least recently used, so y is the one that went.
    expect(lru("n")).toEqual(["z", "x"]);
    expect(raw("y")).toBeNull();
    expect(raw("x")).not.toBeNull();
  });

  it("defaults to a bound rather than to none", () => {
    for (let i = 0; i < LRU_KEEP + 5; i += 1) {
      writeSize(`k${i}`, { collapsed: false, width: 300 }, BOUNDS, { lru: { namespace: "n" } });
    }
    expect(lru("n")).toHaveLength(LRU_KEEP);
  });
});

describe("the hook", () => {
  it("writes once the gesture stops, not once per frame", () => {
    vi.useFakeTimers();
    function Pane() {
      const [size, set] = usePersistedSize("p", BOUNDS);
      return (
        <button type="button" onClick={() => set({ width: size.width + 10 })}>
          {size.width}
        </button>
      );
    }
    render(<Pane />);
    fireEvent.click(screen.getByRole("button"));
    fireEvent.click(screen.getByRole("button"));
    // Nothing is owed to the store yet — the frames are still coming.
    expect(raw("p")).toBeNull();
    act(() => void vi.advanceTimersByTime(SIZE_COMMIT_MS));
    expect(JSON.parse(raw("p")!).width).toBe(BOUNDS.size + 20);
  });

  it("writes what is owed when the page goes away", () => {
    vi.useFakeTimers();
    function Pane() {
      const [size, set] = usePersistedSize("p", BOUNDS);
      return (
        <button type="button" onClick={() => set({ width: 400 })}>
          {size.width}
        </button>
      );
    }
    const view = render(<Pane />);
    fireEvent.click(screen.getByRole("button"));
    expect(raw("p")).toBeNull();
    view.unmount();
    expect(JSON.parse(raw("p")!).width).toBe(400);
  });

  it("re-reads when the key changes, and files the old width under the old key", () => {
    vi.useFakeTimers();
    writeSize("two", { collapsed: false, width: 500 }, BOUNDS);
    function Pane({ which }: { which: string }) {
      const [size, set] = usePersistedSize(which, BOUNDS);
      return (
        <button type="button" onClick={() => set({ width: 280 })}>
          {size.width}
        </button>
      );
    }
    const view = render(<Pane which="one" />);
    fireEvent.click(screen.getByRole("button"));
    view.rerender(<Pane which="two" />);
    // The width set while "one" was on screen went to "one", and "two" opened
    // at the width it was already remembering.
    expect(JSON.parse(raw("one")!).width).toBe(280);
    expect(screen.getByRole("button")).toHaveTextContent("500");
  });
});
