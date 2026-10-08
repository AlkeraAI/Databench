import { forwardRef, type AnchorHTMLAttributes } from "react";

import { cx } from "../../cx";

export type AnchorProps = AnchorHTMLAttributes<HTMLAnchorElement>;

/** The text link. Renders a plain `<a>`; for in-app navigation pass the same
 *  `alk-link` class to a router `<Link>` so both share one styled treatment. */
export const Anchor = forwardRef<HTMLAnchorElement, AnchorProps>(function Anchor({ className, ...rest }, ref) {
  return <a ref={ref} className={cx("alk-link", className)} {...rest} />;
});
