import { createElement, forwardRef, useId, type HTMLAttributes, type ReactNode } from "react";

import { cx } from "../../cx";
import { IconChip, type IconChipTone } from "../../controls/IconChip";

export type CardSize = "sm" | "md" | "lg";

export interface CardProps extends Omit<HTMLAttributes<HTMLDivElement>, "title"> {
  /** Header treatment. `section` (default) is an IconChip + serif title; `panel` is the lighter
   *  labelled-panel look — a plain brand glyph + a small-caps eyebrow title. */
  variant?: "section" | "panel";
  /** The one dial: scales the padding, the title type, and the header IconChip together. */
  size?: CardSize;
  /** Header glyph — rendered inside an IconChip sized + toned to match the card. Omit for no chip. */
  icon?: ReactNode;
  /** IconChip tone for `icon` (default brand) — tints the header chip. */
  tone?: IconChipTone;
  /** A small-caps eyebrow above the title — a date range or section label (the scholar's plate
   *  label). Stacks over the title; the header chip centres against the pair. */
  eyebrow?: ReactNode;
  /** Header title. Omit (with no icon + no actions) for a bare surface. */
  title?: ReactNode;
  /** A subtitle under the title (a date range, a "last 30 days" reading). Stacks below the title
   *  in the heading. */
  sub?: ReactNode;
  /** Heading level for the title element, so the document outline stays correct (default 3). */
  headingLevel?: 2 | 3 | 4 | 5 | 6;
  /** Header-trailing controls (a menu, a link, a button), right-aligned and vertically centred. */
  actions?: ReactNode;
  /** Render the header ALONE — no card surface (no border / background / padding), just the
   *  title · sub · actions row, for a header inside a page panel. */
  bare?: boolean;
  /** Rule a hairline between consecutive body children (a settings / list section). */
  divided?: boolean;
  /** Extra class on the body wrapper, for a page-specific body layout (a tinted descent card, etc.). */
  bodyClassName?: string;
  /** `none` renders a flush body (ledger rows edge-to-edge) — zero body padding. */
  bodyPadding?: "none";
  /** Vertically center the body's content (a lone empty state), so it doesn't pin to the top when the
   *  card is stretched taller than its content (an equal-height row). */
  bodyAlign?: "center";
  /** Rule a hairline under the header, parting it from the body (a settings / config section). */
  headerDivider?: boolean;
  /** Render the surface as a labelled `<section>` region — a navigable AT landmark — instead of a
   *  plain `<div>`; the title becomes the region's accessible name. Needs a `title`. */
  region?: boolean;
  /** A footer slot after the body — pinned to the card's bottom, parted by a hairline. */
  footer?: ReactNode;
  children?: ReactNode;
}

/**
 * Card — the titled surface.
 *
 * A bordered, rounded `--alkCardBg` panel with an optional header (an IconChip + a title + trailing
 * actions) over a body. `size` is the one dial: it scales the padding, the title type, and the chip
 * together, and the header keeps the chip optically centred with the title. Omit the header props
 * for a bare surface; pass `divided` to rule a hairline between body rows. Extra props (className,
 * data-* hooks, onClick) forward to the root.
 */
export const Card = forwardRef<HTMLDivElement, CardProps>(function Card(
  {
    variant = "section",
    size = "md",
    icon,
    tone = "brand",
    eyebrow,
    title,
    sub,
    headingLevel = 3,
    actions,
    bare = false,
    divided,
    bodyClassName,
    bodyPadding,
    bodyAlign,
    headerDivider,
    region = false,
    footer,
    className,
    children,
    ...rest
  },
  ref,
) {
  const titleId = useId();
  const hasHeader = icon != null || title != null || actions != null || eyebrow != null || sub != null;
  const titleTag = `h${headingLevel}` as `h${2 | 3 | 4 | 5 | 6}`;
  // When a region, the title labels the section — give it an id and point the root's aria-labelledby
  // at it (so the surface becomes a landmark named by its own heading).
  const labelledBy = region && title != null ? titleId : undefined;
  const Root = region ? "section" : "div";
  return (
    <Root
      ref={ref}
      className={cx(
        "alk-card",
        `alk-card--${size}`,
        variant === "panel" && "alk-card--panel",
        headerDivider && "alk-card--ruled",
        bare && "alk-card--bare",
        className,
      )}
      aria-labelledby={labelledBy}
      {...rest}
    >
      {hasHeader ? (
        <header className="alk-card__head">
          {/* A single-line title centres against the chip; a title with an eyebrow/subtitle keeps the
              chip on its first line (flex-start), so the chip never drifts to the block's middle. */}
          <div className={cx("alk-card__id", eyebrow == null && sub == null && "alk-card__id--center")}>
            {icon != null ? (
              variant === "panel" ? (
                <span className="alk-card__glyph" aria-hidden="true">
                  {icon}
                </span>
              ) : (
                <IconChip size={size} tone={tone}>
                  {icon}
                </IconChip>
              )
            ) : null}
            {eyebrow != null || sub != null ? (
              <div className="alk-card__heading">
                {eyebrow != null ? <span className="alk-card__eyebrow">{eyebrow}</span> : null}
                {title != null
                  ? createElement(titleTag, { className: "alk-card__title", id: labelledBy }, title)
                  : null}
                {/* A div, not a p: `sub` holds arbitrary nodes (a subtitle string, but also a Skeleton
                    or a reading during loading), and a <p> may not contain a block element. */}
                {sub != null ? <div className="alk-card__sub">{sub}</div> : null}
              </div>
            ) : title != null ? (
              createElement(titleTag, { className: "alk-card__title", id: labelledBy }, title)
            ) : null}
          </div>
          {actions != null ? <div className="alk-card__actions">{actions}</div> : null}
        </header>
      ) : null}
      {children != null ? (
        <div
          className={cx("alk-card__body", divided && "alk-card__body--divided", bodyClassName)}
          data-padding={bodyPadding}
          data-align={bodyAlign}
        >
          {children}
        </div>
      ) : null}
      {footer != null ? <div className="alk-card__foot">{footer}</div> : null}
    </Root>
  );
});
