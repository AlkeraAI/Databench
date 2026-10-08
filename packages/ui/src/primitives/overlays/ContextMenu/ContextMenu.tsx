import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import { cx } from "../../cx";
import { ChevronRightIcon } from "../../icons";

/**
 * ContextMenu — the one right-click menu primitive.
 *
 * A pointer- or element-anchored menu the page opens itself: there is no built-in trigger chrome,
 * because a context menu belongs to a whole row, grid or canvas rather than to one button. The
 * primitive owns only the mechanics every context menu shares:
 *  - opens at a pointer position OR at the box of the element that has focus, so a right-click and
 *    Shift+F10 land in the same menu (`useContextMenu` wires both);
 *  - the full keyboard model — arrows, Home/End, Right/Left across submenus, Enter/Space to select,
 *    Escape to close, type-ahead by first letter — over a roving tabindex;
 *  - a disabled item states WHY: the `disabled` string IS the reason, shown as the row's tooltip and
 *    read by assistive tech as the item's description. Such a row still takes focus (so the reason
 *    is reachable by keyboard) but can never be selected;
 *  - closes on select, outside press, scroll, window blur and Escape, handing focus back to whatever
 *    opened it.
 *
 * Capability gating is the caller's concern: it decides which items exist and passes `disabled` with
 * a reason a person can act on.
 */

/** One row of the menu. A row with a non-empty `submenu` opens a child menu instead of selecting. */
export interface ContextMenuItem {
  /** Stable identity for the row; also how a caller and its tests address it. */
  id: string;
  label: string;
  /** The keystroke that does the same thing elsewhere, rendered dimmed at the trailing edge. */
  shortcut?: string;
  icon?: ReactNode;
  /** Non-empty = disabled, and the string IS the reason (tooltip + accessible description). */
  disabled?: string;
  /** A row whose action destroys something reads in danger ink from the moment the menu opens, so
   *  what the row costs is legible before the pointer reaches it. A refused row keeps the disabled
   *  ink: an action that cannot run states its reason, not its cost. */
  tone?: "destructive";
  onSelect?: () => void;
  submenu?: ContextMenuItem[];
}

/** Where a menu panel is pinned, in viewport coordinates. */
export interface ContextMenuAnchor {
  x: number;
  y: number;
}

export interface ContextMenuProps {
  open: boolean;
  /** Viewport coordinates of the pointer, or of the focused element's corner. */
  anchor: ContextMenuAnchor;
  items: ContextMenuItem[];
  /** Accessible name for the menu (e.g. "Actions for report.csv"). */
  label: string;
  onClose: () => void;
  /** Focused again when the menu closes, per the WAI menu pattern. */
  returnFocus?: HTMLElement | null;
  className?: string;
}

/** Anchor a menu under an element — the keyboard equivalent of a pointer position. */
export function anchorOfElement(el: HTMLElement): ContextMenuAnchor {
  const r = el.getBoundingClientRect();
  return { x: r.left, y: r.bottom };
}

export interface ContextMenuState {
  open: boolean;
  anchor: ContextMenuAnchor;
  /** Open the menu at an explicit point; `trigger` takes focus back on close. */
  openAt: (anchor: ContextMenuAnchor, trigger?: HTMLElement | null) => void;
  close: () => void;
  /** Spread on the element that owns the menu (a row, a grid, a canvas). */
  triggerProps: {
    onContextMenu: (e: ReactMouseEvent<HTMLElement>) => void;
    onKeyDown: (e: ReactKeyboardEvent<HTMLElement>) => void;
  };
  /** Spread on `<ContextMenu>` — `open`, `anchor`, `onClose` and `returnFocus` in one. */
  menuProps: Pick<ContextMenuProps, "open" | "anchor" | "onClose" | "returnFocus">;
}

/**
 * Wire both openers of a context menu: a right-click (anchored to the pointer) and Shift+F10 or the
 * Menu key (anchored to the element that has focus). A keyboard-synthesised `contextmenu` event
 * carries no usable pointer position, so it too falls back to the focused element's box.
 */
export function useContextMenu(): ContextMenuState {
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<ContextMenuAnchor>({ x: 0, y: 0 });
  const [trigger, setTrigger] = useState<HTMLElement | null>(null);

  const openAt = useCallback((at: ContextMenuAnchor, from?: HTMLElement | null) => {
    setTrigger(from ?? (document.activeElement as HTMLElement | null));
    setAnchor(at);
    setOpen(true);
  }, []);
  const close = useCallback(() => setOpen(false), []);

  const focusedWithin = useCallback((host: HTMLElement): HTMLElement => {
    const activeEl = document.activeElement;
    return activeEl instanceof HTMLElement && host.contains(activeEl) ? activeEl : host;
  }, []);

  const onContextMenu = useCallback(
    (e: ReactMouseEvent<HTMLElement>) => {
      e.preventDefault();
      const from = focusedWithin(e.currentTarget);
      // A keyboard-synthesised contextmenu reports (0, 0); anchor it to what has focus instead.
      const fromPointer = e.clientX !== 0 || e.clientY !== 0;
      openAt(fromPointer ? { x: e.clientX, y: e.clientY } : anchorOfElement(from), from);
    },
    [focusedWithin, openAt],
  );

  const onKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLElement>) => {
      if (!(e.key === "F10" && e.shiftKey) && e.key !== "ContextMenu") return;
      e.preventDefault();
      const el = focusedWithin(e.currentTarget);
      openAt(anchorOfElement(el), el);
    },
    [focusedWithin, openAt],
  );

  return {
    open,
    anchor,
    openAt,
    close,
    triggerProps: { onContextMenu, onKeyDown },
    menuProps: { open, anchor, onClose: close, returnFocus: trigger },
  };
}

