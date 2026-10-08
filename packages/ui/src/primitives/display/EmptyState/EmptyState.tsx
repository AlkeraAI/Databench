import type { ReactNode } from "react";

import { cx } from "../../cx";

// A centred "state plate" for the surfaces a data view resolves into when there is nothing to show
// — an empty seat, or a fetch that failed. The shape is a mark, a Newsreader heading, a
// plain-language body, an optional raw-detail disclosure (for an error), and one next action. Shared
// so every page's empty / fail-to-load surface reads the same instead of each rolling its own.
//
// Pure presentation: the caller owns the mark (a bench-line SVG), the copy, and the action node
// (a Button / link wired to retry or navigate). `tone="alert"` paints the mark in the danger color
// and defaults the role to "alert"; "neutral" (the default) is the empty-state treatment.

export interface EmptyStateProps {
  /** A mark / illustration — a bench-line SVG. Inherits the tone color via currentColor. */
  icon?: ReactNode;
  title: ReactNode;
  body?: ReactNode;
  /** The one next action — a Button or link. */
  action?: ReactNode;
  /** Raw technical detail (an error message) tucked under a disclosure, so it never headlines. A
   *  plain string — it renders inside a <pre>, so arbitrary nodes don't belong here. */
  details?: string;
  detailsLabel?: string;
  /** Mark tone: neutral (empty) or alert (a failure). */
  tone?: "neutral" | "alert";
  /** Weight of the plate on a three-step scale.
   *  - `lg` (default) — the full-height plate that REPLACES a whole view (a failed load, an empty
   *    seat): a tall min-height, the large mark, a 3xl title, and Lg body.
   *  - `md` — the compact plate that sits INSIDE a card / panel body (no min-height, smaller mark +
   *    type) — a no-matches state below a populated toolbar.
   *  - `sm` — the tightest step for a small inline note: the smallest mark and title/body/gaps. */
  size?: "sm" | "md" | "lg";
  /** ARIA role — defaults to "alert" for the alert tone, else "status". */
  role?: "status" | "alert";
  className?: string;
}

export function EmptyState({
  icon,
  title,
  body,
  action,
  details,
  detailsLabel = "Details",
  tone = "neutral",
  size = "lg",
  role,
  className,
}: EmptyStateProps) {
  return (
    <div
      // lg is the default, carried by the base rule — only sm/md set a size attribute.
      className={cx("alk-emptystate", className)}
      data-size={size !== "lg" ? size : undefined}
      data-tone={tone === "alert" ? "alert" : undefined}
      data-measure-surface="product"
      role={role ?? (tone === "alert" ? "alert" : "status")}
    >
      {icon != null ? <div className="alk-emptystate__mark">{icon}</div> : null}
      <h2 className="alk-emptystate__title">{title}</h2>
      {body != null ? <p className="alk-emptystate__body">{body}</p> : null}
      {action != null ? <div className="alk-emptystate__action">{action}</div> : null}
      {details != null ? (
        <details className="alk-emptystate__details">
          <summary>{detailsLabel}</summary>
          <pre className="alk-emptystate__raw">{details}</pre>
        </details>
      ) : null}
    </div>
  );
}
