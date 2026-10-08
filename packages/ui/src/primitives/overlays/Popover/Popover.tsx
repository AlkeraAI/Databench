import { useCallback, useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { useDismiss, useFloating, usePresence, type FloatingAlign, type FloatingSide } from "../../../hooks";
import { FloatingOwnerContext, useOwnedFloats } from "../../../hooks/floatingOwner";

/**
 * Popover — an anchored container for ARBITRARY (non-menu) floating content.
 *
 * The sibling to a menu dropdown: same anchoring, portaling, mount-through-exit, and outside-press /
 * Escape dismissal, but no menu semantics. Where a menu wraps a list of rows in `role="menu"`, this
 * wraps whatever the caller renders in a neutral `role="dialog"` container — a form fragment, a
 * provenance card, a date picker, a confirmation. The trigger is a render-prop given the props it
 * must spread (`ref`, `aria-expanded`, `aria-controls`, `data-open`, `onClick`), so a caller can't
 * mis-wire the open state; the body is the caller's own markup.
 *
 * Interaction:
 *  - `openOn="click"` (default) toggles on a press — for an interactive body the user works inside.
 *  - `openOn="hover"` summons it on pointer-hover or keyboard focus, for a NON-INTERACTIVE read-out
 *    only (a legend, a definition): focusing the trigger reveals it for a sighted keyboard user, but
 *    focus can't move into the portaled body, so never put interactive content in a hover popover.
 *    It still toggles on click for touch, and a small open/close delay plus a trigger↔panel bridge
 *    keep it from flickering when the pointer crosses the gap.
 *
 * Mechanics:
 *  - anchored + flips at a viewport edge (`useFloating`);
 *  - portaled to `document.body` so it escapes a scroll-clipping ancestor while still inheriting the
 *    `:root` design tokens;
 *  - mounted through its exit so it animates both ways (`usePresence` + `data-state`);
 *  - click mode closes on Escape / outside press (`useDismiss`); hover mode closes on
 *    pointer-leave, and on Escape like every transient overlay;
 *  - a close while focus is inside the panel hands focus back to the trigger, so a keyboard user
 *    who pressed Escape (or a row that closes the panel) is not dropped onto `<body>` when the
 *    portaled panel unmounts.
 */

export interface PopoverTriggerProps {
  ref: (el: HTMLElement | null) => void;
  "aria-haspopup": "dialog";
  "aria-expanded": boolean;
  "aria-controls": string;
  "data-open"?: true;
  onClick: () => void;
  /** Present only in `openOn="hover"` — spread them all; they're `undefined` for `openOn="click"`. */
  onPointerEnter?: () => void;
  onPointerLeave?: () => void;
  onFocus?: () => void;
  onBlur?: () => void;
}

export interface PopoverRenderProps {
  /** Close the popover from inside its body (e.g. after a Save). */
  close: () => void;
}

export interface PopoverProps {
  /** Render the trigger; spread the supplied props onto a single focusable control. */
  trigger: (props: PopoverTriggerProps) => ReactNode;
  /** The panel body — a node, or a render-prop given `{ close }` to dismiss from within. */
  children: ReactNode | ((props: PopoverRenderProps) => ReactNode);
  /** Accessible name for the panel (announced when focus enters it). */
  label: string;
  /** How the popover is summoned. `click` (default) toggles on press; `hover` opens on hover/focus. */
  openOn?: "click" | "hover";
  /** Force the side the panel opens toward; omit to open below with an automatic flip. */
  side?: FloatingSide;
  /** Force cross-axis alignment; omit to anchor by the trigger's half of the viewport. */
  align?: FloatingAlign;
  /** Floor the panel's width to the trigger's, so it lines up under a wide control. */
  matchWidth?: boolean;
  /** Cap the panel's height to the room available; a longer body then scrolls. Off for a short
   *  body inside a transformed container, where the available-space read under-clamps. */
  clampHeight?: boolean;
  /** Extra class on the panel, for page-specific sizing. */
  panelClassName?: string;
  /** Override the panel's inner padding (the `--alk-popover-pad` token) — e.g. 0 for a full-bleed body. */
  padding?: number | string;
  /** Exit-animation duration; must match the panel's CSS transition. */
  exitMs?: number;
}

// Hover timing: a short open dwell so a pointer sweeping past the trigger doesn't flash it, and a
// close grace so crossing the gap from trigger to panel doesn't dismiss it mid-travel.
const HOVER_OPEN_MS = 90;
const HOVER_CLOSE_MS = 130;

export function Popover({
  trigger,
  children,
  label,
  openOn = "click",
  side,
  align,
  matchWidth = false,
  clampHeight = true,
  panelClassName,
  padding,
  exitMs = 120,
}: PopoverProps) {
  const [open, setOpen] = useState(false);
  const { mounted, state } = usePresence(open, exitMs);
  const wrapRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const float = useFloating({ open, side, align, matchWidth, clampHeight });

  const close = useCallback(() => setOpen(false), []);
  // A submenu opened from the body is portaled elsewhere but still counts as inside this panel.
  const { register, owns } = useOwnedFloats(panelRef);

  const hover = openOn === "hover";
  const openTimer = useRef<number | undefined>(undefined);
  const closeTimer = useRef<number | undefined>(undefined);
  const clearTimers = useCallback(() => {
    window.clearTimeout(openTimer.current);
    window.clearTimeout(closeTimer.current);
  }, []);
  const scheduleOpen = useCallback(() => {
    clearTimers();
    openTimer.current = window.setTimeout(() => setOpen(true), HOVER_OPEN_MS);
  }, [clearTimers]);
  const scheduleClose = useCallback(() => {
    clearTimers();
    closeTimer.current = window.setTimeout(() => setOpen(false), HOVER_CLOSE_MS);
  }, [clearTimers]);
  // Don't let a pending hover open/close fire after unmount.
  useEffect(() => clearTimers, [clearTimers]);

  // Click mode dismisses on outside-press / Escape (the panel lives outside the wrapper in the DOM,
  // so a press inside the portaled panel is spared). Hover mode owns its pointer close through
  // pointer-leave + blur + the panel bridge, so the outside-press watcher stays OFF there —
  // otherwise the two close channels fight (an outside press closes it, then a re-hover instantly
  // re-opens) — but Escape still dismisses it like every transient overlay, through the shared
  // stack so exactly one surface closes per press.
  // Stable, so a re-render does not re-register the Escape layer above one opened from inside
  // the panel (a submenu), which would then lose its Escape to this popover.
  const dismiss = useCallback(
    (target?: Node) => {
      if (owns(target)) return;
      close();
    },
    [owns, close],
  );
  useDismiss(wrapRef, open, dismiss, { outsidePress: !hover });

  const triggerRef = useRef<HTMLElement | null>(null);
  const setTrigger = useCallback(
    (el: HTMLElement | null) => {
      triggerRef.current = el;
      float.setReference(el);
    },
    [float],
  );

  // Checked when `open` flips false, before the exit animation unmounts the panel: focus is still
  // inside it then, and only then is the trigger the right place to put it back. A close caused by
  // pressing somewhere else leaves focus where that press put it.
  useEffect(() => {
    if (open) return;
    const active = document.activeElement;
    if (active instanceof Node && owns(active)) {
      triggerRef.current?.focus({ preventScroll: true });
    }
  }, [open, owns]);

  const setPanel = useCallback(
    (el: HTMLDivElement | null) => {
      panelRef.current = el;
      float.setFloating(el);
    },
    [float],
  );

  const panelStyle: CSSProperties =
    padding != null
      ? { ...float.floatingStyles, ["--alk-popover-pad" as string]: typeof padding === "number" ? `${padding}px` : padding }
      : float.floatingStyles;

  const triggerProps: PopoverTriggerProps = {
    ref: setTrigger,
    "aria-haspopup": "dialog",
    "aria-expanded": open,
    "aria-controls": panelId,
    "data-open": open || undefined,
    onClick: () => {
      clearTimers();
      setOpen((v) => !v);
    },
    ...(hover
      ? {
          onPointerEnter: scheduleOpen,
          onPointerLeave: scheduleClose,
          onFocus: () => {
            clearTimers();
            setOpen(true);
          },
          onBlur: scheduleClose,
        }
      : {}),
  };

  return (
    <div className="alk-popover-wrap" ref={wrapRef}>
      {trigger(triggerProps)}
      {mounted
        ? createPortal(
            <div
              ref={setPanel}
              id={panelId}
              className={
                panelClassName ? `alk-floating-surface alk-popover ${panelClassName}` : "alk-floating-surface alk-popover"
              }
              style={panelStyle}
              data-side={float.side}
              data-state={state}
              role="dialog"
              aria-label={label}
              // The trigger↔panel bridge: entering the panel cancels a pending close, leaving it
              // schedules one — so a hover popover stays put while the pointer is over either half.
              onPointerEnter={hover ? clearTimers : undefined}
              onPointerLeave={hover ? scheduleClose : undefined}
            >
              <FloatingOwnerContext.Provider value={register}>
                {typeof children === "function" ? children({ close }) : children}
              </FloatingOwnerContext.Provider>
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}
