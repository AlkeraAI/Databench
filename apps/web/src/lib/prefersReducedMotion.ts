/** Whether the viewer asked for less motion. Read at the moment of the decision, not held in
 *  state: every caller is imperative (an animation about to start, a scroll about to run). */
export const prefersReducedMotion = (): boolean =>
  typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
