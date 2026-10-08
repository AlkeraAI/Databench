// ANSI escape sequences in kernel text, parsed into styled runs.
//
// Only SGR (`ESC [ ... m`) changes how text looks; every other control
// sequence (cursor moves, line erases, OSC titles and hyperlinks) is dropped.
// The result is plain data, so the component draws it as text spans and never
// builds markup from the stream.

export type AnsiColor = { kind: "palette"; index: number } | { kind: "rgb"; r: number; g: number; b: number };

export interface AnsiStyle {
  fg: AnsiColor | null;
  bg: AnsiColor | null;
  bold: boolean;
  dim: boolean;
  italic: boolean;
  underline: boolean;
  inverse: boolean;
  strike: boolean;
}

export interface AnsiSegment {
  text: string;
  style: AnsiStyle;
}

export const PLAIN_STYLE: AnsiStyle = {
  fg: null,
  bg: null,
  bold: false,
  dim: false,
  italic: false,
  underline: false,
  inverse: false,
  strike: false,
};

// CSI: ESC [ params intermediates final. OSC: ESC ] ... (BEL | ESC \).
// Other two-byte escapes: ESC followed by one char.
// eslint-disable-next-line no-control-regex -- matches ANSI escape sequences
const ESCAPE = /\x1b\[([0-?]*)([ -/]*)([@-~])|\x1b\][\s\S]*?(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x9b([0-?]*)[ -/]*([@-~])/g;

function clampByte(value: number | undefined): number | null {
  if (value === undefined || !Number.isInteger(value) || value < 0 || value > 255) return null;
  return value;
}

/** Reads an extended color (`38;5;n`, `38;2;r;g;b`, or the colon forms)
 *  starting at `codes[i]` (the 38 or 48). Returns the color and how many
 *  codes it consumed. */
function readExtended(codes: (number | number[])[], i: number): { color: AnsiColor | null; used: number } {
  const head = codes[i];
  if (Array.isArray(head)) {
    // Colon form: 38:5:n or 38:2:[colorspace]:r:g:b.
    const [, mode, ...rest] = head;
    if (mode === 5) {
      const index = clampByte(rest[0]);
      return { color: index === null ? null : { kind: "palette", index }, used: 1 };
    }
    if (mode === 2) {
      const rgb = rest.length >= 4 ? rest.slice(1, 4) : rest.slice(0, 3);
      const [r, g, b] = rgb.map(clampByte);
      if (r === null || g === null || b === null || r === undefined || g === undefined || b === undefined) {
        return { color: null, used: 1 };
      }
      return { color: { kind: "rgb", r, g, b }, used: 1 };
    }
    return { color: null, used: 1 };
  }
  const mode = codes[i + 1];
  if (mode === 5) {
    const index = clampByte(codes[i + 2] as number | undefined);
    return { color: index === null ? null : { kind: "palette", index }, used: 3 };
  }
  if (mode === 2) {
    const [r, g, b] = [codes[i + 2], codes[i + 3], codes[i + 4]].map((v) => clampByte(v as number | undefined));
    if (r === null || g === null || b === null) return { color: null, used: 5 };
    return { color: { kind: "rgb", r: r as number, g: g as number, b: b as number }, used: 5 };
  }
  return { color: null, used: 1 };
}

/** Applies one SGR parameter list to a style. */
export function applySgr(style: AnsiStyle, params: string): AnsiStyle {
  const next: AnsiStyle = { ...style };
  const codes: (number | number[])[] = (params === "" ? "0" : params)
    .split(";")
    .map((part) => (part.includes(":") ? part.split(":").map((p) => (p === "" ? -1 : Number(p))) : part === "" ? 0 : Number(part)));
  let i = 0;
  while (i < codes.length) {
    const entry = codes[i];
    const code = Array.isArray(entry) ? entry[0] : entry;
    if (code === 38 || code === 48) {
      const { color, used } = readExtended(codes, i);
      if (color) {
        if (code === 38) next.fg = color;
        else next.bg = color;
      }
      i += used;
      continue;
    }
    if (Array.isArray(entry) && code === 4) {
      // 4:0 turns underline off; 4:1..5 are underline styles.
      next.underline = entry[1] !== 0;
      i += 1;
      continue;
    }
    switch (true) {
      case code === 0:
        Object.assign(next, PLAIN_STYLE);
        break;
      case code === 1:
        next.bold = true;
        break;
      case code === 2:
        next.dim = true;
        break;
      case code === 3:
        next.italic = true;
        break;
      case code === 4:
        next.underline = true;
        break;
      case code === 7:
        next.inverse = true;
        break;
      case code === 9:
        next.strike = true;
        break;
      case code === 21:
        next.underline = true;
        break;
      case code === 22:
        next.bold = false;
        next.dim = false;
        break;
      case code === 23:
        next.italic = false;
        break;
      case code === 24:
        next.underline = false;
        break;
      case code === 27:
        next.inverse = false;
        break;
      case code === 29:
        next.strike = false;
        break;
      case code >= 30 && code <= 37:
        next.fg = { kind: "palette", index: code - 30 };
        break;
      case code === 39:
        next.fg = null;
        break;
      case code >= 40 && code <= 47:
        next.bg = { kind: "palette", index: code - 40 };
        break;
      case code === 49:
        next.bg = null;
        break;
      case code >= 90 && code <= 97:
        next.fg = { kind: "palette", index: code - 90 + 8 };
        break;
      case code >= 100 && code <= 107:
        next.bg = { kind: "palette", index: code - 100 + 8 };
        break;
      default:
        // Unknown or unsupported codes (blink, fonts, NaN) change nothing.
        break;
    }
    i += 1;
  }
  return next;
}

const MOVE_SOURCE = `${String.fromCharCode(27)}\\[(\\d*)([ABK])`;
/** Cursor up, cursor down and erase line: the movement a stream rewrites with. */
const MOVES = new RegExp(MOVE_SOURCE);

/** Collapses overwrites the way a terminal shows them, across every chunk of
 *  a stream joined together. A cursor writes into a grid of lines: `\r` goes
 *  back to the start of the line and what follows overwrites it (progress
 *  bars); cursor up and down (`ESC[nA`, `ESC[nB`) move between lines already
 *  written (several bars at once); erase line clears from the cursor
 *  (`ESC[K`) or the whole line (`ESC[2K`). `\r\n` is a line end. Columns
 *  count characters as written, colour sequences included, which is exact for
 *  the usual whole-line redraws. */
export function collapseCarriageReturns(text: string): string {
  if (!text.includes("\r") && !MOVES.test(text)) return text;
  const lines: string[] = [""];
  let row = 0;
  let col = 0;
  const write = (chunk: string) => {
    if (chunk === "") return;
    const line = lines[row] ?? "";
    const padded = line.length < col ? line + " ".repeat(col - line.length) : line;
    lines[row] = padded.slice(0, col) + chunk + padded.slice(col + chunk.length);
    col += chunk.length;
  };
  const source = text.replace(/\r\n/g, "\n");
  const token = new RegExp(`\\n|\\r|${MOVE_SOURCE}`, "g");
  let last = 0;
  for (let hit = token.exec(source); hit !== null; hit = token.exec(source)) {
    write(source.slice(last, hit.index));
    last = hit.index + hit[0].length;
    if (hit[0] === "\n") {
      row += 1;
      col = 0;
      while (lines.length <= row) lines.push("");
    } else if (hit[0] === "\r") {
      col = 0;
    } else if (hit[2] === "K") {
      lines[row] = hit[1] === "2" ? "" : (lines[row] ?? "").slice(0, col);
      if (hit[1] === "2") col = 0;
    } else {
      const n = Math.max(1, Number(hit[1] || "1"));
      row = hit[2] === "A" ? Math.max(0, row - n) : row + n;
      while (lines.length <= row) lines.push("");
    }
  }
  write(source.slice(last));
  return lines.join("\n");
}

function sameStyle(a: AnsiStyle, b: AnsiStyle): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** Splits `text` into runs of uniformly styled text. */
export function parseAnsi(text: string): AnsiSegment[] {
  const source = collapseCarriageReturns(text);
  const segments: AnsiSegment[] = [];
  let style = PLAIN_STYLE;
  let last = 0;
  const push = (chunk: string) => {
    if (chunk === "") return;
    const tail = segments[segments.length - 1];
    if (tail && sameStyle(tail.style, style)) tail.text += chunk;
    else segments.push({ text: chunk, style });
  };
  ESCAPE.lastIndex = 0;
  for (let match = ESCAPE.exec(source); match !== null; match = ESCAPE.exec(source)) {
    push(source.slice(last, match.index));
    last = match.index + match[0].length;
    const isSgr = (match[3] === "m" && match[2] === "") || match[5] === "m";
    if (isSgr) style = applySgr(style, match[1] ?? match[4] ?? "");
  }
  push(source.slice(last));
  return segments;
}

/** Text with every escape sequence removed (for copying and for the CSV). */
export function stripAnsi(text: string): string {
  return parseAnsi(text)
    .map((segment) => segment.text)
    .join("");
}

/** The xterm 256-color palette entry for index 16..255 as `rgb()`. Indices
 *  0..15 are themed through CSS tokens instead. */
export function xtermColor(index: number): string {
  if (index >= 232) {
    const level = 8 + (index - 232) * 10;
    return `rgb(${level}, ${level}, ${level})`;
  }
  const cube = index - 16;
  const steps = [0, 95, 135, 175, 215, 255];
  const r = steps[Math.floor(cube / 36) % 6];
  const g = steps[Math.floor(cube / 6) % 6];
  const b = steps[cube % 6];
  return `rgb(${r}, ${g}, ${b})`;
}
