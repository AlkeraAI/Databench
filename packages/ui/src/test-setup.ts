import "@testing-library/jest-dom";

import { afterEach } from "vitest";

import { clearStorageMirror } from "./storage";

// jsdom omits several browser APIs the base components reach for. Stub the ones
// the primitives touch so a test renders instead of throwing.

// Color-scheme + reduced-motion queries.
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: query.includes("prefers-reduced-motion"),
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}

// Scroll/measurement surfaces (Select, SidePanel, scroll shadows).
if (typeof window !== "undefined" && !window.ResizeObserver) {
  class StubResizeObserver {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  window.ResizeObserver = StubResizeObserver as unknown as typeof ResizeObserver;
}

// Custom listboxes (Select, Dropdown, Tree) scroll the active option into view.
if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// Floating primitives read the visual viewport when positioning.
if (typeof window !== "undefined" && !window.visualViewport) {
  Object.defineProperty(window, "visualViewport", {
    value: {
      addEventListener: () => {},
      removeEventListener: () => {},
      width: 1024,
      height: 768,
      scale: 1,
      offsetLeft: 0,
      offsetTop: 0,
      pageLeft: 0,
      pageTop: 0,
    },
  });
}

if (typeof document !== "undefined" && !document.fonts) {
  Object.defineProperty(document, "fonts", {
    value: {
      addEventListener: () => {},
      removeEventListener: () => {},
    },
  });
}

// Presence-animated overlays (Toast, Modal) drive the Web Animations API on exit.
if (typeof Element !== "undefined" && !Element.prototype.animate) {
  Element.prototype.animate = (() => ({
    cancel() {},
    finished: Promise.resolve(),
  })) as unknown as Element["animate"];
}

// This runner's jsdom storage lacks a working setItem when launched with
// `--localstorage-file` and no path. Install a minimal in-memory shim only when
// the real one is broken, so a test can tell "this browser refuses storage"
// (which the guarded store has to survive) from "this runner has none".
for (const name of ["localStorage", "sessionStorage"] as const) {
  if (typeof window === "undefined") break;
  if (typeof window[name]?.setItem === "function") continue;
  const entries = new Map<string, string>();
  Object.defineProperty(window, name, {
    configurable: true,
    value: {
      getItem: (key: string) => entries.get(key) ?? null,
      setItem: (key: string, value: string) => {
        entries.set(key, String(value));
      },
      removeItem: (key: string) => {
        entries.delete(key);
      },
      clear: () => {
        entries.clear();
      },
      key: (index: number) => [...entries.keys()][index] ?? null,
      get length() {
        return entries.size;
      },
    },
  });
}

// A write the browser refused is held in memory by the guarded store so the
// feature keeps working for the page session. A test case is its own page, so
// that memory is dropped between them — otherwise a case that made the store
// throw leaves its value behind for the next one to read back.
afterEach(() => {
  clearStorageMirror();
});
