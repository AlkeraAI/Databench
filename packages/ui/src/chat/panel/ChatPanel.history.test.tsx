// The window's edges, as the panel keeps them: the page above is asked for
// near the top and only once at a time; a page landing leaves the entry the
// reader was looking at where it was; a turn arriving below a reader who has
// scrolled up moves nothing and offers the way back; the way back releases
// the pages far above.
//
// jsdom lays nothing out, so geometry is stubbed: every entry is 100px tall
// in tape order, the viewport is 250px.

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatPanel, HISTORY_FAILED_NOTE, type TranscriptEntry } from "./ChatPanel";

const ROW = 100;
const VIEWPORT = 250;

function entry(id: string, text = id): TranscriptEntry {
  return { id, item: { kind: "user", text, at: "" }, status: "settled" };
}

function entries(from: number, to: number): TranscriptEntry[] {
  const list: TranscriptEntry[] = [];
  for (let index = from; index <= to; index += 1) list.push(entry(`e${index}`));
  return list;
}

let restore: (() => void)[] = [];

beforeEach(() => {
  const proto = HTMLElement.prototype;
  const top = Object.getOwnPropertyDescriptor(proto, "offsetTop");
  const height = Object.getOwnPropertyDescriptor(proto, "scrollHeight");
  const client = Object.getOwnPropertyDescriptor(proto, "clientHeight");
  Object.defineProperty(proto, "offsetTop", {
    configurable: true,
    get(this: HTMLElement) {
      const tape = this.closest(".chat-tape");
      if (!tape || !this.hasAttribute("data-entry-id")) return 0;
      return Array.from(tape.querySelectorAll("[data-entry-id]")).indexOf(this) * ROW;
    },
  });
  Object.defineProperty(proto, "scrollHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return this.querySelectorAll("[data-entry-id]").length * ROW;
    },
  });
  Object.defineProperty(proto, "clientHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains("chat-scroll") ? VIEWPORT : 0;
    },
  });
  restore = [
    () => (top ? Object.defineProperty(proto, "offsetTop", top) : Reflect.deleteProperty(proto, "offsetTop")),
    () => (height ? Object.defineProperty(proto, "scrollHeight", height) : Reflect.deleteProperty(proto, "scrollHeight")),
    () => (client ? Object.defineProperty(proto, "clientHeight", client) : Reflect.deleteProperty(proto, "clientHeight")),
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

/** Put the reader somewhere above the live edge: an upward move THEY made is
 *  what unpins. The wheel is the gesture — content arriving, and the browser's
 *  anchoring of it, move the position too, and neither takes the view off the
 *  live edge. */
function scrollUpTo(top: number): void {
  const node = scrollBox();
  node.scrollTop = node.scrollHeight;
  fireEvent.scroll(node);
  fireEvent.wheel(node, { deltaY: -240 });
  node.scrollTop = top;
  fireEvent.scroll(node);
}

describe("asking for the page above", () => {
  it("asks when the reader is near the top, and not again while the page is on its way", () => {
    const loadOlder = vi.fn();
    const { rerender } = render(
      <ChatPanel entries={entries(1, 12)} history={{ hasOlder: true, loading: false, loadOlder }} />,
    );
    scrollUpTo(900);
    expect(loadOlder).not.toHaveBeenCalled();
    scrollUpTo(300);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    rerender(<ChatPanel entries={entries(1, 12)} history={{ hasOlder: true, loading: true, loadOlder }} />);
    fireEvent.scroll(scrollBox());
    fireEvent.scroll(scrollBox());
    expect(loadOlder).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status", { name: "Loading earlier messages" })).toBeTruthy();
  });

  it("asks straight away while the transcript is shorter than the reach, and stops when nothing is older", () => {
    const loadOlder = vi.fn();
    const { rerender } = render(
      <ChatPanel entries={entries(1, 2)} history={{ hasOlder: true, loading: false, loadOlder }} />,
    );
    expect(loadOlder).toHaveBeenCalledTimes(1);
    rerender(<ChatPanel entries={entries(1, 2)} history={{ hasOlder: false, loading: false, loadOlder }} />);
    expect(document.querySelector("[data-history-sentinel]")).toBeNull();
    fireEvent.scroll(scrollBox());
    expect(loadOlder).toHaveBeenCalledTimes(1);
  });

  it("stops asking once a page has failed, and says so at the edge instead", () => {
    // Without this the sentinel re-arms the moment the request settles: near
    // the top of a transcript a refusing server is asked once per round trip,
    // for as long as the tab is open.
    const loadOlder = vi.fn();
    const { rerender } = render(
      <ChatPanel entries={entries(1, 2)} history={{ hasOlder: true, loading: false, loadOlder }} />,
    );
    expect(loadOlder).toHaveBeenCalledTimes(1);
    rerender(
      <ChatPanel entries={entries(1, 2)} history={{ hasOlder: true, loading: false, failed: true, loadOlder }} />,
    );
    fireEvent.scroll(scrollBox());
    fireEvent.scroll(scrollBox());
    expect(loadOlder).toHaveBeenCalledTimes(1);
    expect(document.querySelector("[data-history-sentinel]")).toBeNull();
    expect(screen.getByText(HISTORY_FAILED_NOTE)).toBeTruthy();
  });

  it("offers the reader the retry the tape stopped making", () => {
    const loadOlder = vi.fn();
    render(
      <ChatPanel entries={entries(1, 2)} history={{ hasOlder: true, loading: false, failed: true, loadOlder }} />,
    );
    expect(loadOlder).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(loadOlder).toHaveBeenCalledTimes(1);
  });

  it("hands focus to the sentinel when the retry it was pressed on goes away", () => {
    const loadOlder = vi.fn();
    const failing = { hasOlder: true, loading: false, failed: true, loadOlder };
    const { rerender } = render(<ChatPanel entries={entries(1, 2)} history={failing} />);
    const retry = screen.getByRole("button", { name: "Retry" });
    act(() => {
      retry.focus();
      fireEvent.click(retry);
    });
    rerender(
      <ChatPanel entries={entries(1, 2)} history={{ hasOlder: true, loading: true, loadOlder }} />,
    );
    expect(document.activeElement).toBe(document.querySelector("[data-history-sentinel]"));
  });

  it("leaves focus alone when the ladder's own wait swaps the row out", () => {
    // The reader did not ask for this one, so nothing of theirs is taken.
    const loadOlder = vi.fn();
    const { rerender } = render(
      <ChatPanel entries={entries(1, 2)} history={{ hasOlder: true, loading: false, failed: true, loadOlder }} />,
    );
    const elsewhere = document.createElement("button");
    document.body.appendChild(elsewhere);
    act(() => elsewhere.focus());
    rerender(
      <ChatPanel entries={entries(1, 2)} history={{ hasOlder: true, loading: false, loadOlder }} />,
    );
    expect(document.activeElement).toBe(elsewhere);
    elsewhere.remove();
  });

  it("says nothing about a failure on a chat with nothing older", () => {
    const loadOlder = vi.fn();
    render(
      <ChatPanel entries={entries(1, 2)} history={{ hasOlder: false, loading: false, failed: true, loadOlder }} />,
    );
    expect(screen.queryByText(HISTORY_FAILED_NOTE)).toBeNull();
    expect(loadOlder).not.toHaveBeenCalled();
  });

  it("renders no sentinel for a host without paging", () => {
    render(<ChatPanel entries={entries(1, 3)} />);
    expect(document.querySelector("[data-history-sentinel]")).toBeNull();
  });
});

describe("a page landing above", () => {
  it("keeps the entry under the reader where it was", () => {
    const loadOlder = vi.fn();
    const { rerender } = render(
      <ChatPanel entries={entries(5, 16)} history={{ hasOlder: true, loading: false, loadOlder }} />,
    );
    // Entry e9 sits at 400px; the reader is looking at it from 350px.
    scrollUpTo(350);
    const before = scrollBox().scrollTop;
    // Four rows land above: e9 now sits at 800px.
    rerender(<ChatPanel entries={entries(1, 16)} history={{ hasOlder: false, loading: false, loadOlder }} />);
    expect(scrollBox().scrollTop).toBe(before + 4 * ROW);
    expect(scrollBox().querySelector('[data-entry-id="e9"]')).toBeTruthy();
  });

  it("does not move a reader who is at the live edge", () => {
    const { rerender } = render(
      <ChatPanel entries={entries(5, 8)} history={{ hasOlder: true, loading: false, loadOlder: () => {} }} />,
    );
    const node = scrollBox();
    expect(node.getAttribute("data-follow")).toBe("pinned");
    rerender(<ChatPanel entries={entries(1, 8)} history={{ hasOlder: false, loading: false, loadOlder: () => {} }} />);
    // Pinned: the follow scroll keeps the bottom, so the position is the
    // bottom of the taller tape, not the old position plus the page.
    expect(node.scrollTop).toBe(node.scrollHeight);
  });
});

describe("a turn arriving below a reader who scrolled up", () => {
  it("moves nothing and offers the way back, which re-pins and releases", () => {
    const release = vi.fn();
    const history = { hasOlder: false, loading: false, loadOlder: () => {}, release };
    const { rerender } = render(<ChatPanel entries={entries(1, 12)} history={history} />);
    expect(release).toHaveBeenCalledTimes(1); // mounted at the live edge
    scrollUpTo(700);
    expect(screen.getByRole("button", { name: "Jump to latest" })).toBeTruthy();
    rerender(<ChatPanel entries={entries(1, 14)} history={history} />);
    expect(scrollBox().scrollTop).toBe(700);
    const jump = screen.getByRole("button", { name: "New messages" });
    expect(release).toHaveBeenCalledTimes(1);
    act(() => {
      fireEvent.click(jump);
    });
    expect(scrollBox().scrollTop).toBe(scrollBox().scrollHeight);
    expect(scrollBox().getAttribute("data-follow")).toBe("pinned");
    expect(screen.queryByRole("button", { name: /Jump to latest|New messages/ })).toBeNull();
    expect(release).toHaveBeenCalledTimes(2);
  });

  it("weighs the budget every time the tape grows under a pinned reader", () => {
    // The budget's whole reason is the tab left open for days. A reader who
    // opens a chat and never scrolls never changes `pinned`, so evaluating it
    // only on that transition evaluated it exactly once for the life of the
    // tab — while the transcript it was meant to bound kept growing.
    const release = vi.fn();
    const history = { hasOlder: false, loading: false, loadOlder: () => {}, release };
    const { rerender } = render(<ChatPanel entries={entries(1, 4)} history={history} />);
    expect(release).toHaveBeenCalledTimes(1);
    for (let n = 5; n <= 9; n += 1) {
      rerender(<ChatPanel entries={entries(1, n)} history={history} />);
    }
    expect(release).toHaveBeenCalledTimes(6);
    // A commit that did not grow the tape is not a new reason to weigh it.
    rerender(<ChatPanel entries={entries(1, 9)} working history={history} />);
    expect(release).toHaveBeenCalledTimes(6);
  });
});
