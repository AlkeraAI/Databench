/** Slash-path helpers shared by the drive, the move picker and the chat workspace. */

/** `path` without trailing slashes, keeping `/` itself whole. */
function trimTrailingSlashes(path: string): string {
  const trimmed = path.replace(/\/+$/, "");
  return trimmed === "" && path.startsWith("/") ? "/" : trimmed;
}

/**
 * Whether `target` is `root` or sits anywhere under it.
 *
 * Compared on a path SEGMENT boundary, not as a bare prefix: a sibling named
 * after the root with something appended (`/a/Q3-archive` next to `/a/Q3`)
 * starts with the same characters and is a different folder entirely. A
 * trailing slash on either side does not change the answer, and every
 * absolute path lies within `/`.
 */
export function isPathWithin(root: string, target: string): boolean {
  const base = trimTrailingSlashes(root);
  const under = trimTrailingSlashes(target);
  if (under === base) return true;
  const prefix = base.endsWith("/") ? base : `${base}/`;
  return under.startsWith(prefix);
}

/** Whether `target` sits under `root` and is not `root` itself (a trailing
 *  slash does not make a path a different one). */
export function isPathBelow(root: string, target: string): boolean {
  return trimTrailingSlashes(root) !== trimTrailingSlashes(target) && isPathWithin(root, target);
}
