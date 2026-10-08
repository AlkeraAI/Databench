import type { ReactNode } from "react";

import { AlertIcon, CheckIcon, ClockIcon, CrossIcon } from "./icons";

// The status mark for a terminal device-grant state — a flask-line glyph in a tinted ring.
// Each kind pairs a distinct shape with a distinct hue, so the outcome reads without relying
// on color alone.

export type MarkKind = "success" | "denied" | "expired" | "error";

const MARKS: Record<MarkKind, ReactNode> = {
  success: <CheckIcon size={26} />,
  denied: <CrossIcon size={26} />,
  expired: <ClockIcon size={26} />,
  error: <AlertIcon size={26} />,
};

export function StatusMark({ kind }: { kind: MarkKind }) {
  return (
    <span className="pa-mark" data-kind={kind} aria-hidden="true">
      {MARKS[kind]}
    </span>
  );
}
