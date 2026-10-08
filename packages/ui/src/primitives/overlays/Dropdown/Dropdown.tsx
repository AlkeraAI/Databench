import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";

import { cx } from "../../cx";
import { useDismiss, useFloating, usePresence, type FloatingAlign, type FloatingSide } from "../../../hooks";
import { pushEsc } from "../../../hooks/overlayStack";
import { Button, type ButtonFill, type ButtonVariant } from "../../controls/Button";
import { CheckIcon, ChevronDownIcon } from "../../icons";
import type { ControlSize } from "../../sizes";

/**
 * Dropdown — the one anchored popover-menu primitive.
 *
 * An anchored, portaled menu that animates open AND closed and dismisses on an outside press or
 * Escape. The caller renders its own trigger and panel body, so the page owns its chrome; the base
 * owns only the mechanics every menu shares:
 *  - anchored + flips at a viewport edge (`useFloating`);
 *  - portaled up to the nearest themed root, so it escapes a scroll-clipping ancestor while still
 *    inheriting the tokens and the active scheme (falls back to body);
 *  - mounted through its exit so it animates both ways (`usePresence` + `data-state`);
 *  - keyboard-dismissable — Escape and outside press close it (`useDismiss`).
 *
 * The trigger is a render-prop given the props it must spread (`ref`, `aria-*`, `onClick`,
 * `data-open`) so the wiring can't drift; `<DropdownItem>` is the standard menu row.
 */

/** The wiring Dropdown injects onto whatever control it renders as its trigger. Internal. */
interface TriggerWiring {
  ref: (el: HTMLElement | null) => void;
  "aria-haspopup": "menu";
  "aria-expanded": boolean;
  "aria-controls": string;
  "data-open"?: true;
  onClick: () => void;
  onKeyDown: (e: ReactKeyboardEvent) => void;
}

/** The standard styling escape every trigger kind accepts. */
interface TriggerStyle {
  className?: string;
  style?: CSSProperties;
}

/**
 * How the Dropdown draws its OWN trigger — the common shapes are built in, custom styling comes
 * through `className` (+ a `content` slot on `menu` for bespoke inner markup). No render-prop:
 *  - `icon`   — a square icon Button (a kebab / dots overflow).
 *  - `select` — the select-like chrome: an optional leading icon, a dimmed label, the emphasised
 *               value, an optional trailing slot, and a chevron that tracks the open state.
 *  - `menu`   — a page-styled menu button: the caller's `content` + `className`, and Dropdown adds
 *               the chevron + wiring. The escape for a bespoke trigger, no render function needed.
 */
export type DropdownTrigger =
  | ({ kind: "icon"; icon: ReactNode; ariaLabel: string; variant?: ButtonVariant; fill?: ButtonFill; size?: ControlSize } & TriggerStyle)
  | ({
      kind: "select";
      leadingIcon?: ReactNode;
      label?: ReactNode;
      value?: ReactNode;
      /** Fill the row rather than shrink to the chosen value. A select standing
       *  among full-width inputs reads as a filter when it shrink-fits. */
      block?: boolean;
      trailing?: ReactNode;
      active?: boolean;
      size?: ControlSize;
      hideChevron?: boolean;
      /** Accessible name for the trigger button — set it when the visible chrome is an icon + a value
       *  (no text label), so the control still announces what it filters (e.g. "Filter by repository:
       *  all repos"). Overrides the content-derived name. */
      ariaLabel?: string;
      /** The trigger's own id, so a `<label htmlFor>` can point at it. */
      id?: string;
      /** An id on the VALUE span, so an `aria-labelledby` can name the control by its field label
       *  plus what it currently reads. Pointing that list at the button itself would work in a
       *  browser and lose the value in jsdom, which does not implement the self-reference step. */
      valueId?: string;
      /** Ids whose text composes the trigger's name. A field's select passes its own label id plus
       *  `valueId`, so the control announces "Deployment tier, Production" rather than the bare
       *  value its content would otherwise supply. */
      ariaLabelledBy?: string;
    } & TriggerStyle)
  | ({ kind: "menu"; content: ReactNode; ariaLabel?: string; active?: boolean; hideChevron?: boolean } & TriggerStyle);

