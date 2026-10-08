// A small menu button: a list of commands with their keys, keyboard
// navigable (arrows, Home, End, Escape), closing on a pick or an outside
// click. The list opens inside the area it can be seen in (the pane the
// notebook sits in, a scrolling cell list, the window): it flips and slides
// rather than spilling past an edge where it would be cut off or drawn under
// a neighbouring pane.

import { useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

export interface MenuItem {
  id: string;
  label: string;
  keys?: string;
  disabled?: boolean;
  /** Draws a separator before this item. */
  separated?: boolean;
  checked?: boolean;
}

export interface MenuProps {
  label: string;
  /** What the button shows (the label when unset). */
  trigger?: ReactNode;
  items: readonly MenuItem[];
  onPick(id: string): void;
  className?: string;
  align?: "start" | "end";
}

/** A box in window coordinates. */
export interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/** Where an open list goes, relative to its menu's own box. */
export interface Placement {
  left: number;
  top: number;
  maxHeight: number;
  /** Opened above its button: there was no room below. */
  above: boolean;
}

/** The space kept between a list and the edge of what can be seen. */
const MARGIN = 4;
/** The gap between a button and the list it opens. */
const GAP = 4;

/** Where a list of `size` opened from `button` goes so it stays inside
 *  `bounds`: on the `align` side of the button when that fits, slid back in
 *  when it does not; below the button, or above it when there is more room
 *  there; and no taller than the room it has. Positions are relative to
 *  `origin`, the box the list is positioned against. */
export function placeMenu(button: Box, size: { width: number; height: number }, bounds: Box, align: "start" | "end", origin: Box): Placement {
  const minLeft = bounds.left + MARGIN;
  const maxRight = bounds.right - MARGIN;
  let left = align === "start" ? button.left : button.right - size.width;
  if (left + size.width > maxRight) left = maxRight - size.width;
  if (left < minLeft) left = minLeft;
  const below = bounds.bottom - MARGIN - (button.bottom + GAP);
  const aboveRoom = button.top - GAP - (bounds.top + MARGIN);
  const above = size.height > below && aboveRoom > below;
  const room = Math.max(0, above ? aboveRoom : below);
  const height = Math.min(size.height, room);
  const top = above ? button.top - GAP - height : button.bottom + GAP;
  return { left: left - origin.left, top: top - origin.top, maxHeight: room, above };
}

/** What of the window an element inside `from` can be seen in: the window
 *  cut down by every ancestor that clips what overflows it. */
export function visibleBounds(from: Element): Box {
  const view = from.ownerDocument.defaultView;
  let box: Box = { left: 0, top: 0, right: view?.innerWidth ?? Infinity, bottom: view?.innerHeight ?? Infinity };
  for (let el = from.parentElement; el !== null && view !== null; el = el.parentElement) {
    const style = view.getComputedStyle(el);
    if (style.overflowX === "visible" && style.overflowY === "visible") continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    box = { left: Math.max(box.left, r.left), top: Math.max(box.top, r.top), right: Math.min(box.right, r.right), bottom: Math.min(box.bottom, r.bottom) };
  }
  return box;
}

export function Menu({ label, trigger, items, onPick, className, align = "end" }: MenuProps) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [placement, setPlacement] = useState<Placement | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const id = useId();
  const enabled = items.map((item, i) => (item.disabled ? -1 : i)).filter((i) => i >= 0);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (root.current !== null && !root.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  // Placed before it is painted, so it never shows where it would be cut off.
  useLayoutEffect(() => {
    if (!open) {
      setPlacement(null);
      return;
    }
    const place = () => {
      const el = list.current;
      const at = button.current;
      const box = root.current;
      if (el === null || at === null || box === null) return;
      setPlacement(placeMenu(at.getBoundingClientRect(), { width: el.offsetWidth, height: el.scrollHeight }, visibleBounds(box), align, box.getBoundingClientRect()));
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [open, align]);

  useEffect(() => {
    if (open) list.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.focus({ preventScroll: true });
  }, [open, active]);

  const close = () => {
    setOpen(false);
    button.current?.focus();
  };

  const step = (delta: number) => {
    if (enabled.length === 0) return;
    const at = enabled.indexOf(active);
    const next = enabled[(at + delta + enabled.length) % enabled.length];
    if (next !== undefined) setActive(next);
  };

  return (
    <div ref={root} className={`nb-menu ${className ?? ""}`}>
      <button
        ref={button}
        type="button"
        className="nb-icon-button"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={(event) => {
          event.stopPropagation();
          setActive(enabled[0] ?? 0);
          setOpen((o) => !o);
        }}
      >
        {trigger ?? label}
      </button>
      {open ? (
        <ul
          ref={list}
          id={id}
          role="menu"
          aria-label={label}
          className={`nb-menu__list nb-menu__list--${align}${placement?.above ? " nb-menu__list--above" : ""}`}
          style={placement === null ? HIDDEN : { left: placement.left, top: placement.top, right: "auto", margin: 0, maxHeight: placement.maxHeight }}
          data-placed={placement === null ? undefined : "true"}
          onKeyDown={(event) => {
            event.stopPropagation();
            if (event.key === "ArrowDown") step(1);
            else if (event.key === "ArrowUp") step(-1);
            else if (event.key === "Home") setActive(enabled[0] ?? 0);
            else if (event.key === "End") setActive(enabled[enabled.length - 1] ?? 0);
            else if (event.key === "Escape" || event.key === "Tab") close();
            else return;
            event.preventDefault();
          }}
        >
          {items.map((item, index) => (
            <li key={item.id} role="none" className={item.separated ? "nb-menu__sep" : undefined}>
              <button
                type="button"
                role={item.checked === undefined ? "menuitem" : "menuitemcheckbox"}
                aria-checked={item.checked}
                data-index={index}
                tabIndex={index === active ? 0 : -1}
                disabled={item.disabled}
                className="nb-menu__item"
                onClick={(event) => {
                  event.stopPropagation();
                  close();
                  onPick(item.id);
                }}
              >
                <span>{item.label}</span>
                {item.keys ? <kbd>{item.keys}</kbd> : null}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** Laid out but not seen, until it is placed. */
const HIDDEN: CSSProperties = { visibility: "hidden" };
