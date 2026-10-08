import type { CSSProperties, ElementType, HTMLAttributes, ReactNode } from "react";

import { cx } from "../../cx";
import { resolveGap, type SpaceScale } from "../gap";

export interface StackProps extends HTMLAttributes<HTMLElement> {
  /** The element to render (default `div`). */
  as?: ElementType;
  /** Gap between children — a space-scale step (0–8) or a CSS length. Defaults to the tight 2px cell
   *  gap; pass a step (e.g. `gap={5}`) for a spacing container. */
  gap?: SpaceScale | string;
  /** `align-items` (default `flex-start`). */
  align?: CSSProperties["alignItems"];
  /** `justify-content`. */
  justify?: CSSProperties["justifyContent"];
  /** Fill the remaining space in the PARENT flex container (`flex: 1`) — a card body that pushes the
   *  footer down, a column that takes the leftover height. */
  grow?: boolean;
  children?: ReactNode;
}

/**
 * Stack — a vertical flex layout primitive. Children stack in a column with a shared `gap`, so a page
 * composes spacing through one prop instead of a bespoke `display:flex;flex-direction:column;gap` class.
 * `align`/`justify` set the cross/main axis; `as` renders any element. Extra DOM props forward to the root.
 */
export function Stack({ as, gap, align, justify, grow, className, style, children, ...rest }: StackProps) {
  const Component = (as ?? "div") as ElementType;
  // ALWAYS pin the gap var, defaulted — a custom property inherits, so a gap-less Stack nested in a
  // `<Stack gap={7}>` page column would otherwise read the ancestor's 16px instead of the tight
  // 2px default.
  const g = resolveGap(gap ?? 0);
  const merged = {
    ...style,
    "--alk-stack-gap": g,
    ...(align ? { alignItems: align } : {}),
    ...(justify ? { justifyContent: justify } : {}),
  } as CSSProperties;
  return (
    <Component className={cx("alk-stack", className)} data-grow={grow ? "" : undefined} style={merged} {...rest}>
      {children}
    </Component>
  );
}
