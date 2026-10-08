import type { HTMLAttributes, ReactNode } from "react";

import { cx } from "../../cx";

export interface CollapseProps extends HTMLAttributes<HTMLDivElement> {
  /** Whether the region is expanded. Animates between states; closed, the content is `inert`. */
  open: boolean;
  children: ReactNode;
}

/**
 * Collapse — the animated disclosure REGION (not the trigger). Wrap the content a trigger reveals; the
 * caller owns the trigger and its chevron and just flips `open`. Height animates via grid-template-rows
 * (0fr↔1fr) — the one technique that animates a panel to its INTRINSIC height, so tall content is never
 * clipped by a guessed max-height. Closed, the inner is `inert`, so Tab and pointer skip the hidden
 * content. Reduced-motion swaps the animation for an instant toggle. Extra props forward to the root.
 */
export function Collapse({ open, className, children, ...rest }: CollapseProps) {
  return (
    <div className={cx("alk-collapse", className)} data-state={open ? "open" : "closed"} {...rest}>
      <div className="alk-collapse__inner" inert={open ? undefined : true}>
        {children}
      </div>
    </div>
  );
}
