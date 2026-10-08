import { useCallback, useEffect, useRef, useState, type ReactElement, type UIEvent } from "react";

import { Button } from "../../primitives/controls/Button";
import type { PreviewContent } from "../types";
import { formatBytes } from "../size";

// The end of a text file that has not all arrived.
//
// A large text file reaches a renderer one window at a time: the host hands over
// what has landed, how much of the file that is, and `more()` for the next
// window. Every text-like renderer draws the same line at the end of what it
// shows — how much of the file is here, and a way to bring the rest — and asks
// for the next window on its own when the reader scrolls near the end, so
// reading on is scrolling on.
//
// A window is asked for once. A scroll that keeps firing near the end asks
// again only after the window it asked for has landed, and a window that failed
// is asked for again only by the button, so a broken read is not retried on
// every wheel notch.

/** How close to the end, in screens of the scroller, the next window is asked
 *  for. Two screens is enough for a window to land before the reader runs out. */
const SCREENS_BEFORE_END = 2;

/** A size as the Files list reads it, so the header and this line agree. */
export function windowSize(bytes: number): string {
  return formatBytes(bytes);
}

export interface MoreWindow {
  /** How much of the file is here, when not all of it is. */
  partial: { loaded: number; total: number } | null;
  /** A window is on its way. */
  loading: boolean;
  /** The last window asked for did not arrive. */
  failed: boolean;
  /** Bring the next window — the button's ask. */
  ask(): void;
  /** Attach to the renderer's root as `onScrollCapture`: a scroll inside it that
   *  comes within reach of the end asks for the next window. */
  onScrollCapture(event: UIEvent<HTMLElement>): void;
}

/** The next-window state for a renderer's content. */
export function useMoreWindow(content: PreviewContent): MoreWindow {
  const text = content.kind === "text" ? content : null;
  const loaded = text?.loaded;
  const total = text?.total;
  const more = text?.more;
  const partial =
    loaded !== undefined && total !== undefined && more !== undefined && loaded < total
      ? { loaded, total }
      : null;

  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  /** The `loaded` a window was last asked for from — a scroll asks once per window. */
  const askedAt = useRef<number | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const request = useCallback(() => {
    if (more === undefined || loaded === undefined) return;
    askedAt.current = loaded;
    setLoading(true);
    setFailed(false);
    more().then(
      () => {
        if (!mounted.current) return;
        setLoading(false);
      },
      () => {
        if (!mounted.current) return;
        setLoading(false);
        setFailed(true);
      },
    );
  }, [loaded, more]);

  const ask = useCallback(() => {
    if (partial === null || loading) return;
    request();
  }, [loading, partial, request]);

  const onScrollCapture = useCallback(
    (event: UIEvent<HTMLElement>) => {
      if (partial === null || loading || askedAt.current === partial.loaded) return;
      const scroller = event.target as HTMLElement;
      const left = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
      if (left > SCREENS_BEFORE_END * scroller.clientHeight) return;
      request();
    },
    [loading, partial, request],
  );

  return { partial, loading, failed, ask, onScrollCapture };
}

/** The line at the end of a partly loaded file. Nothing once the file is whole. */
export function MoreWindowBar({ tail }: { tail: MoreWindow }): ReactElement | null {
  const { partial, loading, failed, ask } = tail;
  if (partial === null) return null;
  let line = `Showing ${windowSize(partial.loaded)} of ${windowSize(partial.total)}`;
  if (loading) line = "Loading more…";
  else if (failed) line = "The next part of the file could not be loaded.";
  return (
    <div className="alk-preview-more" data-testid="preview-more">
      <p className="alk-preview-more__line" role="status">
        {line}
      </p>
      {loading ? null : (
        <Button variant="secondary" fill="ghost" size="sm" onClick={ask}>
          Show more
        </Button>
      )}
    </div>
  );
}
