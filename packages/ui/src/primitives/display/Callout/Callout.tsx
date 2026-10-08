import { type ComponentType, type ReactNode } from "react";

import { cx } from "../../cx";
import { AlertCircleIcon, AlertTriangleIcon, CheckCircleIcon, InfoIcon, type IconProps } from "../../icons";

export type CalloutTone = "brand" | "neutral" | "info" | "success" | "warning" | "danger";

export interface CalloutProps {
  /** Drives the box's background, border, and text ink — the leading icon and every child read the
   *  one toned colour. `brand` is the low-emphasis brand wash (a confirmed/verified reading);
   *  `neutral` is the quiet un-toned inset (primary-ink text on the neutral soft ground, no default
   *  glyph — a plain sub-plate, not a status); the rest mirror the status tones. */
  tone?: CalloutTone;
  /** A bolded headline over the body — turns the callout into a titled status banner (a form error,
   *  a "Signed in" confirmation). Omit it for the inline graded-plate reading. */
  title?: ReactNode;
  /** Override the tone's default leading glyph (e.g. a domain mark). Pass `null` for no icon. */
  icon?: ReactNode;
  /** Live-region politeness. Defaults from the tone: `danger` asserts (`alert`), every other tone
   *  politely announces (`status`). Pass `note` for a calm, non-announcing annotation (a decorative
   *  graded plate). */
  role?: "note" | "status" | "alert";
  className?: string;
  children?: ReactNode;
}

/** Each tone's default leading glyph. `neutral` has none — a quiet inset is a container, not a
 *  status, so it carries no mark unless the caller supplies one. */
const TONE_ICON: Record<CalloutTone, ComponentType<IconProps> | null> = {
  brand: CheckCircleIcon,
  neutral: null,
  info: InfoIcon,
  success: CheckCircleIcon,
  warning: AlertCircleIcon,
  danger: AlertTriangleIcon,
};

/**
 * Callout — a toned box that calls out one reading, from a calm inline plate to a titled status
 * banner. A callout notices; it does not demand a response the way an alert does.
 *
 * One `tone` keys the background, border, AND text ink, so the leading mark, any inline label, and
 * the note all inherit the same brand / status colour — never a grey note beside a coloured heading.
 * The icon sits LEFT of the body, pinned to the body's first line — a wrapping note flows beside the
 * glyph, never below it. The icon is the tone's default glyph (a domain mark can override it, or
 * `null` drops it). With a `title` the body reads as a two-level banner (headline + text). The
 * live-region role defaults from the tone (danger asserts, the rest politely announce); a decorative
 * plate passes `role="note"` to stay silent.
 */
export function Callout({ tone = "info", title, icon, role, className, children }: CalloutProps) {
  const Glyph = TONE_ICON[tone];
  const mark = icon === undefined ? (Glyph != null ? <Glyph size={16} /> : null) : icon;
  return (
    <div
      className={cx("alk-callout", className)}
      data-tone={tone}
      role={role ?? (tone === "danger" ? "alert" : "status")}
    >
      {mark != null ? (
        <span className="alk-callout__icon" aria-hidden="true">
          {mark}
        </span>
      ) : null}
      <div className="alk-callout__body">
        {title != null ? <p className="alk-callout__title">{title}</p> : null}
        {children != null ? <div className="alk-callout__text">{children}</div> : null}
      </div>
    </div>
  );
}
