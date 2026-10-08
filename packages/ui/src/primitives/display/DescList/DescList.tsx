import { type HTMLAttributes, type ReactNode } from "react";

import { cx } from "../../cx";

export interface DescListProps extends HTMLAttributes<HTMLDListElement> {
  children: ReactNode;
}

export interface DescRowProps {
  /** The term in the left column (a small-caps eyebrow). */
  label: ReactNode;
  /** The definition in the right column. */
  children: ReactNode;
}

/**
 * DescList — a definition list as a `label | value` grid.
 *
 * A `<dl>` laid as ONE two-column grid: the term column hugs the longest label, the value takes the
 * rest, and every row shares the same edges — so a block of metadata (a fact's provenance, an
 * object's properties) reads as an aligned two-column ledger. Pair with {@link DescRow}; the term is
 * a small-caps eyebrow the base styles itself.
 */
export function DescList({ className, children, ...rest }: DescListProps) {
  return (
    <dl className={cx("alk-desclist", className)} {...rest}>
      {children}
    </dl>
  );
}

/** One term/definition row of a {@link DescList}. */
export function DescRow({ label, children }: DescRowProps) {
  return (
    <div className="alk-desclist__row">
      <dt className="alk-desclist__dt">{label}</dt>
      <dd className="alk-desclist__dd">{children}</dd>
    </div>
  );
}
