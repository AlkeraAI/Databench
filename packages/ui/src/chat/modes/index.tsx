// The panel's one mode vocabulary. Every organ that marks a permission mode --
// the composer trigger, the plan card's rows, the mode chips -- renders the
// same glyph for the same wire value, so a mode changed in one place is
// recognizable in the next. An unknown mode wears no glyph; its label carries
// it alone. Not exported from the package barrel.

import type { ReactNode } from "react";

// A guarded shield, a plan file, a double chevron, a circle-minus, a
// slashed probe: five distinct shapes on the same risk gradient as the hues.
const MODE_GLYPHS: Record<string, ReactNode> = {
  default: <path d="M7 1.7 11.5 3.3v3.3c0 2.7-1.9 4.7-4.5 5.5C4.4 11.3 2.5 9.3 2.5 6.6V3.3L7 1.7Z" />,
  plan: (
    <>
      <path d="M3.6 1.9h4.4l2.4 2.4v7.8H3.6Z" />
      <path d="M8 1.9v2.4h2.4" />
      <path d="M5.4 7.1h3.2M5.4 9.3h3.2" />
    </>
  ),
  auto: <path d="M2.8 3.4 6.4 7 2.8 10.6M7.4 3.4 11 7 7.4 10.6" />,
  read_only: (
    <>
      <circle cx="7" cy="7" r="5" />
      <path d="M4.4 7h5.2" />
    </>
  ),
  bypass: (
    <>
      <circle cx="4.7" cy="4.7" r="2.4" />
      <path d="M6.4 6.4 11 11M9.7 9.7l1.3-1.3M11 11l1-1" />
    </>
  ),
};

export function ModeGlyph({
  mode,
  className,
  size = 14,
}: {
  mode: string;
  className: string;
  size?: number;
}): ReactNode {
  return (
    <svg className={className} viewBox="0 0 14 14" width={size} height={size} aria-hidden="true">
      {MODE_GLYPHS[mode] ?? null}
    </svg>
  );
}