/** A constant-width value box for a `select` trigger: every option stacks in one grid cell so the box
 *  is as wide as the WIDEST option and only the selected one shows — picking never reflows the box. */
export function DropdownTriggerValue({ options, value }: { options: { key: string; label: ReactNode }[]; value: string }) {
  return (
    <span className="alk-dropdown-trigger__valuestack">
      {options.map((o) => (
        <span key={o.key} className="alk-dropdown-trigger__valueopt" data-shown={o.key === value || undefined}>
          {o.label}
        </span>
      ))}
    </span>
  );
}

/** Draw the configured trigger with the Dropdown's wiring spread on. The chevron rotates via the
 *  injected `data-open`. */
function renderTrigger(t: DropdownTrigger, w: TriggerWiring): ReactNode {
  if (t.kind === "icon") {
    return (
      <Button
        {...w}
        iconOnly
        variant={t.variant ?? "secondary"}
        fill={t.fill ?? "ghost"}
        size={t.size}
        aria-label={t.ariaLabel}
        className={t.className}
        style={t.style}
      >
        {t.icon}
      </Button>
    );
  }
  const chevron = t.hideChevron ? null : (
    <span className="alk-dropdown-trigger__chev" data-open={w["data-open"]} aria-hidden="true">
      <ChevronDownIcon size={14} />
    </span>
  );
  if (t.kind === "select") {
    return (
      <button
        {...w}
        type="button"
        id={t.id}
        className={cx("alk-dropdown-trigger", t.className)}
        data-block={t.block || undefined}
        data-size={t.size && t.size !== "lg" ? t.size : undefined}
        data-on={t.active || undefined}
        aria-label={t.ariaLabel}
        aria-labelledby={t.ariaLabelledBy}
        style={t.style}
      >
        {t.leadingIcon != null ? (
          <span className="alk-dropdown-trigger__lead" aria-hidden="true">
            {t.leadingIcon}
          </span>
        ) : null}
        {t.label != null ? <span className="alk-dropdown-trigger__label">{t.label}</span> : null}
        {t.value != null ? (
          <span className="alk-dropdown-trigger__value" id={t.valueId}>
            {t.value}
          </span>
        ) : null}
        {t.trailing}
        {chevron}
      </button>
    );
  }
  // menu — the caller's page-styled content + className; Dropdown owns the chevron + wiring.
  return (
    <button
      {...w}
      type="button"
      className={cx("alk-dropdown-menu-trigger", t.className)}
      data-on={t.active || undefined}
      aria-label={t.ariaLabel}
      style={t.style}
    >
      {t.content}
      {chevron}
    </button>
  );
}

/** Which edge the selected tick rides — `"left"` reserves a leading mark column (today's default),
 *  `"right"` trails the tick and flushes rows left so an icon-less menu isn't indented. */
export type DropdownTickSide = "left" | "right";

interface DropdownContextValue {
  /** Close from inside the panel: the trigger takes focus back, per the WAI menu-button
   *  pattern. Without it a keyboard pick leaves focus on the removed row, which resolves
   *  to `<body>` — and inside a modal that is an escape from the focus trap, because the
   *  trap only redirects Tab at its own first and last stop. */
  close: () => void;
  tickSide: DropdownTickSide;
  /** Register a nested sub-dropdown's portaled panel as a descendant of this menu, so an outside
   *  press that lands inside it doesn't dismiss this menu (the sub-panel lives OUTSIDE this panel in
   *  the DOM once portaled). Returns an unregister; it bubbles up the whole ancestor chain. */
  registerDescendantPanel: (el: HTMLElement) => () => void;
}
const DropdownContext = createContext<DropdownContextValue | null>(null);

/** Close the dropdown from inside its panel (e.g. after picking a single-select option). */
export function useDropdown(): DropdownContextValue {
  const ctx = useContext(DropdownContext);
  if (!ctx) throw new Error("useDropdown must be used inside a <Dropdown> panel");
  return ctx;
}

