/* The theme's motion curves for JS consumers. CSS reads them as tokens (tokens.css); an
 * imperative animator (a rAF interpolator, a canvas camera) needs the same curve as a function,
 * and a hand-rolled approximation drifts from the theme's motion. */

/** The JS mirror of `--alkEaseDecelerate: cubic-bezier(0, 0, 0, 1)`. That bezier reduces to the
 *  closed form y = 3x^(2/3) - 2x, so this IS the token's curve, not an approximation. */
export const easeDecelerate = (t: number): number => (t <= 0 ? 0 : t >= 1 ? 1 : 3 * Math.cbrt(t * t) - 2 * t);
