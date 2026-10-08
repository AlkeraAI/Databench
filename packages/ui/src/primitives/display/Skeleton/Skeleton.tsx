import { forwardRef, type CSSProperties, type HTMLAttributes } from "react";

import { cx } from "../../cx";

export interface SkeletonProps extends HTMLAttributes<HTMLDivElement> {
  width?: number | string;
  height?: number | string;
  /** Round the placeholder fully — for avatars and dots. */
  circle?: boolean;
}

const dimension = (value: number | string | undefined): string | undefined =>
  typeof value === "number" ? `${value}px` : value;

/** A loading placeholder that pulses between two surface steps. Mirror the real
 *  layout's shape so the resolve doesn't jump. */
export const Skeleton = forwardRef<HTMLDivElement, SkeletonProps>(function Skeleton(
  { width, height, circle = false, className, style, ...rest },
  ref,
) {
  const sized: CSSProperties = {
    width: dimension(width),
    height: dimension(height),
    ...style,
  };
  return (
    <div
      ref={ref}
      className={cx("alk-skeleton", circle && "alk-skeleton--circle", className)}
      style={sized}
      {...rest}
    />
  );
});
