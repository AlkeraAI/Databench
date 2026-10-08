/**
 * True when the element cannot show all of its content — the measurement behind a truncation
 * tooltip, so an annotation is offered on the reading that is actually cut off and on no other.
 *
 * Both axes are read: a single-line ellipsis overruns width, a line clamp overruns height.
 *
 * Measured ON DEMAND, never watched. A tooltip matters only at the instant a pointer or focus
 * arrives, and reading `scrollWidth` forces synchronous layout — a component used thousands of
 * times on one transcript cannot pay a layout read and a ResizeObserver per instance to say
 * nothing. Measuring at the event is also self-correcting: a column widened since the last paint
 * reports its current width, so a reading that now fits offers no tip.
 */
export function isOverflowing(el: HTMLElement | null): boolean {
  if (!el) return false;
  // 1px of slack: a fractional layout width rounds scrollWidth up past clientWidth on text that
  // is not actually clipped.
  return el.scrollWidth - el.clientWidth > 1 || el.scrollHeight - el.clientHeight > 1;
}