export interface DropdownProps {
  /** The trigger, drawn by Dropdown from a config: `icon` (a kebab), `select` (the select-like
   *  chrome), or `menu` (page-styled `content` + `className`). No render function needed. */
  trigger: DropdownTrigger;
  /** The panel body. Wrapped in the menu container; use `<DropdownItem>` for rows. */
  children: ReactNode;
  /** Accessible name for the menu panel (e.g. "Filter investigations"). */
  label: string;
  /** Force the side the panel opens toward; omit to open below with an automatic flip. */
  side?: FloatingSide;
  /** Force cross-axis alignment; omit to anchor by the trigger's half of the viewport. */
  align?: FloatingAlign;
  /** Floor the panel's width to the trigger's, so it lines up under a wide control. */
  matchWidth?: boolean;
  /** Extra class on the panel, for page-specific sizing. */
  panelClassName?: string;
  /** Row height of the MENU. Defaults to the trigger's own size when the trigger
   *  declares one, so a menu reads as part of the control that opened it. */
  menuSize?: ControlSize;
  /** Side the selected tick rides (default `"left"`). Use `"right"` for an icon-less menu so rows
   *  flush left instead of reserving a leading mark column. */
  tickSide?: DropdownTickSide;
  /** Render the panel INLINE (inside the wrapper) instead of portaling it. The panel still floats
   *  (`position: fixed`), but staying in the DOM keeps it INSIDE a parent dropdown's panel — so a
   *  nested menu (a sub-filter inside a Filters menu) doesn't dismiss the parent when picked. The
   *  host panel must then not clip (`overflow: visible`). Default true (portaled). */
  portal?: boolean;
  /** Exit-animation duration; must match the panel's CSS transition. */
  exitMs?: number;
}

