import { IconChevronRight } from "@tabler/icons-react";

import "./caret.css";

export interface CaretProps {
  /** Points down when true; chevron-right when false. */
  open: boolean;
  size?: number | string;
}

/** Disclosure caret shared by collapsible tool-card rows. */
export function Caret({ open, size = 14 }: CaretProps) {
  return (
    <span className="alk-caret" data-open={open ? "true" : undefined} aria-hidden>
      <IconChevronRight size={size} />
    </span>
  );
}