export function ContextMenu({ open, anchor, items, label, onClose, returnFocus, className }: ContextMenuProps) {
  const returnRef = useRef<HTMLElement | null>(null);
  if (open && returnFocus) returnRef.current = returnFocus;

  const closeAll = useCallback(() => {
    onClose();
    // Focus goes back to the opener, or it resolves to <body> and the row is lost.
    returnRef.current?.focus();
  }, [onClose]);

  // Dismiss on anything that moves the menu away from what it points at.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target;
      if (t instanceof Node) {
        // Every panel of this menu (submenus included) is portaled; a press in any of them is inside.
        for (const p of document.querySelectorAll(".alk-ctxmenu")) if (p.contains(t)) return;
      }
      closeAll();
    };
    const onScroll = () => closeAll();
    const onBlur = () => closeAll();
    // Capture: a canvas or grid that stops propagation would otherwise swallow the dismissal.
    document.addEventListener("mousedown", onDown, true);
    document.addEventListener("scroll", onScroll, true);
    window.addEventListener("blur", onBlur);
    return () => {
      document.removeEventListener("mousedown", onDown, true);
      document.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("blur", onBlur);
    };
  }, [open, closeAll]);

  // Where the panel portals to. The nearest themed ancestor of the element the menu was
  // opened from wins — an `aria-modal` dialog (a panel portaled past it is invisible to a
  // screen reader that is ignoring everything outside the dialog), otherwise the element
  // carrying the active light/dark scheme (`[data-alkera-color-scheme]`) or the IDE surface
  // (`[data-alkera-ide]`), so the panel resolves the SAME `--alk*` tokens the page it grew
  // out of resolved. Portaling straight to `<body>` picked up the document's scheme instead,
  // which drew a light menu over a dark page. Falls back to body.
  const portalTarget =
    returnRef.current?.closest<HTMLElement>(
      '[role="dialog"][aria-modal="true"], [data-alkera-color-scheme], [data-alkera-ide]',
    ) ?? document.body;

  if (!open) return null;
  return createPortal(
    <MenuPanel anchor={anchor} items={items} label={label} onDismiss={closeAll} className={className} depth={0} />,
    portalTarget,
  );
}

/** Where a panel sits and how tall it may grow, in viewport pixels. */
interface Placement {
  x: number;
  y: number;
  /** The viewport's height once the panel is measured: a taller menu scrolls inside itself. */
  maxHeight: number | null;
}

/**
 * Place a panel of `size` at `anchor` inside `viewport`. It opens below and to the right of the
 * anchor; where there is no room it flips back over the anchor; where it fits on neither side it
 * is pinned to the viewport's far edge. The panel is never taller than the viewport (it scrolls
 * instead), so the result always keeps every row on screen. A panel not yet laid out (zero size)
 * stays at the anchor.
 */
export function placePanel(
  anchor: ContextMenuAnchor,
  size: { width: number; height: number },
  viewport: { width: number; height: number },
): Placement {
  if (!(size.width > 0) || !(size.height > 0)) return { x: anchor.x, y: anchor.y, maxHeight: null };
  const along = (start: number, extent: number, room: number): number => {
    if (start + extent <= room) return start;
    if (start - extent >= 0) return start - extent;
    return Math.max(0, room - extent);
  };
  const height = Math.min(size.height, viewport.height);
  return {
    x: along(anchor.x, Math.min(size.width, viewport.width), viewport.width),
    y: along(anchor.y, height, viewport.height),
    maxHeight: viewport.height,
  };
}

interface MenuPanelProps {
  anchor: ContextMenuAnchor;
  items: ContextMenuItem[];
  label: string;
  onDismiss: () => void;
  /** Called when a submenu hands focus back to its parent row (Left, or Escape inside it). */
  onCloseSelf?: () => void;
  className?: string;
  depth: number;
}

