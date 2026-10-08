// A stand-in host for the renderer tests: it holds a file's windows and lands
// the next one each time `more()` is called, the way the real host does. It does
// NOT merge asks that overlap, so a renderer that asked twice for one window
// would be seen drawing two — which is what lets a test read "asked once" off
// the screen instead of counting calls.

import { useCallback, useState, type ReactElement } from "react";

import type { PreviewContent } from "../types";

export const MIB = 1024 * 1024;

export interface Gate {
  /** The promise each `more()` waits on before its window lands. */
  wait(): Promise<void>;
  /** Let every waiting `more()` land (or fail). */
  open(): void;
  fail(): void;
}

/** A gate the test opens by hand, so a window can be held in flight. */
export function manualGate(): Gate {
  let pending: { resolve: () => void; reject: (error: Error) => void }[] = [];
  return {
    wait: () =>
      new Promise<void>((resolve, reject) => {
        pending.push({ resolve, reject });
      }),
    open: () => {
      const all = pending;
      pending = [];
      for (const each of all) each.resolve();
    },
    fail: () => {
      const all = pending;
      pending = [];
      for (const each of all) each.reject(new Error("The next part of the file could not be loaded"));
    },
  };
}

/** A gate that lets every window land at once. */
export const openGate: Gate = { wait: async () => {}, open: () => {}, fail: () => {} };

export function WindowedHost({
  windows,
  windowBytes = MIB,
  total,
  gate = openGate,
  draw,
}: {
  windows: readonly string[];
  windowBytes?: number;
  total: number;
  gate?: Gate;
  draw(content: PreviewContent): ReactElement;
}): ReactElement {
  const [landed, setLanded] = useState(1);
  const more = useCallback(async () => {
    await gate.wait();
    setLanded((count) => Math.min(count + 1, windows.length));
  }, [gate, windows.length]);
  const loaded = landed >= windows.length ? total : Math.min(landed * windowBytes, total);
  return draw({ kind: "text", text: windows.slice(0, landed).join(""), loaded, total, more });
}

/** Give a scroller the geometry jsdom does not lay out, and park it at `top`. */
export function placeScroller(
  element: Element,
  geometry: { scrollHeight: number; clientHeight: number; top: number },
): void {
  Object.defineProperty(element, "scrollHeight", { configurable: true, value: geometry.scrollHeight });
  Object.defineProperty(element, "clientHeight", { configurable: true, value: geometry.clientHeight });
  (element as HTMLElement).scrollTop = geometry.top;
}

/** Windows of numbered lines, `lines` each, continuing the count across windows. */
export function numberedWindows(count: number, lines: number, prefix = "line"): string[] {
  return Array.from({ length: count }, (_, window) =>
    Array.from({ length: lines }, (_, line) => `${prefix} ${window * lines + line + 1}\n`).join(""),
  );
}
