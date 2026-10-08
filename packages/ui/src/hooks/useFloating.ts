import {
  autoUpdate,
  flip,
  offset,
  shift,
  size,
  useFloating as floatingUi,
  type Placement,
  type Strategy,
  type VirtualElement,
} from "@floating-ui/react";
import { useCallback, useEffect, useState, type CSSProperties } from "react";

/**
 * The one floating-positioner for @alkera/ui — anything that floats off an anchor (dropdowns,
 * popovers, tooltips) shares it, so they all anchor, flip, and stack the same way. Built on
 * `@floating-ui/react`; it adds a z-index stack so a float opened later sits above ones opened
 * before it, and an explicit side/align override so a caller can force the open direction.
 *
 * - Smart anchoring: `flip()` swaps to the roomier side at a viewport edge, `shift()` slides it
 *   back inside, `size()` caps its height so a long list scrolls rather than overflowing.
 * - Auto cross-axis: with no `align`, a trigger in the right half opens `end`-aligned and one in
 *   the left half opens `start`-aligned, so a menu never runs off.
 * - Override: pass `side` / `align` to pin the direction (e.g. a footer menu opens `top`).
 * - Side lock: `allowFlip: false` keeps a field-owned result list on its requested side while the
 *   size middleware limits the list to the space available there.
 */

export type FloatingSide = "top" | "bottom" | "left" | "right";
export type FloatingAlign = "start" | "center" | "end";

// Every float paints ABOVE the overlay/modal band and below toasts — a Select opened inside a Modal
// (--alkZModal 1200) or a menu from a SidePanel (--alkZOverlay 1100) must never be buried behind that
// surface (you only ever open a menu from the topmost surface anyway). Mirrors --alkZFloating.
const Z_FLOATING_BASE = 1250;
// A per-open cursor keeps a float opened later above earlier ones (a submenu over its menu, a tooltip
// over a menu). It wraps within a narrow band — far more slots than can ever be open at once — so a
// long session of opens/hovers can't climb the stack into the toast layer (--alkZToast 1400).
const Z_STACK_SPAN = 40;
let zCursor = 0;

export interface UseFloatingOptions {
  open: boolean;
  /** Force the side the float opens toward. Omit to open `bottom` (with an automatic flip). */
  side?: FloatingSide;
  /** Force cross-axis alignment. Omit to anchor by the trigger's half of the viewport. */
  align?: FloatingAlign;
  /** Gap between anchor and float, px. */
  gap?: number;
  /** Floor the float's width to the anchor's, so a menu lines up under its trigger. */
  matchWidth?: boolean;
  /** Lock the float to the anchor's width. Use for a field-owned result list whose rows must not
   *  widen or narrow independently of the control. */
  exactWidth?: boolean;
  /** Let the float move to the opposite side when space runs out. Defaults to true. */
  allowFlip?: boolean;
  /** Cap the float's height to the available space (a longer body then scrolls). Turn OFF for a
   *  short menu in a transformed container, where the available-space math under-reads. */
  clampHeight?: boolean;
  /** Hard cap on the float's height when clamping; a longer body scrolls inside it. */
  maxHeight?: number;
  strategy?: Strategy;
}

export interface FloatingResult {
  setReference: (el: HTMLElement | null) => void;
  /** Pin the float to a point instead of the trigger — a cursor rest position as a floating-ui
   *  virtual element — while the trigger keeps owning alignment and interactions. Null returns
   *  positioning to the trigger. */
  setPositionReference: (ref: VirtualElement | null) => void;
  setFloating: (el: HTMLElement | null) => void;
  /** position + offset + the claimed z-index; spread onto the floating element's style. */
  floatingStyles: CSSProperties;
  /** Placement AFTER flip/shift — drive transform-origin / slide direction off this. */
  placement: Placement;
  /** The side the float actually landed on, for the caller's open animation. */
  side: FloatingSide;
}

function toPlacement(side: FloatingSide, align: FloatingAlign): Placement {
  return align === "center" ? side : (`${side}-${align}` as Placement);
}

