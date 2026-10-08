import "@testing-library/jest-dom";
import { cleanup, configure } from "@testing-library/react";
import { clearStorageMirror } from "@alkera/ui/storage";
import { afterEach, vi } from "vitest";

import { resetReadGate } from "@/api/readGate";

// The timer React Query itself schedules notifications on, captured before any
// case can stub it — a drain that went through a stubbed `setTimeout` would
// never come back.
const systemSetTimeout = globalThis.setTimeout;

// One waitFor budget everywhere: the 1 s local default made element waits
// flaky under a loaded machine while the same tests always passed at CI's 5 s.
configure({ asyncUtilTimeout: 5000 });

// Shared UI code reads window.matchMedia for color scheme and reduced-motion
// queries; jsdom doesn't provide it. Report reduced motion as matching so modal
// open/close and other animated unmounts stay deterministic in tests.
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

// Some scroll and navigation surfaces reach for ResizeObserver.
if (typeof window !== "undefined" && !window.ResizeObserver) {
  class StubResizeObserver {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  window.ResizeObserver = StubResizeObserver as unknown as typeof ResizeObserver;
}

// Sticky/scroll-reveal surfaces (the run report's verdict header) observe intersection; jsdom has
// no IntersectionObserver, so stub it as an inert observer (nothing intersects in a headless DOM).
if (typeof window !== "undefined" && !window.IntersectionObserver) {
  class StubIntersectionObserver {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
    takeRecords(): [] {
      return [];
    }
  }
  window.IntersectionObserver = StubIntersectionObserver as unknown as typeof IntersectionObserver;
}

// The CodeMirror editor measures text through Range rects on an animation
// frame; jsdom's Range has neither, so they answer an empty layout.
if (typeof Range !== "undefined" && !Range.prototype.getClientRects) {
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => new DOMRect(0, 0, 0, 0);
}

// Native and custom select interactions may scroll the active option into view;
// jsdom doesn't implement Element.scrollIntoView, so no-op it for tests.
if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

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
      // Pages gate first-paint signals on the fonts settling; resolve immediately in jsdom.
      ready: Promise.resolve(),
    },
  });
}

// The auth background (VineBackground) reads SVG path geometry and drives the Web Animations
// API in a layout effect; jsdom implements neither, so any auth surface would throw on render.
// The decorative result is irrelevant to a test, so stub them to harmless no-ops.
if (typeof SVGElement !== "undefined") {
  const svgProto = SVGElement.prototype as unknown as {
    getTotalLength: () => number;
    getPointAtLength: (distance: number) => { x: number; y: number };
  };
  svgProto.getTotalLength = () => 0;
  svgProto.getPointAtLength = () => ({ x: 0, y: 0 });
}

if (typeof Element !== "undefined" && !Element.prototype.animate) {
  Element.prototype.animate = (() => ({
    cancel() {},
    finished: Promise.resolve(),
  })) as unknown as Element["animate"];
}

// This runner's jsdom localStorage lacks a working setItem when launched with
// `--localstorage-file` and no path. Install a minimal in-memory shim only when
// the real one is broken.
if (typeof window !== "undefined" && typeof window.localStorage?.setItem !== "function") {
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => {
        store.set(key, String(value));
      },
      removeItem: (key: string) => {
        store.delete(key);
      },
      clear: () => {
        store.clear();
      },
      key: (index: number) => [...store.keys()][index] ?? null,
      get length() {
        return store.size;
      },
    },
  });
}

// The @alkera/ui barrel carries every connector mark through the SVGR/svgo transform.
// Loading it here keeps that burst in setup, outside any test's timeout window; a
// dynamic import inside a test body paid it on the slowest CI runner and blew the
// 5s budget. Last on purpose: the jsdom shims above must exist before UI code loads.
await import("@alkera/ui");

// jsdom ships a `Blob` without `arrayBuffer()`, and the upload client reads every
// part through it. Polyfilled once, here, through jsdom's own `FileReader` rather
// than `new Response(blob)`: undici's Response wants a body that `stream()`s,
// which jsdom's Blob only grew on the newest engines, so the Response route
// ended every upload as `failed` on the Node the gate runs.
if (typeof Blob.prototype.arrayBuffer !== "function") {
  Object.defineProperty(Blob.prototype, "arrayBuffer", {
    configurable: true,
    value(this: Blob): Promise<ArrayBuffer> {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as ArrayBuffer);
        reader.onerror = () => reject(reader.error ?? new Error("the blob could not be read"));
        reader.readAsArrayBuffer(this);
      });
    },
  });
}

// The same jsdom `Blob` has no `stream()` either, and undici's `Response`
// (the engine's own fetch) refuses a body without one — so a test that hands
// a Blob to `new Response(...)` saw every read fail on that engine. One
// chunk, the whole blob: enough for a fixture, never a streaming claim.
if (typeof Blob.prototype.stream !== "function") {
  Object.defineProperty(Blob.prototype, "stream", {
    configurable: true,
    value(this: Blob): ReadableStream<Uint8Array> {
      return new ReadableStream<Uint8Array>({
        start: async (controller) => {
          controller.enqueue(new Uint8Array(await this.arrayBuffer()));
          controller.close();
        },
      });
    },
  });
}

// A write the browser refused is held in memory by the guarded store so the
// feature keeps working for the page session. A test case is its own page, so
// that memory is dropped between them — otherwise a case that made the store
// throw leaves its value behind for the next one to read back.
afterEach(() => {
  clearStorageMirror();
  // The read pause is module state on purpose — a refusal the server hands one
  // read holds the rest of the page's. A test case is its own page, so a case
  // that drove a 429 must not leave every later case in this file waiting at a
  // gate it never armed.
  resetReadGate();
});

// React Query hands every cache notification to a `setTimeout(0)`, so the answers
// a page received in its last moments are still queued when the case ends.
// Testing Library's cleanup unmounts the tree but does not drain that queue: the
// callbacks land after Vitest has torn this file's jsdom down, and React reads
// `window` while stamping the update — a `ReferenceError: window is not defined`
// with no case left to attribute it to, which fails the whole run.
//
// So unmount here rather than waiting for the library's own hook (a queued
// notification then reaches no listener), and give the queue the one tick it
// needs while the document is still standing. Only for a case that rendered, and
// never under fake timers: a tick that the case's own clock has to advance would
// never arrive, and a queue no real timer will ever run is discarded with the
// environment anyway.
afterEach(async () => {
  const rendered = typeof document !== "undefined" && document.body.childElementCount > 0;
  cleanup();
  if (!rendered || vi.isFakeTimers()) return;
  await new Promise((resolve) => systemSetTimeout(resolve, 0));
});
