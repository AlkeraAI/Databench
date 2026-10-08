import type { CSSProperties } from "react";

/** A viz series tone — the core tone vocabulary, each mapping to its solid token. Setting one paints
 *  the component's `--alk-viz-color` inline; unset, the ambient `--alk-viz-color` (or the brand
 *  fallback) paints the series as before. */
export type VizTone = "brand" | "neutral" | "info" | "success" | "warning" | "danger";

const VIZ_TONE_COLOR: Record<VizTone, string> = {
  brand: "var(--alkBrand)",
  neutral: "var(--alkCatNeutralSolid)",
  info: "var(--alkInfo)",
  success: "var(--alkSuccess)",
  warning: "var(--alkWarning)",
  danger: "var(--alkDanger)",
};

/** The inline style a toned viz root carries — `undefined` when no tone is set, so the ambient
 *  series colour keeps flowing through untouched. */
export function vizToneStyle(tone: VizTone | undefined): CSSProperties | undefined {
  return tone ? ({ "--alk-viz-color": VIZ_TONE_COLOR[tone] } as CSSProperties) : undefined;
}
