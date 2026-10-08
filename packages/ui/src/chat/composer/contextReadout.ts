// What the composer's context readout says, in one place.
//
// The reader's question is "how close is this conversation to being compacted",
// and the honest answer depends on what the shell knows. With the model's
// window it is a share, and the ring can be drawn. Without one it is the token
// count alone: a percentage of a window nobody published would be a fraction of
// a guess.

import { compactTokens } from "../transcript";

export interface ContextReadoutProps {
  /** What the rail shows. Absent when there is nothing to report. */
  context?: string;
  /** 0–100, only where a window is known. Drives the ring. */
  contextPercent?: number;
  /** The whole fact, for the tooltip and the accessible name. */
  contextTitle?: string;
}

/** The three context props for a conversation of `tokens` against `window`.
 *  `tokens` null (no turn has reported usage yet) reports nothing at all. */
export function contextReadout(
  tokens: number | null | undefined,
  window?: number | null,
): ContextReadoutProps {
  if (typeof tokens !== "number" || !Number.isFinite(tokens) || tokens < 0) return {};
  if (typeof window === "number" && window > 0) {
    const percent = Math.min(100, Math.round((tokens / window) * 100));
    return {
      context: `${percent}%`,
      contextPercent: percent,
      contextTitle: `${tokens.toLocaleString()} of ${window.toLocaleString()} tokens`,
    };
  }
  return {
    context: compactTokens(tokens),
    contextTitle: `${tokens.toLocaleString()} tokens in this conversation`,
  };
}
