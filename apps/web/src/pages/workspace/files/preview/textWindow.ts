// Where a window of a text file is cut, and what a ranged answer says it holds.
//
// A large text file is read a window of bytes at a time, and a window's edge
// falls wherever the byte count does: inside a line, inside a markdown block,
// inside a multi-byte character. Drawing that edge would show a half line that
// the next window then completes somewhere else. So each window is cut back to
// the last whole line — for markdown, the last blank line — and the bytes after
// the cut are carried into the next window, where they are completed.
//
// The cut is made in bytes, before decoding. A newline byte never occurs inside
// a multi-byte UTF-8 sequence, so a cut after one never splits a character; the
// last-resort cut (a window with no newline at all) backs off to a character
// boundary for the same reason.

const NEWLINE = 0x0a;
const CARRIAGE_RETURN = 0x0d;

/** The two halves of a window: what is drawn now, and what waits for the next. */
export interface WindowCut {
  take: Uint8Array;
  carry: Uint8Array;
}

/** How a window may be cut: at the last line, or at the last blank line so a
 *  markdown block (a paragraph, a list, a table) is never split. */
export type CutAt = "line" | "block";

/** Join what the previous window carried onto the bytes this one brought. */
export function joinBytes(carry: Uint8Array, next: Uint8Array): Uint8Array {
  if (carry.length === 0) return next;
  const out = new Uint8Array(carry.length + next.length);
  out.set(carry, 0);
  out.set(next, carry.length);
  return out;
}

/** The offset just past the last blank line in `bytes`, or -1. A blank line is
 *  a newline followed by another, with an optional carriage return between. */
function afterLastBlankLine(bytes: Uint8Array): number {
  for (let index = bytes.length - 1; index > 0; index -= 1) {
    if (bytes[index] !== NEWLINE) continue;
    const before = bytes[index - 1] === CARRIAGE_RETURN ? index - 2 : index - 1;
    if (before >= 0 && bytes[before] === NEWLINE) return index + 1;
  }
  return -1;
}

/** The offset just past the last newline in `bytes`, or -1. */
function afterLastNewline(bytes: Uint8Array): number {
  const at = bytes.lastIndexOf(NEWLINE);
  return at === -1 ? -1 : at + 1;
}

/** The largest offset at or before `bytes.length` that does not split a UTF-8
 *  character. */
function characterBoundary(bytes: Uint8Array): number {
  let lead = bytes.length - 1;
  // Walk back over continuation bytes (10xxxxxx) to the sequence's lead byte.
  while (lead >= 0 && lead > bytes.length - 4 && ((bytes[lead] ?? 0) & 0xc0) === 0x80) lead -= 1;
  if (lead < 0) return bytes.length;
  const first = bytes[lead] ?? 0;
  const width = first < 0x80 ? 1 : first >= 0xf0 ? 4 : first >= 0xe0 ? 3 : first >= 0xc0 ? 2 : 1;
  return lead + width <= bytes.length ? bytes.length : lead;
}

/**
 * Cut a window so no line (or, for `block`, no markdown block) is split.
 *
 * The last window of a file is taken whole: there is nothing after it to carry
 * into. A window with no blank line falls back to its last line, and one with
 * no newline at all — a minified document, one long line — to its last whole
 * character, because drawing nothing until a newline turns up could mean
 * fetching the whole file before the reader sees a byte.
 */
export function cutWindow(bytes: Uint8Array, options: { final: boolean; at: CutAt }): WindowCut {
  if (options.final) return { take: bytes, carry: new Uint8Array(0) };
  let cut = options.at === "block" ? afterLastBlankLine(bytes) : -1;
  if (cut <= 0) cut = afterLastNewline(bytes);
  if (cut <= 0) cut = characterBoundary(bytes);
  return { take: bytes.subarray(0, cut), carry: bytes.slice(cut) };
}

/** What a `206` says it served: the inclusive span and the whole size. */
export interface ServedRange {
  start: number;
  end: number;
  total: number | null;
}

/** Read `Content-Range: bytes start-end/total`. `null` for anything else — a
 *  missing header, another unit, an unsatisfied-range form. */
export function servedRange(header: string | null): ServedRange | null {
  if (header === null) return null;
  const match = /^\s*bytes\s+(\d+)-(\d+)\/(\d+|\*)\s*$/i.exec(header);
  if (!match) return null;
  const start = Number(match[1]);
  const end = Number(match[2]);
  if (end < start) return null;
  return { start, end, total: match[3] === "*" ? null : Number(match[3]) };
}
