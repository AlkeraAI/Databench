import {
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import { useEscLayer, useFloating, usePresence, type FloatingAlign } from "../../../hooks";
import { FloatingOwnerContext } from "../../../hooks/floatingOwner";
import { cx } from "../../cx";
import { ChevronRightIcon } from "../../icons";

/**
 * Submenu: one row of a menu that opens a flyout of further rows beside it.
 *
 * The row is a `menuitem` with `aria-haspopup="menu"`; the flyout is a `role="menu"` panel the
 * caller fills with its own `menuitem` rows. The primitive owns only the mechanics:
 *  - opens on hover after a short dwell, on click (touch), and on ArrowRight / Enter / Space;
 *  - hover intent: leaving the row or the flyout closes it only after a grace period that entering
 *    either cancels, so a pointer cutting diagonally across a sibling row reaches the flyout;
 *  - a keyboard open moves focus to the first enabled row; ArrowUp / ArrowDown / Home / End walk the
 *    flyout's rows; ArrowLeft and Escape close the flyout alone and put focus back on the row;
 *  - anchored to the row's right edge and flipped to the left when there is no room; a row in the
 *    lower half of the viewport grows its flyout upward, so a menu at the bottom of the screen keeps
 *    every row on screen;
 *  - portaled to `<body>`, and registered with an enclosing Popover so a press in the flyout does
 *    not read as a press outside the menu.
 */

export interface SubmenuRenderProps {
  /** Close the flyout (focus returns to the row when it was inside the flyout). */
  close: () => void;
}

export interface SubmenuProps {
  /** The row's text; also the flyout's accessible name. */
  label: string;
  /** The flyout's rows: `role="menuitem"` controls, or a render-prop given `{ close }`. */
  children: ReactNode | ((props: SubmenuRenderProps) => ReactNode);
  /** Class for the row, so it matches the rows beside it. */
  className?: string;
  /** Extra class on the flyout panel. */
  panelClassName?: string;
}

// A sweep past the row does not flash the flyout; leaving gives the pointer time to cross over.
const OPEN_DELAY_MS = 100;
const CLOSE_DELAY_MS = 300;
const EXIT_MS = 120;

const ROW_SELECTOR = '[role^="menuitem"]:not(:disabled):not([aria-disabled="true"])';

export function Submenu({ label, children, className, panelClassName }: SubmenuProps) {
  const [open, setOpen] = useState(false);
  const [align, setAlign] = useState<FloatingAlign>("start");
  const { mounted, state } = usePresence(open, EXIT_MS);
  const float = useFloating({ open, side: "right", align, gap: 4 });
  const panelId = useId();
  const rowRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const focusFirstOnOpen = useRef(false);
  const register = useContext(FloatingOwnerContext);

  const timer = useRef<number | undefined>(undefined);
  const clearTimer = useCallback(() => window.clearTimeout(timer.current), []);
  useEffect(() => clearTimer, [clearTimer]);

  const show = useCallback(
    (focusFirst: boolean) => {
      clearTimer();
      const row = rowRef.current;
      if (row) {
        const r = row.getBoundingClientRect();
        setAlign((r.top + r.bottom) / 2 > window.innerHeight / 2 ? "end" : "start");
      }
      setOpen(true);
      // Already showing (a hover opened it): move in now. Otherwise once the panel mounts.
      if (focusFirst && panelRef.current) focusRow(panelRef.current, "first");
      else focusFirstOnOpen.current = focusFirst;
    },
    [clearTimer],
  );

  const close = useCallback(() => {
    clearTimer();
    setOpen(false);
  }, [clearTimer]);

  const closeToRow = useCallback(() => {
    close();
    rowRef.current?.focus({ preventScroll: true });
  }, [close]);

  const scheduleShow = (e: ReactPointerEvent) => {
    // A touch press fires pointerenter just before its click; the click opens it.
    if (e.pointerType === "touch") return;
    clearTimer();
    timer.current = window.setTimeout(() => show(false), OPEN_DELAY_MS);
  };
  const scheduleClose = (e: ReactPointerEvent) => {
    if (e.pointerType === "touch") return;
    clearTimer();
    timer.current = window.setTimeout(close, CLOSE_DELAY_MS);
  };

  // Escape closes the flyout alone: it registers above the menu it grew out of.
  useEscLayer(open, closeToRow);

  // A close for any other reason (the pointer left) while focus is inside the flyout hands focus
  // back to the row, so the keyboard is never left on <body>.
  useEffect(() => {
    if (open) return;
    const active = document.activeElement;
    if (active instanceof Node && panelRef.current?.contains(active)) rowRef.current?.focus({ preventScroll: true });
  }, [open]);

  useLayoutEffect(() => {
    if (!mounted || !open || !focusFirstOnOpen.current || !panelRef.current) return;
    focusFirstOnOpen.current = false;
    focusRow(panelRef.current, "first");
  }, [mounted, open]);

  useEffect(() => {
    const el = panelRef.current;
    if (!mounted || !el || !register) return;
    return register(el);
  }, [mounted, register]);

  const setRow = useCallback(
    (el: HTMLButtonElement | null) => {
      rowRef.current = el;
      float.setReference(el);
    },
    [float],
  );
  const setPanel = useCallback(
    (el: HTMLDivElement | null) => {
      panelRef.current = el;
      float.setFloating(el);
    },
    [float],
  );

  const onRowKeyDown = (e: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (e.key !== "ArrowRight" && e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    show(true);
  };

  const onPanelKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const panel = panelRef.current;
    if (!panel) return;
    const moves: Record<string, Walk | undefined> = {
      ArrowDown: "next",
      ArrowUp: "previous",
      Home: "first",
      End: "last",
    };
    const walk = moves[e.key];
    if (walk) {
      // Stopped here so the menu this grew out of does not walk its own rows on the same press.
      e.preventDefault();
      e.stopPropagation();
      focusRow(panel, walk);
      return;
    }
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      e.stopPropagation();
      closeToRow();
    }
    // Escape is left to the shared stack (useEscLayer above): stopping it here would keep it from
    // reaching the document listener that routes it.
  };

  return (
    <>
      <button
        ref={setRow}
        type="button"
        role="menuitem"
        className={cx("alk-submenu__row", className)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        data-open={open || undefined}
        onClick={() => show(false)}
        onKeyDown={onRowKeyDown}
        onPointerEnter={scheduleShow}
        onPointerLeave={scheduleClose}
      >
        <span className="alk-submenu__label">{label}</span>
        <span className="alk-submenu__chev" aria-hidden="true">
          <ChevronRightIcon size={14} />
        </span>
      </button>
      {mounted
        ? createPortal(
            <div
              ref={setPanel}
              id={panelId}
              className={cx("alk-floating-surface", "alk-submenu", panelClassName)}
              style={float.floatingStyles}
              data-side={float.side}
              data-state={state}
              role="menu"
              aria-label={label}
              aria-orientation="vertical"
              onKeyDown={onPanelKeyDown}
              onPointerEnter={clearTimer}
              onPointerLeave={scheduleClose}
            >
              {typeof children === "function" ? children({ close }) : children}
            </div>,
            document.body,
          )
        : null}
    </>
  );
}

type Walk = "first" | "last" | "next" | "previous";

/** Move focus among the panel's enabled rows, wrapping at either end. */
function focusRow(panel: HTMLElement, walk: Walk): void {
  const rows = [...panel.querySelectorAll<HTMLElement>(ROW_SELECTOR)];
  if (rows.length === 0) return;
  const at = rows.indexOf(document.activeElement as HTMLElement);
  const last = rows.length - 1;
  const next =
    walk === "first" ? 0
    : walk === "last" ? last
    : walk === "next" ? (at < 0 || at >= last ? 0 : at + 1)
    : at <= 0 ? last
    : at - 1;
  rows[next].focus();
}
