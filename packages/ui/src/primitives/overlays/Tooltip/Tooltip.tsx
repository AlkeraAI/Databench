import { useCallback, useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { useFloating, usePresence, pushEsc, type FloatingSide } from "../../../hooks";
import type { VirtualElement } from "@floating-ui/react";

/**
 * Tooltip — the hover/focus annotation, the bench's marginal note on a control.
 *
 * Anchored on the shared `useFloating` positioner (so it flips at a viewport edge) and portaled so
 * it escapes a scroll-clipping ancestor and inherits the theme tokens. Its interaction model is its
 * own: it opens on hover OR keyboard focus after a short delay (so a pointer sweeping across a row
 * of controls doesn't flash a column of tips), and closes instantly on leave or blur. It is
 * `pointer-events: none`, so it never steals the hover that summoned it.
 *
 * Accessibility: the label is wired to the trigger via `aria-describedby`, and the floating element
 * carries `role="tooltip"`, so a screen reader announces it as the control's description rather than
 * a second interactive element. The trigger is a render-prop given the listener + aria props to
 * spread onto a single focusable element. Escape dismisses an open tip via the shared overlay stack
 * (so a tip never steals Escape from a modal above it). Honors `prefers-reduced-motion` — the tip
 * fades, never scales (the reduced-motion rule lives in tooltip.css).
 */

export interface TooltipTriggerProps {
  ref: (el: HTMLElement | null) => void;
  "aria-describedby"?: string;
  /** Marks the trigger as already annotated, so a control that would otherwise
   *  fall back to a native `title` (an icon-only Button) leaves the tip to this
   *  one and the reader gets one annotation rather than two. */
  "data-tip": "";
  onPointerEnter: (e?: React.PointerEvent) => void;
  onPointerMove?: (e: React.PointerEvent) => void;
  onPointerLeave: () => void;
  onFocus: () => void;
  onBlur: () => void;
}

export interface TooltipProps {
  /** The tip text (or small node). Kept short — a tooltip is an annotation, not a panel. */
  label: ReactNode;
  /** Render the trigger; spread the supplied props onto a single focusable element. */
  children: (props: TooltipTriggerProps) => ReactNode;
  /** Side the tip opens toward (default `top`). It still flips at a viewport edge. */
  side?: FloatingSide;
  /** What the tip pins to. `trigger` (default) anchors to the trigger element; `pointer` opens at
   *  the spot where the cursor rested out the delay and stays there until the pointer leaves (the
   *  native-title behavior). Keyboard focus always anchors to the element (there is no
   *  cursor to pin to). */
  anchor?: "trigger" | "pointer";
  /** Open delay in ms (default 300). Close is always instant. */
  openDelay?: number;
  /** Exit-animation duration; must match the panel's CSS transition. */
  exitMs?: number;
  /** Allow longer labels, such as file paths, to wrap with hyphenation. */
  wrap?: boolean;
  /** Optional width cap for longer wrapped labels. */
  maxWidth?: number | string;
}

/** A zero-size floating-ui reference at the cursor's rest point. */
function cursorReference(at: { x: number; y: number }): VirtualElement {
  return {
    getBoundingClientRect: () => ({
      x: at.x,
      y: at.y,
      top: at.y,
      bottom: at.y,
      left: at.x,
      right: at.x,
      width: 0,
      height: 0,
    }),
  };
}

/** The pointer anchor: tracks the cursor's rest point while the tip is closed, and at a
 *  hover-open freezes it as a virtual reference for the shared positioner (whose flip/shift
 *  middleware owns collision from there). A focus-open, the default anchor, or a close leaves
 *  the trigger element pinned — closing never touches the reference, so the exit fade can't
 *  jump. */
function useCursorAnchor(
  track: boolean,
  openedBy: "hover" | "focus" | null,
  setPositionReference: (ref: VirtualElement | null) => void,
): { seed: (e?: React.PointerEvent) => void; move?: (e: React.PointerEvent) => void } {
  const cursorRef = useRef<{ x: number; y: number } | null>(null);
  useEffect(() => {
    if (openedBy == null) return;
    const at = cursorRef.current;
    if (track && openedBy === "hover" && at != null) {
      setPositionReference(cursorReference(at));
    } else {
      setPositionReference(null);
    }
  }, [openedBy, track, setPositionReference]);
  const closed = openedBy == null;
  return {
    seed: (e) => {
      if (track && e != null) cursorRef.current = { x: e.clientX, y: e.clientY };
    },
    // The rest point tracks the cursor only while the tip is closed; once open it is pinned
    // until pointer-leave, like a native title tip. It never follows the moving cursor.
    move: track
      ? (e) => {
          if (closed) cursorRef.current = { x: e.clientX, y: e.clientY };
        }
      : undefined,
  };
}

export function Tooltip({
  label,
  children,
  side = "top",
  anchor = "trigger",
  openDelay = 300,
  exitMs = 120,
  wrap = false,
  maxWidth,
}: TooltipProps) {
  // What currently holds the tip open. Focus always wins the element anchor the docstring
  // promises; hover claims only a closed tip, and only through the delay.
  const [openedBy, setOpenedBy] = useState<"hover" | "focus" | null>(null);
  const open = openedBy != null;
  const { mounted, state } = usePresence(open, exitMs);
  const wrapRef = useRef<HTMLElement | null>(null);
  const timer = useRef<number | null>(null);
  const tipId = useId();
  const track = anchor === "pointer";
  // A tooltip is short and never clamps — its label box should keep its natural size, not collapse
  // to a sliver inside a transformed container. Small gap so it sits close to its mark; a cursor
  // anchor gets a wider one so the pointer never occludes the tip.
  const float = useFloating({ open, side, align: "center", gap: track ? 12 : 6, clampHeight: false });
  const cursor = useCursorAnchor(track, openedBy, float.setPositionReference);

  const clearTimer = useCallback(() => {
    if (timer.current) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  // Open after the delay; close instantly. A pointer leaving before the delay elapses cancels the
  // pending open, so a quick sweep across a row of controls never flashes their tips. The firing
  // timer never demotes a focus-held tip to a hover one.
  const scheduleOpen = useCallback(() => {
    clearTimer();
    timer.current = window.setTimeout(() => setOpenedBy((prev) => prev ?? "hover"), openDelay);
  }, [clearTimer, openDelay]);
  const closeNow = useCallback(() => {
    clearTimer();
    setOpenedBy(null);
  }, [clearTimer]);

  useEffect(() => clearTimer, [clearTimer]);

  // Escape dismisses an open tip immediately, the keyboard counterpart to pointer-leave. Routed
  // through the shared overlay stack so only the topmost surface answers a single press.
  useEffect(() => {
    if (!open) return;
    return pushEsc(closeNow);
  }, [open, closeNow]);

  const portalTarget =
    (mounted ? (wrapRef.current?.closest("[data-theme], [data-theme-group]") as HTMLElement | null) : null) ?? document.body;

  const setRef = useCallback(
    (el: HTMLElement | null) => {
      wrapRef.current = el;
      float.setReference(el);
    },
    [float],
  );
  const style: CSSProperties = {
    ...float.floatingStyles,
    ...(maxWidth !== undefined ? { maxWidth } : {}),
  };

  return (
    <>
      {children({
        ref: setRef,
        "aria-describedby": open || mounted ? tipId : undefined,
        "data-tip": "",
        onPointerEnter: (e) => {
          cursor.seed(e);
          scheduleOpen();
        },
        onPointerMove: cursor.move,
        onPointerLeave: closeNow,
        // Keyboard focus shows the tip at once (no delay — the user is already on the control);
        // a focus moving INTO the trigger from outside opens it, a blur closes it.
        onFocus: () => setOpenedBy("focus"),
        onBlur: closeNow,
      })}
      {mounted
        ? createPortal(
            <div
              ref={float.setFloating}
              id={tipId}
              role="tooltip"
              className="alk-tooltip"
              style={style}
              data-side={float.side}
              data-state={state}
              data-wrap={wrap ? "true" : undefined}
            >
              {label}
            </div>,
            portalTarget,
          )
        : null}
    </>
  );
}