function MenuPanel({ anchor, items, label, onDismiss, onCloseSelf, className, depth }: MenuPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);
  const [active, setActive] = useState(0);
  const [openSub, setOpenSub] = useState<number | null>(null);
  const { x: anchorX, y: anchorY } = anchor;
  const [place, setPlace] = useState<Placement>({ x: anchorX, y: anchorY, maxHeight: null });
  const reasonId = useId();

  // Keep the whole panel on screen, so every row can be reached. Placement is recomputed whenever
  // the panel's size changes, not only when it opens: a caller whose rows depend on a selection the
  // same click makes (a right-click on an unselected row) renders a short menu first and the full
  // one a frame later, and a position measured for the short one pushes the tall one off the page.
  const reposition = useCallback(() => {
    const el = panelRef.current;
    if (!el) return;
    const next = placePanel(
      { x: anchorX, y: anchorY },
      { width: el.offsetWidth, height: el.offsetHeight },
      { width: window.innerWidth, height: window.innerHeight },
    );
    setPlace((prev) =>
      prev.x === next.x && prev.y === next.y && prev.maxHeight === next.maxHeight ? prev : next,
    );
  }, [anchorX, anchorY]);

  useLayoutEffect(() => {
    reposition();
  }, [reposition, items]);

  useEffect(() => {
    const el = panelRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => reposition());
    observer.observe(el);
    window.addEventListener("resize", reposition);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", reposition);
    };
  }, [reposition]);

  // Roving tabindex: exactly one row is tabbable, and while no submenu is open it holds DOM focus.
  useEffect(() => {
    if (openSub === null) rowRefs.current[active]?.focus();
  }, [active, openSub]);

  const select = useCallback(
    (i: number) => {
      const item = items[i];
      if (!item || item.disabled) return;
      if (item.submenu?.length) {
        setOpenSub(i);
        return;
      }
      item.onSelect?.();
      onDismiss();
    },
    [items, onDismiss],
  );

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const last = items.length - 1;
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setActive((i) => (i >= last ? 0 : i + 1));
        return;
      case "ArrowUp":
        e.preventDefault();
        setActive((i) => (i <= 0 ? last : i - 1));
        return;
      case "Home":
        e.preventDefault();
        setActive(0);
        return;
      case "End":
        e.preventDefault();
        setActive(last);
        return;
      case "ArrowRight":
        if (items[active]?.submenu?.length && !items[active]?.disabled) {
          e.preventDefault();
          setOpenSub(active);
        }
        return;
      case "ArrowLeft":
        if (onCloseSelf) {
          e.preventDefault();
          onCloseSelf();
        }
        return;
      case "Enter":
      case " ":
        e.preventDefault();
        select(active);
        return;
      case "Escape":
        e.preventDefault();
        e.stopPropagation();
        if (onCloseSelf) onCloseSelf();
        else onDismiss();
        return;
      default:
        break;
    }
    // Type-ahead: one printable character jumps to the NEXT row starting with it, wrapping — so
    // repeating the letter cycles the matches instead of sticking on the first.
    if (e.key.length !== 1 || e.metaKey || e.ctrlKey || e.altKey) return;
    const want = e.key.toLowerCase();
    for (let n = 1; n <= items.length; n += 1) {
      const i = (active + n) % items.length;
      const item = items[i];
      if (item && item.label.toLowerCase().startsWith(want)) {
        e.preventDefault();
        setActive(i);
        return;
      }
    }
  };

  const subItem = openSub === null ? undefined : items[openSub];
  const subRect = openSub === null ? undefined : rowRefs.current[openSub]?.getBoundingClientRect();

  return (
    <>
      <div
        ref={panelRef}
        className={cx("alk-floating-surface", "alk-ctxmenu", className)}
        style={{ left: place.x, top: place.y, ...(place.maxHeight != null ? { maxHeight: place.maxHeight } : {}) }}
        data-state="open"
        data-depth={depth}
        role="menu"
        aria-label={label}
        aria-orientation="vertical"
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        {items.map((item, i) => {
          const hasSub = !!item.submenu?.length;
          const disabled = !!item.disabled;
          return (
            <div
              key={item.id}
              ref={(el) => {
                rowRefs.current[i] = el;
              }}
              className="alk-ctxmenu__item"
              role="menuitem"
              data-item={item.id}
              data-tone={item.tone}
              data-disabled={disabled || undefined}
              aria-disabled={disabled || undefined}
              aria-haspopup={hasSub ? "menu" : undefined}
              aria-expanded={hasSub ? openSub === i : undefined}
              aria-describedby={disabled ? `${reasonId}-${item.id}` : undefined}
              title={item.disabled}
              tabIndex={active === i ? 0 : -1}
              onMouseEnter={() => setActive(i)}
              onClick={() => select(i)}
            >
              {item.icon != null ? (
                <span className="alk-ctxmenu__icon" aria-hidden="true">
                  {item.icon}
                </span>
              ) : null}
              <span className="alk-ctxmenu__label">{item.label}</span>
              {item.shortcut ? <span className="alk-ctxmenu__shortcut">{item.shortcut}</span> : null}
              {hasSub ? (
                <span className="alk-ctxmenu__chev" aria-hidden="true">
                  <ChevronRightIcon size={14} />
                </span>
              ) : null}
              {disabled ? (
                <span className="alk-ctxmenu__reason" id={`${reasonId}-${item.id}`}>
                  {item.disabled}
                </span>
              ) : null}
            </div>
          );
        })}
      </div>
      {subItem?.submenu && subRect ? (
        <MenuPanel
          anchor={{ x: subRect.right, y: subRect.top }}
          items={subItem.submenu}
          label={subItem.label}
          onDismiss={onDismiss}
          onCloseSelf={() => setOpenSub(null)}
          depth={depth + 1}
        />
      ) : null}
    </>
  );
}
