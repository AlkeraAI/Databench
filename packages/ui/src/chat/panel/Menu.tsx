// The chat's popover menu: one trigger, one frame, one dismissal, one row
// style. The chrome's settings and quick-links menus are this, and so are the
// home list's per-chat actions -- a menu that framed or dismissed itself
// differently in one place would read as a different kind of control.
//
// A row that destroys something carries the danger ink from the moment the menu
// opens, so the reader knows what the row costs before the pointer reaches it.

import { useEffect, useRef, useState, type ReactElement, type ReactNode } from "react";

import { Text, useDismiss } from "../sharedUi";

import { useRovingFocus } from "../hooks";
import "./menu.css";

export interface PopMenuProps {
  /** Names the trigger for the pointer and the screen reader. */
  label: string;
  /** The trigger's face: an action's own icon, or `OverflowGlyph` where the menu
   *  is what other controls fold into. */
  glyph: ReactNode;
  /** Extra class on the positioning frame, so a surface can place or fold the
   *  whole menu at a width this package cannot know. */
  className?: string;
  /** Extra class on the trigger, for a surface that skins its own. The trigger
   *  wears the chrome's icon button either way. */
  triggerClassName?: string;
  /** What the rows act for, above them. A caption, never a row. */
  header?: ReactNode;
  /** Rendered with `close`, so a row can act and dismiss in one press. */
  children: (close: () => void) => ReactNode;
}

export function PopMenu({
  label,
  glyph,
  className,
  triggerClassName,
  header,
  children,
}: PopMenuProps): ReactElement {
  const [open, setOpen] = useState(false);
  const frameRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useDismiss(frameRef, open, () => setOpen(false));
  const onMenuKey = useRovingFocus(menuRef, "menuitem", () => {
    setOpen(false);
    buttonRef.current?.focus();
  });

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
  }, [open]);

  return (
    <div className={className ? `chat-menu ${className}` : "chat-menu"} ref={frameRef}>
      <button
        type="button"
        ref={buttonRef}
        className={triggerClassName ? `chat-icon ${triggerClassName}` : "chat-icon"}
        title={label}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {glyph}
      </button>
      {open ? (
        <div className="chat-menu__panel" role="menu" ref={menuRef} onKeyDown={onMenuKey}>
          {header ? <div className="chat-menu__head">{header}</div> : null}
          {children(() => setOpen(false))}
        </div>
      ) : null}
    </div>
  );
}

export interface MenuRowProps {
  /** Leads the row. A menu's rows either all carry one or none do. */
  icon?: ReactNode;
  label: string;
  /** The action destroys something, so the row reads in danger ink at rest. */
  danger?: boolean;
  /** Closes the row, e.g. a count badge. */
  trailing?: ReactNode;
  /** Marks the row for a caller that keys off the action rather than its
   *  wording, so rewording a row is never a breaking change. */
  id?: string;
  onSelect: () => void;
}

export function MenuRow({ icon, label, danger, trailing, id, onSelect }: MenuRowProps): ReactElement {
  return (
    <button
      type="button"
      className="chat-menu__row"
      role="menuitem"
      data-menu-id={id}
      data-danger={danger ? "" : undefined}
      onClick={onSelect}
    >
      {icon ? <span className="chat-menu__icon">{icon}</span> : null}
      <Text className="chat-menu__label" tooltip="truncate">
        {label}
      </Text>
      {trailing}
    </button>
  );
}

/** Three dots on the chat's 14 grid: the face of a menu that other controls
 *  fold into. */
export function OverflowGlyph(): ReactElement {
  return (
    <svg viewBox="0 0 14 14" aria-hidden="true" width="14" height="14">
      <circle cx="3" cy="7" r="1.1" fill="currentColor" />
      <circle cx="7" cy="7" r="1.1" fill="currentColor" />
      <circle cx="11" cy="7" r="1.1" fill="currentColor" />
    </svg>
  );
}
