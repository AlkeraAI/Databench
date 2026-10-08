/** The one shape a client-side retry wait takes: a floor doubled per attempt,
 *  held at a cap.
 *
 *  Every caller keeps its own floor, its own cap, its own idea of which attempt
 *  is the first and its own jitter; what they share is the climb, written once
 *  here so two ladders that look alike are alike. It lives in the UI library
 *  because the library's own components retry too, and the portal reaches it
 *  from `@alkera/ui/backoff` rather than keeping a second copy.
 *
 *  `rung` is the number of doublings: 0 is the floor itself. A caller whose
 *  first retry is not rung 0 subtracts its own offset before asking. With no
 *  cap the wait doubles without bound, which is what a ladder that stops after
 *  a fixed handful of asks wants.
 */
export function doublingDelay(rung: number, floorMs: number, capMs: number = Number.POSITIVE_INFINITY): number {
  return Math.min(floorMs * 2 ** rung, capMs);
}
