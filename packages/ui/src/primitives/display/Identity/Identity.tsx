import type { ReactNode } from "react";

import { cx } from "../../cx";
import { Avatar } from "../Avatar";
import type { ControlSize } from "../../sizes";

export interface IdentityProps {
  /** Monogram initials for the leading Avatar (a person). Ignored when `leading` is given. */
  initials?: string;
  /** A custom leading node (a team's IconChip, an org glyph) in place of the person Avatar. */
  leading?: ReactNode;
  /** Avatar disc size (default md). */
  size?: ControlSize;
  /** The name line — the primary identifier. */
  name: ReactNode;
  /** An inline node right after the name — a "You" tag, a status dot. */
  nameTrailing?: ReactNode;
  /** The demoted second line — an email, a role. */
  secondary?: ReactNode;
  /** A node pinned to the row's trailing edge — a chevron, a check, a small action. */
  trailing?: ReactNode;
  className?: string;
}

/**
 * Identity — an avatar beside a name over a quiet second line.
 *
 * One component for every place a person (or an org) shows as a monogram + name + email/role: a
 * roster row, a people picker, the account button in the shell. The name rides the medium-weight
 * reading, the second line the quiet caption; both truncate. Pure presentation — the caller computes
 * the initials and supplies the copy + any trailing control.
 */
export function Identity({ initials, leading, size, name, nameTrailing, secondary, trailing, className }: IdentityProps) {
  return (
    <div className={cx("alk-identity", className)}>
      {leading ?? (initials != null ? <Avatar initials={initials} size={size} /> : null)}
      <span className="alk-identity__text">
        <span className="alk-identity__name">
          {name}
          {nameTrailing}
        </span>
        {secondary != null ? <span className="alk-identity__secondary">{secondary}</span> : null}
      </span>
      {trailing}
    </div>
  );
}
