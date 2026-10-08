// The theme a reader chose, and what happens when the browser will not keep it.
//
// This hook is mounted in the product's shell, so anything it throws takes the
// whole page with it. `localStorage` throws outright in a private window, in an
// embedded context, and wherever site data is blocked — a remembered theme is
// worth nothing beside the page it is painted on.

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useColorScheme } from "./useColorScheme";

const KEY = "alkera.scheme";

function Theme() {
  const { scheme, resolved, setScheme } = useColorScheme({ storageKey: KEY, defaultScheme: "system" });
  return (
    <button type="button" onClick={() => setScheme("light")}>
      {scheme}/{resolved}
    </button>
  );
}

function stubSystem(dark: boolean): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    media: query,
    matches: dark,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    onchange: null,
    dispatchEvent: () => false,
  }));
}

/** A working store, since this runner's jsdom ships without one. */
function memoryStore(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (k: string) => map.get(k) ?? null,
    key: (i: number) => Array.from(map.keys())[i] ?? null,
    removeItem: (k: string) => void map.delete(k),
    setItem: (k: string, v: string) => void map.set(k, String(v)),
  };
}

beforeEach(() => {
  vi.unstubAllGlobals();
  vi.stubGlobal("localStorage", memoryStore());
  stubSystem(true);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  document.documentElement.removeAttribute("data-alkera-color-scheme");
});

describe("the theme a reader chose", () => {
  it("comes back on the next page, and paints it", () => {
    const first = render(<Theme />);
    act(() => screen.getByRole("button").click());
    expect(localStorage.getItem(KEY)).toBe("light");
    expect(document.documentElement.getAttribute("data-alkera-color-scheme")).toBe("light");
    first.unmount();

    render(<Theme />);
    expect(screen.getByRole("button")).toHaveTextContent("light/light");
  });

  it("falls back to the default when the stored value is not a scheme", () => {
    localStorage.setItem(KEY, "aubergine");
    render(<Theme />);
    expect(screen.getByRole("button")).toHaveTextContent("system/dark");
  });
});

describe("a store that will not answer", () => {
  const angry = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("denied");
    },
    removeItem: () => undefined,
  } as unknown as Storage;

  it("still renders, and still paints the reader's choice for this page", () => {
    stubSystem(true);
    vi.stubGlobal("localStorage", angry);
    render(<Theme />);
    // The page is what matters: without the guard this render throws and the
    // whole shell around it goes with it.
    expect(screen.getByRole("button")).toHaveTextContent("system/dark");
    act(() => screen.getByRole("button").click());
    expect(screen.getByRole("button")).toHaveTextContent("light/light");
    expect(document.documentElement.getAttribute("data-alkera-color-scheme")).toBe("light");
  });
});