export function Dropdown({
  trigger,
  children,
  label,
  side,
  align,
  matchWidth = false,
  panelClassName,
  menuSize,
  tickSide = "left",
  portal = true,
  exitMs = 120,
}: DropdownProps) {
  const [open, setOpen] = useState(false);
  const { mounted, state } = usePresence(open, exitMs);
  const wrapRef = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const triggerSize = "size" in trigger ? trigger.size : undefined;
  const rowSize = menuSize ?? triggerSize;
  const float = useFloating({ open, side, align, matchWidth });

  // The parent menu, if this Dropdown is rendered INSIDE another Dropdown's panel (a nested
  // sub-filter). Read from context so a nested menu can register its portaled panel with its ancestors.
  const parent = useContext(DropdownContext);

  // Where the panel portals to. Nearest match wins:
  //  - an `aria-modal` dialog the trigger sits in. That attribute tells assistive
  //    technology to ignore everything OUTSIDE the dialog, so a panel portaled past
  //    it renders on screen and reaches no screen reader at all — every option in a
  //    select opened inside a modal was a dead end. The panel is `position: fixed`,
  //    and floating-ui resolves its offset against whatever containing block it
  //    lands in, so moving it inside costs nothing geometrically.
  //  - otherwise the themed page root — the nearest element carrying the active
  //    light/dark scheme (`[data-alkera-color-scheme]`) or the IDE surface
  //    (`[data-alkera-ide]`), so it resolves the tokens + active scheme in both the
  //    web app and the extension webview, escaping any scroll-clipping ancestor.
  // Falls back to body.
  const portalTarget =
    (mounted
      ? (wrapRef.current?.closest(
          '[role="dialog"][aria-modal="true"], [data-alkera-color-scheme], [data-alkera-ide]',
        ) as HTMLElement | null)
      : null) ?? document.body;

  const close = useCallback(() => setOpen(false), []);
  // "keyboard" when the menu was opened from the keyboard (focus then moves into the
  // items, per the WAI menu-button pattern); null after a pointer open (focus stays on
  // the trigger; the first ArrowDown moves in).
  const openedByKey = useRef<"down" | "up" | null>(null);

  const focusTrigger = useCallback(() => {
    wrapRef.current?.querySelector<HTMLElement>('[aria-haspopup="menu"]')?.focus();
  }, []);

  const menuItems = useCallback((): HTMLElement[] => {
    if (!panelRef.current) return [];
    return [...panelRef.current.querySelectorAll<HTMLElement>('[role^="menuitem"]:not(:disabled)')];
  }, []);

  /** Roving focus for the menu: arrows wrap, Home/End jump, Tab closes and hands
   *  focus back to the trigger so the tab order continues from where the menu grew. */
  const handleMenuKeys = useCallback(
    (e: ReactKeyboardEvent) => {
      if (!open) return;
      // The portaled panel's events bubble through the REACT tree to the wrap as well —
      // without this guard every arrow press would rove twice (panel, then wrap).
      if (e.defaultPrevented) return;
      if (e.key === "Tab") {
        close();
        focusTrigger();
        return;
      }
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
      const items = menuItems();
      if (items.length === 0) return;
      e.preventDefault();
      const current = items.indexOf(document.activeElement as HTMLElement);
      const next =
        e.key === "Home" ? 0
        : e.key === "End" ? items.length - 1
        : e.key === "ArrowDown" ? (current < 0 ? 0 : (current + 1) % items.length)
        : current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length;
      items[next]?.focus();
    },
    [open, close, focusTrigger, menuItems],
  );
  // Panels of nested sub-dropdowns (registered through context). An outside press that lands inside
  // one of them must be spared — otherwise picking a value in a portaled sub-menu, which lives outside
  // this panel in the DOM, would read as an outside press and collapse this (consolidated) menu.
  const descendantPanels = useRef<Set<HTMLElement>>(new Set());
  const registerDescendantPanel = useCallback(
    (el: HTMLElement): (() => void) => {
      descendantPanels.current.add(el);
      // Bubble up so a grandparent menu spares the press too (a sub-sub-dropdown).
      const detachFromParent = parent?.registerDescendantPanel(el);
      return () => {
        descendantPanels.current.delete(el);
        detachFromParent?.();
      };
    },
    [parent],
  );

  // Dismiss watches the wrapper (trigger) AND the portaled panel (which lives outside the wrapper),
  // AND any registered nested sub-dropdown panel — an outside press must spare a click inside any of them.
  const panelRef = useRef<HTMLDivElement>(null);
  useDismiss(
    wrapRef,
    open,
    (target) => {
      if (!target) return void close();
      if (panelRef.current?.contains(target)) return;
      for (const p of descendantPanels.current) if (p.contains(target)) return;
      close();
    },
    // Escape is registered below on the shared overlay stack (with focus return);
    // useDismiss keeps only the outside-press half here.
    { escape: false },
  );
  useEffect(() => {
    if (!open) return;
    return pushEsc(() => {
      close();
      focusTrigger();
    });
  }, [open, close, focusTrigger]);

  // Register THIS panel with the parent chain while it's mounted, so ancestor menus spare presses in it.
  const [panelEl, setPanelEl] = useState<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!panelEl || !parent) return;
    return parent.registerDescendantPanel(panelEl);
  }, [panelEl, parent]);

  // The panel's own closer returns focus; the outside-press closer must NOT, or a click
  // elsewhere on the page would be yanked back to the trigger it just left.
  const closeFromPanel = useCallback(() => {
    setOpen(false);
    focusTrigger();
  }, [focusTrigger]);

  const ctx = useMemo<DropdownContextValue>(
    () => ({ close: closeFromPanel, tickSide, registerDescendantPanel }),
    [closeFromPanel, tickSide, registerDescendantPanel],
  );

  useEffect(() => {
    if (!open || !panelEl || !openedByKey.current) return;
    const items = menuItems();
    (openedByKey.current === "up" ? items[items.length - 1] : items[0])?.focus();
    openedByKey.current = null;
  }, [open, panelEl, menuItems]);

  const setPanel = useCallback(
    (el: HTMLDivElement | null) => {
      panelRef.current = el;
      setPanelEl(el);
      float.setFloating(el);
    },
    [float],
  );

  const panelNode = (
    <DropdownContext.Provider value={ctx}>
      <div
        ref={setPanel}
        id={panelId}
        className={cx("alk-floating-surface", "alk-dropdown-panel", panelClassName)}
        style={float.floatingStyles}
        data-side={float.side}
        data-state={state}
        data-size={rowSize}
        role="menu"
        aria-label={label}
        onKeyDown={handleMenuKeys}
      >
        {children}
      </div>
    </DropdownContext.Provider>
  );

  return (
    <div className="alk-dropdown" ref={wrapRef} onKeyDown={handleMenuKeys}>
      {renderTrigger(trigger, {
        ref: float.setReference,
        "aria-haspopup": "menu",
        "aria-expanded": open,
        "aria-controls": panelId,
        "data-open": open || undefined,
        onClick: () => setOpen((v) => !v),
        onKeyDown: (e: ReactKeyboardEvent) => {
          if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
            e.preventDefault();
            openedByKey.current = e.key === "ArrowUp" ? "up" : "down";
            setOpen(true);
          } else if (!open && (e.key === "Enter" || e.key === " ")) {
            // the native click fires next; remember it was a keyboard open
            openedByKey.current = "down";
          }
        },
      })}
      {mounted ? (portal ? createPortal(panelNode, portalTarget) : panelNode) : null}
    </div>
  );
}

