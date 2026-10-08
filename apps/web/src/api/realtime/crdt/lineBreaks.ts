// Offsets between a CodeMirror document and the Loro text it shows.
//
// CodeMirror counts a line break as one position whatever it is spelled as,
// while the Loro text holds the file's bytes as they are: a file whose breaks
// are all `\r\n` keeps them, and each one is two UTF-16 units there. The
// editor is told to split lines only on the file's own separator (`\r\n` for
// such a file, `\n` otherwise), so its text joined back is byte for byte the
// Loro text, and an offset maps by adding one per break before it. Anything
// else (a lone `\r`, a lone `\n` in a CRLF file) is an ordinary character on
// both sides. UTF-16 is both sides' unit, so a surrogate pair counts two on
// each.

/** The interface of a CodeMirror `Text` this module needs. */
export interface LineIndex {
  readonly length: number;
  readonly lines: number;
  lineAt(pos: number): { number: number; from: number; to: number };
  line(n: number): { number: number; from: number; to: number };
}

export type LineSeparator = "\r\n" | "\n";

/** The separator the editor splits `text` on: `\r\n` when every line break in
 *  it is one, `\n` otherwise (and for a text with no break at all). */
export function lineSeparatorFor(text: string): LineSeparator {
  let crlf = 0;
  let lf = 0;
  for (let i = text.indexOf("\n"); i !== -1; i = text.indexOf("\n", i + 1)) {
    lf += 1;
    if (i > 0 && text.charCodeAt(i - 1) === 13) crlf += 1;
  }
  return crlf > 0 && crlf === lf ? "\r\n" : "\n";
}

/** The Loro offset of editor position `pos`. */
export function toLoro(doc: LineIndex, pos: number, separator: LineSeparator): number {
  if (separator === "\n") return pos;
  return pos + doc.lineAt(pos).number - 1;
}

/** The editor position of Loro offset `index`, or `null` when it falls between
 *  the two halves of a `\r\n` the editor shows as one break. */
export function fromLoro(doc: LineIndex, index: number, separator: LineSeparator): number | null {
  if (separator === "\n") return index;
  // Line n starts at Loro offset line.from + (n - 1): find the last line that
  // starts at or before `index`.
  let low = 1;
  let high = doc.lines;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if (doc.line(mid).from + mid - 1 <= index) low = mid;
    else high = mid - 1;
  }
  const line = doc.line(low);
  const pos = index - (low - 1);
  // Past the line's end by one is the middle of its break.
  if (pos > line.to) return null;
  return pos;
}
