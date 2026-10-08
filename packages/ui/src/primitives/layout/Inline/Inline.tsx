import type { CSSProperties, ElementType, HTMLAttributes, ReactNode } from "react";

import { cx } from "../../cx";
import { resolveGap, type SpaceScale } from "../gap";

export interface InlineProps extends HTMLAttributes<HTMLElement> {
  /** The element to render (default `div`). */
  as?: ElementType;
  /** Gap between children — a space-scale step (0–8) or a CSS length. Default 8px. */
  gap?: SpaceScale | string;
  /** `align-items` (default `center`). */
  align?: CSSProperties["alignItems"];
  /** `justify-content`. */
  justify?: CSSProperties["justifyContent"];
  /** Wrap onto multiple lines (default `true`). */
  wrap?: boolean;
  /** Fill the remaining space in the PARENT flex container (`flex: 1`) — a cluster that takes the
   *  leftover row width. */
  grow?: boolean;
  /** Render as a BLOCK-level flex row (`display: flex`) instead of the inline-flex cluster — for a
   *  full-width row directly inside a block container (a divided Card body's settings row), where
   *  inline-flex would shrink-fit or share a line. */
  block?: boolean;
  children?: ReactNode;
}

/**
 * Inline — a horizontal flex cluster: a gapped, vertically-centred, wrapping row (an icon beside a
 * reading, a pill row, a toolbar). One `gap` prop replaces a bespoke `display:inline-flex;gap` class.
 * `align`/`justify` set the axes, `wrap={false}` keeps it on one line, `as` renders any element.
 */
export function Inline({ as, gap, align, justify, wrap, grow, block, className, style, children, ...rest }: InlineProps) {
  const Component = (as ?? "div") as ElementType;
  // ALWAYS pin the gap var, defaulted — custom properties inherit, so a nested gap-less Inline would
  // otherwise read an ancestor Inline's wider gap.
  const g = resolveGap(gap ?? 3);
  const merged = {
    ...style,
    "--alk-inline-gap": g,
    ...(align ? { alignItems: align } : {}),
    ...(justify ? { justifyContent: justify } : {}),
    ...(wrap === false ? { flexWrap: "nowrap" as const } : {}),
  } as CSSProperties;
  return (
    <Component className={cx("alk-inline", className)} data-grow={grow ? "" : undefined} data-block={block ? "" : undefined} style={merged} {...rest}>
      {children}
    </Component>
  );
}
