import { type CSSProperties, type HTMLAttributes } from "react";

import { cx } from "../../cx";

export type IconChipTone = "brand" | "neutral" | "surface";
export type IconChipSize = "sm" | "md" | "lg";

/** A numeric size's glyph rides ~57% of the box edge — the ratio the sm/md/lg pairs already use. */
const GLYPH_RATIO = 0.57;

export interface IconChipProps extends HTMLAttributes<HTMLSpanElement> {
  /** Tint. `brand` is a brand-subtle wash with brand ink (default); `neutral` is a quiet grey wash;
   *  `surface` is a card-coloured tile with a hairline (the logo-on-card treatment). */
  tone?: IconChipTone;
  /** Square edge + glyph, as a pair: `sm` 24/14 · `md` 28/16 · `lg` 34/20. Pass a NUMBER for a
   *  custom box edge in px (an off-scale media cell); the glyph scales with it. */
  size?: IconChipSize | number;
}

/**
 * IconChip — a small, non-interactive tinted square holding a glyph.
 *
 * The decorative badge in a card / section header or a media cell — the still sibling of an
 * icon-only {@link Button} (no cursor, hover, or focus). It is `aria-hidden` by default because it
 * accompanies a text label that already carries the meaning. The chip sizes the glyph itself (via
 * CSS, overriding the icon's own width/height), so a caller just drops an icon in and picks a size.
 */
export function IconChip({ tone = "brand", size = "md", className, style, children, ...rest }: IconChipProps) {
  const custom = typeof size === "number";
  const sizeStyle: CSSProperties | undefined = custom
    ? ({
        ["--alk-iconchip-box" as string]: `${size}px`,
        ["--alk-iconchip-glyph" as string]: `${Math.round(size * GLYPH_RATIO)}px`,
      } as CSSProperties)
    : undefined;
  return (
    <span
      aria-hidden="true"
      {...rest}
      style={sizeStyle ? { ...sizeStyle, ...style } : style}
      className={cx("alk-iconchip", className)}
      data-tone={tone !== "brand" ? tone : undefined}
      data-size={!custom && size !== "md" ? size : undefined}
    >
      {children}
    </span>
  );
}
