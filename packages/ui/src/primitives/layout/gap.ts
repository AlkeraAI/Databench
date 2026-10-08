/** A step on the 8px-ish space scale (tokens `--alkSpace0`…`--alkSpace8`). A layout primitive takes
 *  this (or a raw CSS length) for its gap, so spacing stays on the scale instead of a guessed px value. */
export type SpaceScale = 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8;

/** Resolve a `gap` prop to a CSS value: a scale step → its `var(--alkSpaceN)` token; a string → itself
 *  (a raw length / calc); `undefined` → undefined (the CSS default applies). */
export function resolveGap(gap: SpaceScale | string | undefined): string | undefined {
  if (gap == null) return undefined;
  return typeof gap === "number" ? `var(--alkSpace${gap})` : gap;
}
