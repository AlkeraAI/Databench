// Where a chat lands when it opens, and what is allowed to take it away from
// there.
//
// A long chat opened on a turn thousands of pixels above its newest one: the
// tape kept growing after the first page landed — a live turn still streaming,
// an artifact laying itself out — and every one of those growths moved the
// scroll position, which the panel read as the reader leaving the live edge.
// It then held them there faithfully. So the pin survives content arriving
// late, however late, and only a gesture releases it.
//
// jsdom lays nothing out, so the tape's height and the viewport are stubbed and
// the ResizeObserver is one this file can fire.

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ChatPanel, type TranscriptEntry } from "@alkera/ui";

const VIEWPORT = 400;

/** The tape's height, which the test grows the way late content does. */
let tapeHeight = 2_000;

let observers: { target: Element; fire: () => void }[] = [];
let restore: (() => void)[] = [];

function entry(id: string): TranscriptEntry {
  return { id, item: { kind: "user", text: id, at: "" }, status: "settled" };
}

const ENTRIES = [entry("first"), entry("last")];

beforeEach(() => {
  observers = [];
  tapeHeight = 2_000;
  const proto = HTMLElement.prototype;
  const height = Object.getOwnPropertyDescriptor(proto, "scrollHeight");
  const client = Object.getOwnPropertyDescriptor(proto, "clientHeight");
  Object.defineProperty(proto, "scrollHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains("chat-scroll") ? tapeHeight : 0;
    },
  });
  Object.defineProperty(proto, "clientHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains("chat-scroll") ? VIEWPORT : 0;
    },
  });
  const realObserver = window.ResizeObserver;
  class FiringResizeObserver {
    constructor(private readonly callback: () => void) {}
    observe(target: Element): void {
      observers.push({ target, fire: () => this.callback() });
    }
    unobserve(target: Element): void {
      observers = observers.filter((entry) => entry.target !== target);
    }
    disconnect(): void {
      observers = [];
    }
  }
  window.ResizeObserver = FiringResizeObserver as unknown as typeof ResizeObserver;
  restore = [
    () => (height ? Object.defineProperty(proto, "scrollHeight", height) : Reflect.deleteProperty(proto, "scrollHeight")),
    () => (client ? Object.defineProperty(proto, "clientHeight", client) : Reflect.deleteProperty(proto, "clientHeight")),
    () => {
      window.ResizeObserver = realObserver;
    },
  ];
});

afterEach(() => {
  for (const undo of restore) undo();
  restore = [];
});

function scrollBox(): HTMLElement {
  const node = document.querySelector<HTMLElement>(".chat-scroll");
  if (!node) throw new Error("no scroll box");
  return node;
}

/** The tape grew after the reader arrived — a stream, an artifact, an image
 *  that finished loading. The browser reports it through the observer that is
 *  watching the content box. */
function growTape(by: number): void {
  tapeHeight += by;
  act(() => {
    for (const observer of [...observers]) observer.fire();
  });
}

/** The reader, taking the view up. The gesture is what makes it theirs. */
/** The browser reporting the snap the panel just made. jsdom does not fire a
 *  scroll for an assignment, and the direction of the next move is measured
 *  against the last position the panel was told about. */
function settleAtBottom(): void {
  const node = scrollBox();
  node.scrollTop = node.scrollHeight;
  fireEvent.scroll(node);
}

function readerScrollsUp(to: number): void {
  const node = scrollBox();
  settleAtBottom();
  act(() => {
    fireEvent.wheel(node, { deltaY: -240 });
    node.scrollTop = to;
    fireEvent.scroll(node);
  });
}

describe("where a long chat lands when it opens", () => {
  it("re-lands the bottom as content arrives after the first page", () => {
    // The chat opens on its spinner, which is what the first observer attaches
    // to; the tape replaces it once the page lands.
    const { rerender } = render(<ChatPanel entries={[]} loading />);
    rerender(<ChatPanel entries={ENTRIES} />);
    const node = scrollBox();
    expect(node.scrollTop).toBe(2_000);

    growTape(5_000);

    expect(node.scrollTop).toBe(7_000);
    expect(node.dataset.follow).toBe("pinned");
    expect(screen.queryByRole("button", { name: /Jump to latest|New messages/ })).toBeNull();
  });

  it("stays pinned when the content's own arrival moves the position", () => {
    render(<ChatPanel entries={ENTRIES} />);
    const node = scrollBox();
    settleAtBottom();

    // What the browser's scroll anchoring does while something above the
    // viewport settles: the position moves UP, and no reader touched it.
    act(() => {
      node.scrollTop = 900;
      fireEvent.scroll(node);
    });

    expect(node.dataset.follow).toBe("pinned");
    growTape(3_000);
    expect(node.scrollTop).toBe(5_000);
  });

  it("releases on the reader's own upward scroll, and the way back re-pins", () => {
    render(<ChatPanel entries={ENTRIES} />);
    const node = scrollBox();

    readerScrollsUp(600);

    expect(node.dataset.follow).toBe("free");
    const jump = screen.getByRole("button", { name: "Jump to latest" });

    // Released means released: what arrives below no longer moves the view.
    growTape(3_000);
    expect(node.scrollTop).toBe(600);

    fireEvent.click(jump);
    expect(node.dataset.follow).toBe("pinned");
    expect(node.scrollTop).toBe(5_000);
  });
});