export interface DropdownItemProps {
  /** Single-select row marks itself checked and shows a tick when on. */
  selected?: boolean;
  /** Glyph shown when the row isn't selected (the tick replaces it when it is). */
  icon?: ReactNode;
  /** Trailing slot — a demoted reading (a count, a provider family). The caller owns its type
   *  treatment. */
  trailing?: ReactNode;
  children: ReactNode;
  onSelect: () => void;
  /** Close the dropdown after selecting (default true; turn off for a multi-select group). */
  closeOnSelect?: boolean;
  /** A destructive command (Delete, …) — the label, its mark, and its hover take the danger ink. */
  danger?: boolean;
  /** An unavailable command — dimmed and inert, kept in the menu so the set reads whole and the
   *  reason it's unavailable stays discoverable (never silently dropped). Doesn't fire `onSelect`. */
  disabled?: boolean;
}

/** A standard menu row: tick/glyph · label · optional trailing reading.
 *  A row given a `selected` boolean is a single-select option (`menuitemradio` + `aria-checked`);
 *  a row with no `selected` is a command (`menuitem`, e.g. "Clear filters"), so a screen reader
 *  announces an action, not an unchecked radio.
 *
 *  The tick rides the menu's `tickSide`. `"left"` (or any row carrying a leading `icon`) keeps the
 *  leading mark column; `"right"` flushes the row left and trails the tick at the row's end, shown
 *  only when selected — so an icon-less menu isn't indented by an empty mark slot. */
export function DropdownItem({
  selected,
  icon,
  trailing,
  children,
  onSelect,
  closeOnSelect = true,
  danger = false,
  disabled = false,
}: DropdownItemProps) {
  const { close, tickSide } = useDropdown();
  const isOption = selected !== undefined;
  // A leading icon needs its column, so it pins the tick left even when the menu trails it.
  const tickRight = tickSide === "right" && icon == null;
  return (
    <button
      type="button"
      role={isOption ? "menuitemradio" : "menuitem"}
      aria-checked={isOption ? selected : undefined}
      aria-disabled={disabled || undefined}
      disabled={disabled}
      className="alk-dropdown-item"
      data-on={selected || undefined}
      data-danger={danger || undefined}
      data-disabled={disabled || undefined}
      onClick={() => {
        if (disabled) return;
        onSelect();
        if (closeOnSelect) close();
      }}
    >
      {!tickRight ? (
        <span className="alk-option-mark alk-dropdown-item__mark" aria-hidden="true">
          {selected ? <CheckIcon size={14} /> : icon}
        </span>
      ) : null}
      <span className="alk-dropdown-item__label">{children}</span>
      {trailing != null ? <span className="alk-dropdown-item__trail">{trailing}</span> : null}
      {tickRight && selected ? (
        <span className="alk-option-mark alk-dropdown-item__mark alk-dropdown-item__mark--trail" aria-hidden="true">
          <CheckIcon size={14} />
        </span>
      ) : null}
    </button>
  );
}

/** A small-caps section header inside the panel — groups a run of items. */
export function DropdownGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="alk-dropdown-group" role="group" aria-label={label}>
      <span className="alk-dropdown-group__eyebrow">{label}</span>
      {children}
    </div>
  );
}

/** A hairline divider between groups. */
export function DropdownDivider() {
  return <div className="alk-dropdown-divider" role="separator" aria-hidden="true" />;
}
