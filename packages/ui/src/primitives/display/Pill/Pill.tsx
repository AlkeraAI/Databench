import { forwardRef, type ButtonHTMLAttributes, type HTMLAttributes, type ReactNode, type Ref } from "react";

import { cx } from "../../cx";

export type PillTone =
  | "brand"
  | "neutral"
  | "success"
  | "danger"
  | "warning"
  | "info"
  | "catNeutral"
  | "cat1"
  | "cat2"
  | "cat3"
  | "cat4"
  | "cat5"
  | "cat6"
  | "cat7"
  | "cat8";

interface PillBase {
  tone?: PillTone;
  /** `soft` (default) is the low-emphasis wash — the tone's subtle ground with readable tone ink;
   *  `solid` is the full-strength fill (the tone's solid colour under on-accent ink) for the one chip
   *  that must lead; `outline` is a bordered choice-chip on the surface (transparent fill) — the look
   *  for a filter pill or a navigable neighbour; `plain` is a chip-less inline mark (no fill, no
   *  border, just the toned word + glyph) for a status that sits inside a line of text without
   *  growing the line; `code` is an inline mono code mark — renders a `<code>` element, tone ignored. */
  variant?: "soft" | "solid" | "outline" | "plain" | "code";
  /** Render as a compact, min-width, tabular-nums numeric counter — a count
   *  riding a button, nav item, or segment. Pair with a tone; `active` isn't needed (use the brand tone
   *  for the selected-segment brightening). */
  numeric?: boolean;
  /** `sm` (default) is the compact inline chip; `md` is a fixed 28px badge; `lg` matches the action-
   *  button height (38px), for a status that occupies a row's control slot in place of a button (so
   *  the slot keeps one height). */
  size?: "sm" | "md" | "lg";
  /** Corner shape: `pill` (fully round, default) or `rect` (the rounded-rectangular badge — a trust
   *  grade, a role chip). Independent of `size`. */
  shape?: "pill" | "rect";
  /** Render the leading status dot. Its colour follows the tone; override with the `--alk-pill-dot`
   *  custom property (any colour OR a gradient). */
  dot?: boolean;
  /** A leading glyph slot, distinct from the boolean `dot` (a colored status circle) — a role chip's
   *  shield, a status check. If both `icon` and `dot` are given, `icon` wins. */
  icon?: ReactNode;
}

export interface PillProps extends PillBase, HTMLAttributes<HTMLSpanElement> {
  interactive?: false;
  /** A toggle ring is only meaningful on an interactive pill — forbidden on the static span. */
  selected?: undefined;
}

export interface PillButtonProps extends PillBase, Omit<ButtonHTMLAttributes<HTMLButtonElement>, "type"> {
  /** Render the pill as a real `<button>` — adds hover/active/focus affordances and an `onClick`. */
  interactive: true;
  /** A choice pill's toggle state: sets `aria-pressed` and the selected treatment. Omit for a plain
   *  navigable pill (no pressed semantics). */
  selected?: boolean;
}

/** A compact status/category label. Every tone renders the same soft treatment (the tone's subtle
 *  ground + readable tone ink), so a family of categories reads as one palette; `variant="solid"` is
 *  the full-strength fill for the one chip that must lead. Pass `interactive` to render it as a
 *  clickable `<button>` (a filter chip, a navigable neighbour), `variant="outline"` for the bordered
 *  choice-chip look, and `selected` for a toggle. */
export const Pill = forwardRef<HTMLSpanElement | HTMLButtonElement, PillProps | PillButtonProps>(function Pill(
  { tone = "neutral", variant = "soft", size = "sm", shape = "pill", dot = false, numeric = false, icon, interactive, selected, className, children, ...rest },
  ref,
) {
  const cls = cx(
    "alk-pill",
    `alk-pill--${variant}`,
    shape === "rect" && "alk-pill--rect",
    size !== "sm" && `alk-pill--${size}`,
    numeric && "alk-pill--numeric",
    interactive && "alk-pill--interactive",
    className,
  );
  const body = (
    <>
      {icon ? (
        <span className="alk-pill__icon" aria-hidden="true">
          {icon}
        </span>
      ) : dot ? (
        <span className="alk-pill__dot" />
      ) : null}
      {children}
    </>
  );

  if (interactive) {
    return (
      <button ref={ref as Ref<HTMLButtonElement>} type="button" className={cls} data-tone={tone} aria-pressed={selected} {...(rest as ButtonHTMLAttributes<HTMLButtonElement>)}>
        {body}
      </button>
    );
  }
  // `code` renders a real <code> element — an inline mono mark.
  if (variant === "code") {
    return (
      <code ref={ref as unknown as Ref<HTMLElement>} className={cls} data-tone={tone} {...(rest as HTMLAttributes<HTMLElement>)}>
        {body}
      </code>
    );
  }
  return (
    <span ref={ref as Ref<HTMLSpanElement>} className={cls} data-tone={tone} {...(rest as HTMLAttributes<HTMLSpanElement>)}>
      {body}
    </span>
  );
});
