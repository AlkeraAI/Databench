// Package-internal interaction hooks: list keyboarding for the organs'
// overlays (dismissal itself is @alkera/ui's useDismiss, whose overlay stack
// keeps one Escape to one close), the transcript's follow scroll, and the
// disclosure memory every fold in the transcript reads. Not exported from the
// package barrel.

import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from "react";

/** What the reader has opened and closed, for as long as this client runs.
 *
 *  Leaving a chat and coming back must not undo the reader's own hands: a well
 *  they opened to read stays open, a run they folded stays folded. React state
 *  cannot carry that, because the transcript unmounts with the chat. Nor can
 *  disk, and deliberately -- these are reading positions, not preferences, and
 *  a fold from last week is a stale answer to a chat the reader has since
 *  forgotten. Module scope is exactly the lifetime that means: the webview's
 *  own. Reload the extension and every surface is back at its default. */
const disclosed = new Map<string, boolean>();

/** What the reader last did to this disclosure, or undefined if they never
 *  touched it -- the caller's own default then stands. */
export function recallDisclosure(key: string): boolean | undefined {
  return disclosed.get(key);
}

export function rememberDisclosure(key: string, open: boolean): void {
  disclosed.set(key, open);
}

/** A single disclosure that remembers itself. The key must name the THING, not
 *  the position -- a tool call's id, not its index -- or a run that gains a
 *  step hands the reader someone else's fold. */
export function useDisclosure(key: string, fallback: boolean): [boolean, (open: boolean) => void] {
  const [open, setOpen] = useState(() => recallDisclosure(key) ?? fallback);
  useEffect(() => {
    rememberDisclosure(key, open);
  }, [key, open]);
  return [open, setOpen];
}

/** The open list's keyboard: arrow keys rove focus across its `role` rows,
 *  wrapping at the ends; Escape hands control back through `onEscape`, which
 *  closes the overlay and refocuses its trigger. */
export function useRovingFocus(
  listRef: RefObject<HTMLElement | null>,
  role: string,
  onEscape: () => void,
): (event: ReactKeyboardEvent<HTMLElement>) => void {
  return (event) => {
    if (event.key === "Escape") {
      onEscape();
      return;
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const rows = Array.from(listRef.current?.querySelectorAll<HTMLElement>(`[role="${role}"]`) ?? []);
    if (rows.length === 0) return;
    const at = rows.indexOf(document.activeElement as HTMLElement);
    const step = event.key === "ArrowDown" ? 1 : -1;
    rows[(at + step + rows.length) % rows.length].focus();
  };
}

/** The at-bottom decision, kept pure so it is testable without a DOM: within
 *  `threshold` px of the bottom counts as reading the live edge. */
export function isAtBottom(scrollTop: number, clientHeight: number, scrollHeight: number, threshold = 32): boolean {
  return scrollHeight - scrollTop - clientHeight < threshold;
}

/** How long after a wheel, a drag or a key a scroll still counts as the
 *  reader's own. A gesture's first scroll event lands within a frame or two;
 *  only that first upward one has to qualify, because it is what releases the
 *  pin. */
const READER_SCROLL_MS = 500;

/** While pinned, snap the container to the bottom BEFORE paint on every
 *  `watch` change; once the reader scrolls up, stay out of the way until they
 *  return to the bottom band.
 *
 *  Only an upward movement THE READER MADE may unpin. A programmatic snap
 *  fires a scroll event too, and so does the browser's own scroll anchoring,
 *  which moves the position whenever something above the viewport changes
 *  size: a stream still arriving, an artifact laying itself out, an image or an
 *  iframe settling. Judging "left the bottom" on those is what left a long chat
 *  thousands of pixels above its newest turn seconds after it opened — the view
 *  was unpinned by the content's own arrival and then held there faithfully.
 *  So the pin survives until a gesture moves it, and the follow re-lands the
 *  bottom however late the content comes. */
export function useFollowScroll(watch: unknown): {
  ref: RefObject<HTMLDivElement | null>;
  pinned: boolean;
  /** Return to the live edge on purpose: snap to the bottom and follow again. */
  pin: () => void;
} {
  const ref = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);
  const lastTop = useRef(0);
  // Read by the observers, which are wired once and must not be torn down and
  // rebuilt on every pin — a ResizeObserver that re-attaches misses the growth
  // happening while it is being replaced.
  const pinnedRef = useRef(true);
  pinnedRef.current = pinned;
  const gestureAt = useRef(0);
  const pin = () => {
    const node = ref.current;
    if (node) {
      node.scrollTop = node.scrollHeight;
      lastTop.current = node.scrollTop;
    }
    setPinned(true);
  };

  useLayoutEffect(() => {
    if (!pinned) return;
    const node = ref.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [watch, pinned]);

  // Content can grow WITHOUT a new entry: a step that settles opens its own
  // well from inside the card, an artifact lays itself out, an image loads.
  // The entry-keyed snap above never sees that, so the pin also follows the
  // content box's size.
  //
  // WHICH box that is changes: a chat that is still opening draws a spinner,
  // and the tape replaces it once the first page lands. An observer attached to
  // the spinner watches a detached node for the rest of the session, which is
  // how a long chat opened on a turn thousands of pixels above its newest one —
  // nothing was following the tape that grew underneath it.
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (pinnedRef.current) node.scrollTop = node.scrollHeight;
    });
    let watched: Element | null = null;
    const attach = (): void => {
      const content = node.firstElementChild;
      if (content === watched) return;
      if (watched) observer.unobserve(watched);
      watched = content;
      if (content) observer.observe(content);
    };
    attach();
    const swaps = typeof MutationObserver === "undefined" ? null : new MutationObserver(attach);
    swaps?.observe(node, { childList: true });
    return () => {
      observer.disconnect();
      swaps?.disconnect();
    };
  }, []);

  // A gesture that can move the scroller. Only a scroll that follows one counts
  // as the reader leaving the live edge; everything else moving the position is
  // the content, or the browser's anchoring of it.
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const mark = (): void => {
      gestureAt.current = Date.now();
    };
    node.addEventListener("wheel", mark, { passive: true });
    node.addEventListener("touchmove", mark, { passive: true });
    node.addEventListener("pointerdown", mark);
    node.addEventListener("keydown", mark);
    return () => {
      node.removeEventListener("wheel", mark);
      node.removeEventListener("touchmove", mark);
      node.removeEventListener("pointerdown", mark);
      node.removeEventListener("keydown", mark);
    };
  }, []);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const onScroll = () => {
      const movedUp = node.scrollTop < lastTop.current - 1;
      lastTop.current = node.scrollTop;
      const atBottom = isAtBottom(node.scrollTop, node.clientHeight, node.scrollHeight);
      const byReader = Date.now() - gestureAt.current < READER_SCROLL_MS;
      if (movedUp && byReader) setPinned(atBottom);
      else if (atBottom) setPinned(true);
    };
    node.addEventListener("scroll", onScroll, { passive: true });
    return () => node.removeEventListener("scroll", onScroll);
  }, []);

  return { ref, pinned, pin };
}

/** A bare Enter, no modifier riding along to re-mean it. The interrupt cards
 *  commit on exactly this key. */
export function isPlainEnter(event: ReactKeyboardEvent<HTMLElement>): boolean {
  return event.key === "Enter" && !event.shiftKey && !event.metaKey && !event.ctrlKey && !event.altKey;
}
