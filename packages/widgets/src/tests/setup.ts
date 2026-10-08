// Browser APIs jsdom omits that Lumino and the controls touch at import time.
const g = globalThis as unknown as Record<string, unknown>;
if (typeof g.DragEvent === "undefined") {
  g.DragEvent = class DragEvent extends MouseEvent {};
}
if (typeof g.ResizeObserver === "undefined") {
  g.ResizeObserver = class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  };
}
