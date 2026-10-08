// The admin registry's shared cell primitives every register reuses (a mono id, a USD amount, a
// linked name), so a column of them reconciles by eye. Titled sections use the @alkera/ui Card.

import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { Anchor, cx } from "@alkera/ui";

import shared from "../admin.module.css";

// ---- cell primitives ------------------------------------------------------

/** A measured reading — mono, tabular, right-set for a numeric column. The unit of
 *  record for ids, USD, counts, and timestamps. */
export function Reading({ children, align = "start", muted, className }: { children: ReactNode; align?: "start" | "end"; muted?: boolean; className?: string }) {
  return <span className={cx("alk-num", shared.reading, align === "end" && shared.readingEnd, muted && "alk-faint", className)}>{children}</span>;
}

/** A short mono id (the leading 8 chars), the recognisable handle for a row. */
export function IdCell({ id, full }: { id: string; full?: boolean }) {
  return (
    <span className="alk-num alk-caption" title={id}>
      {full ? id : id.slice(0, 8)}
    </span>
  );
}

/** A router-linked name cell — a register row's primary anchor into its detail. Clamps to one line
 *  (the ellipsis needs the anchor to be a block box) so a long name never wraps a table row to two
 *  lines; the stacked card lets its title wrap.
 *
 *  A profile that hasn't been completed carries the empty-string name sentinel, so the cell falls
 *  back — to `fallback` (the row's email, where it has one), then to the short id from its own
 *  href. The anchor is the row's only doorway into the detail: nameless, it is announced as a bare
 *  "link" and is a zero-size tab stop nobody can see. */
export function LinkCell({ to, children, fallback }: { to: string; children: ReactNode; fallback?: string }) {
  return (
    <NavLink to={to} className={cx("alk-link", "alk-truncate")} style={{ display: "block" }}>
      {isBlank(children) ? (fallback?.trim() || shortIdOf(to)) : children}
    </NavLink>
  );
}

/** A name the row does not actually have: absent, or present but empty / whitespace. */
function isBlank(name: ReactNode): boolean {
  return name == null || name === false || (typeof name === "string" && name.trim() === "");
}

/** The leading 8 chars of the id the href ends in — the same handle `IdCell` shows. */
function shortIdOf(to: string): string {
  return (to.split("/").filter(Boolean).pop() ?? to).slice(0, 8);
}

/** An inline text-link. With `href` it's a real anchor; otherwise a `<button>` dressed by alk-link
 *  (the base owns the button reset) for an in-flow action — a table row that opens a drawer, an
 *  inline "Set" / "Cancel" editor. */
export function TextLink({ href, onClick, className, children }: { href?: string; onClick?: () => void; className?: string; children: ReactNode }) {
  if (href) return <Anchor href={href} className={className}>{children}</Anchor>;
  return (
    <button type="button" className={cx("alk-link", className)} onClick={onClick}>
      {children}
    </button>
  );
}

/** The muted em-dash for an absent reading, so an empty cell reads as "none" not "broken". */
export function Dash() {
  return <span style={{ color: "var(--alkTertiaryText)" }} aria-label="none">—</span>;
}
