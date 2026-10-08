import type { ComponentPropsWithRef, CSSProperties } from "react";

import { cx } from "../../cx";
import type { ControlSize } from "../../sizes";

type SpanProps = Omit<ComponentPropsWithRef<"span">, "children" | "role" | "title" | "aria-label" | "aria-hidden">;

export interface AvatarProps extends SpanProps {
  /** What the disc shows where there is no picture. The caller computes them. */
  initials: string;
  /**
   * Who the disc is, in full. Two letters are not a name to anybody, so a labelled disc says the
   * name on hover AND to a reader who cannot hover, from the one string. Omit it where the name is
   * already drawn beside the disc.
   */
  label?: string;
  /** The person's picture, drawn in place of the initials. It is decoration: the disc carries the name. */
  picture?: string | null;
  /** The person's own hue (0–359), the same one they wear elsewhere (a caret, a cursor): the disc is
   *  painted in it rather than in the brand tint, so the same person reads as the same colour. */
  hue?: number;
  /** Disc size — `sm` 24 / `md` 32 (default) / `lg` 40. The initials scale with it. */
  size?: ControlSize;
  className?: string;
}

/**
 * Avatar — a person's disc: their picture, else their initials.
 *
 * One component for every place a person shows as a small disc (the account button, a roster row, a
 * people picker, the faces of who else is here), so the size, shape, and tint stay identical instead
 * of being re-declared per surface. Decorative by default (the name sits beside it); pass a `label`
 * only when the disc stands alone, which makes it an `img` with that accessible name and gives it a
 * native tooltip carrying the same text — unless the disc is the trigger of a shared `Tooltip`
 * (`data-tip`), which already says it. Any other span attribute (a ref, `tabIndex`, a data
 * attribute, the Tooltip's trigger props) lands on the disc.
 */
export function Avatar({ initials, label, picture, hue, size = "md", className, style, ...rest }: AvatarProps) {
  // A blank label names nobody: rather than offer an empty tooltip, the disc stays decoration.
  const named = (label ?? "").trim() || undefined;
  const annotated = "data-tip" in rest;
  const hued = hue === undefined ? style : ({ ...style, "--alk-avatar-hue": hue } as CSSProperties);
  return (
    <span
      {...rest}
      className={cx("alk-avatar", className)}
      data-size={size !== "md" ? size : undefined}
      data-hued={hue === undefined ? undefined : ""}
      style={hued}
      role={named ? "img" : undefined}
      title={named && !annotated ? named : undefined}
      aria-label={named}
      aria-hidden={named ? undefined : true}
    >
      {picture ? <img className="alk-avatar__picture" src={picture} alt="" /> : initials}
    </span>
  );
}
