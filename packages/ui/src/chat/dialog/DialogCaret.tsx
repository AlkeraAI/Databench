// The package's disclosure caret: the plan document's fold, the permission
// card's always-allow menu, and a subagent report's fold open with one mark.

import type { ReactNode } from "react";

import "./dialog.css";

/** The caret takes the organ's own class, because each sheet still places it. */
export function DialogCaret({ className, open }: { className: string; open: boolean }): ReactNode {
  return (
    <svg className={className} data-open={open ? "" : undefined} viewBox="0 0 14 14" width="13" height="13" aria-hidden="true">
      <path d="M3.6 5.4 7 8.8l3.4-3.4" />
    </svg>
  );
}