export function useFloating(opts: UseFloatingOptions): FloatingResult {
  const {
    open,
    side,
    align,
    gap = 6,
    matchWidth = false,
    exactWidth = false,
    allowFlip = true,
    clampHeight = true,
    maxHeight = 360,
    strategy = "fixed",
  } = opts;
  const [trigger, setTrigger] = useState<HTMLElement | null>(null);
  const [zIndex, setZIndex] = useState(Z_FLOATING_BASE);
  const [placementReq, setPlacementReq] = useState<Placement>(
    toPlacement(side ?? "bottom", align ?? "start"),
  );

  // Claim the next stacking slot each time the float opens, so a float anchored inside a drawer/dialog
  // sits above it and a float opened later sits above earlier ones.
  useEffect(() => {
    if (!open) return;
    zCursor = (zCursor % Z_STACK_SPAN) + 1;
    setZIndex(Z_FLOATING_BASE + zCursor);
  }, [open]);

  // Resolve the requested placement: fully explicit when side+align are given, otherwise auto
  // cross-axis alignment by the trigger's half of the viewport.
  useEffect(() => {
    if (side && align) {
      setPlacementReq(toPlacement(side, align));
      return;
    }
    if (!trigger) return;
    const compute = () => {
      const r = trigger.getBoundingClientRect();
      const a: FloatingAlign =
        align ??
        ((r.left + r.right) / 2 > window.innerWidth / 2 ? "end" : "start");
      setPlacementReq(toPlacement(side ?? "bottom", a));
    };
    compute();
    window.addEventListener("resize", compute);
    return () => window.removeEventListener("resize", compute);
  }, [trigger, open, side, align]);

  const middleware = [
    offset(gap),
    ...(allowFlip ? [flip({ padding: 8 })] : []),
    // `shift` keeps the panel within the viewport along its alignment axis. With no `flip`, it does
    // not cross the anchor onto the opposite side.
    shift({ padding: 8 }),
  ];
  // `size` constrains the float to the available space. Include it ONLY when something needs it —
  // clamping height or matching the anchor width.
  if (clampHeight || matchWidth || exactWidth) {
    middleware.push(
      size({
        padding: 8,
        apply({ availableHeight, rects, elements }) {
          Object.assign(elements.floating.style, {
            ...(clampHeight
              ? {
                  maxHeight: `${Math.max(0, Math.min(availableHeight, maxHeight))}px`,
                }
              : { maxHeight: "" }),
            ...(exactWidth
              ? {
                  width: `${rects.reference.width}px`,
                  minWidth: `${rects.reference.width}px`,
                  maxWidth: `${rects.reference.width}px`,
                }
              : matchWidth
                ? {
                    width: "",
                    minWidth: `${rects.reference.width}px`,
                    maxWidth: "",
                  }
                : { width: "", minWidth: "", maxWidth: "" }),
          });
        },
      }),
    );
  }

  const {
    refs,
    x,
    y,
    strategy: resolvedStrategy,
    placement,
  } = floatingUi({
    open,
    strategy,
    placement: placementReq,
    whileElementsMounted: autoUpdate,
    middleware,
  });

  const setReference = useCallback(
    (el: HTMLElement | null) => {
      setTrigger(el);
      refs.setReference(el);
    },
    [refs],
  );

  // `size()` writes inline constraints. Clear a constraint when a still-mounted caller turns its
  // corresponding option off, including the all-options-off case where the middleware is absent.
  useEffect(() => {
    const element = refs.floating.current;
    if (!element) return;
    if (!clampHeight) element.style.maxHeight = "";
    if (!exactWidth) element.style.width = "";
    if (!matchWidth && !exactWidth) element.style.minWidth = "";
    if (!exactWidth) element.style.maxWidth = "";
  }, [clampHeight, exactWidth, matchWidth, refs.floating]);

  // Position via top/left, NOT a transform — so the caller keeps `transform` free for the float's
  // own open/close animation. `right`/`bottom: auto` are pinned inline so a stray stylesheet rule
  // can't set an opposing offset and stretch the float's size.
  return {
    setReference,
    setPositionReference: refs.setPositionReference,
    setFloating: refs.setFloating,
    floatingStyles: {
      position: resolvedStrategy,
      top: y ?? 0,
      left: x ?? 0,
      right: "auto",
      bottom: "auto",
      zIndex,
    },
    placement,
    side: placement.split("-")[0] as FloatingSide,
  };
}
