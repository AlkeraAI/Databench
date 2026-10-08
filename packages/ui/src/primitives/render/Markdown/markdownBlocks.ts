// Block parser for the chat markdown subset (headings, paragraphs, lists, quotes,
// fenced code, tables, display math, blob reference cards, chat images). Pure
// data-in / data-out, no React and no raw HTML passthrough.

import { chatImagePath } from "./chatPaths";

export type MarkdownBlock =
  | { kind: "heading"; level: 1 | 2 | 3; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "quote"; text: string }
  | { kind: "code"; language: string | null; text: string }
  | { kind: "math"; text: string }
  | MarkdownListBlock
  | { kind: "table"; headers: string[]; rows: string[][] }
  | { kind: "blob"; handle: string; name: string }
  | { kind: "image"; alt: string; path: string };

/** A list. `items` is each item's own text, its continuation lines joined
 *  with newlines. `nested` is present only when an item holds a list indented
 *  under it: `nested[i]` is item `i`'s sub-lists, in order, and a flat list
 *  carries no `nested` at all. */
export interface MarkdownListBlock {
  kind: "list";
  ordered: boolean;
  items: string[];
  nested?: MarkdownListBlock[][];
}

// A line that is SOLELY a markdown image: `![alt](path)`. Own-line only, on
// purpose: the transcript parses the whole markdown on every streamed token,
// and splitting paragraphs around inline images would put a second pass on
// that hot path for a form neither the agent's brief nor the composer writes.
// An image buried mid-sentence stays text, and a target that is not a chat
// image (a web URL, a data URI, an escape — see `chatImagePath`) stays text.
const IMAGE_LINE = /^!\[([^\]]*)\]\(([^)]+)\)$/;

/** The image block a line is, or null when it is not solely a chat image. */
function parseImageLine(line: string): { alt: string; path: string } | null {
  const match = IMAGE_LINE.exec(line.trim());
  if (!match) return null;
  const path = chatImagePath(match[2]);
  if (path === null) return null;
  const alt = match[1].trim();
  return { alt: alt === "" ? path.slice(path.lastIndexOf("/") + 1) : alt, path };
}

// A line that is SOLELY a blob link, as the agent emits a result handle:
// `[label](blob:<sha>)` (canonical) or the tolerated `blob://<sha>`. The handle
// is a content sha256 — 64 lowercase hex chars, the shape the blob store
// validates. Anchored on the trimmed line so the rich card renders only when the
// whole paragraph IS the link, not when one is buried mid-sentence.
const BLOB_LINK_BLOCK = /^\[([^\]]+)\]\(blob:(?:\/\/)?([0-9a-f]{64})\)$/;

// The tolerated bare-line form this renderer historically accepted:
// `blob:<handle>` optionally followed by a display name.
const BARE_BLOB_LINE = /^blob:([A-Za-z0-9._:-]+)(?:\s+(.+))?$/;

/** The (name, handle) of a paragraph that is solely a blob reference, else null. */
function parseBlobBlock(text: string): { name: string; handle: string } | null {
  const trimmed = text.trim();
  const canonical = BLOB_LINK_BLOCK.exec(trimmed);
  if (canonical) return { name: canonical[1], handle: canonical[2] };
  const bare = BARE_BLOB_LINE.exec(trimmed);
  if (bare) return { name: bare[2] ?? "Result", handle: bare[1] };
  return null;
}

export function parseMarkdownBlocks(content: string): MarkdownBlock[] {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: MarkdownBlock[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index] ?? "";
    if (line.trim() === "") {
      index += 1;
      continue;
    }

    const fence = /^```([A-Za-z0-9_-]+)?\s*$/.exec(line);
    if (fence) {
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index] ?? "")) {
        body.push(lines[index] ?? "");
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push({ kind: "code", language: fence[1] ?? null, text: body.join("\n") });
      continue;
    }

    if (/^\$\$\s*$/.test(line)) {
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !/^\$\$\s*$/.test(lines[index] ?? "")) {
        body.push(lines[index] ?? "");
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push({ kind: "math", text: body.join("\n").trim() });
      continue;
    }

    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    if (heading) {
      blocks.push({ kind: "heading", level: heading[1].length as 1 | 2 | 3, text: heading[2] });
      index += 1;
      continue;
    }

    if (/^>\s?/.test(line)) {
      const body: string[] = [];
      while (index < lines.length && /^>\s?/.test(lines[index] ?? "")) {
        body.push((lines[index] ?? "").replace(/^>\s?/, ""));
        index += 1;
      }
      blocks.push({ kind: "quote", text: body.join("\n") });
      continue;
    }

    const table = parseTable(lines, index);
    if (table) {
      blocks.push(table.block);
      index = table.nextIndex;
      continue;
    }

    const list = parseList(lines, index);
    if (list) {
      blocks.push(list.block);
      index = list.nextIndex;
      continue;
    }

    const image = parseImageLine(line);
    if (image) {
      blocks.push({ kind: "image", ...image });
      index += 1;
      continue;
    }

    const paragraph: string[] = [];
    while (index < lines.length && lines[index]?.trim() !== "" && !isBlockStart(lines, index)) {
      paragraph.push(lines[index] ?? "");
      index += 1;
    }
    if (paragraph.length === 0) {
      paragraph.push(line);
      index += 1;
    }
    const text = paragraph.join("\n");
    const blob = parseBlobBlock(text);
    blocks.push(blob ? { kind: "blob", ...blob } : { kind: "paragraph", text });
  }

  return blocks;
}

function parseTable(lines: string[], index: number): { block: MarkdownBlock; nextIndex: number } | null {
  const header = lines[index] ?? "";
  const divider = lines[index + 1] ?? "";
  // One column is a table too (`| Table |` over `|---|`): the divider needs a
  // pipe, which is what keeps a bare `---` rule from reading as one.
  if (
    !header.includes("|")
    || !divider.includes("|")
    || !/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(divider)
  ) {
    return null;
  }
  const headers = splitTableRow(header);
  const rows: string[][] = [];
  let cursor = index + 2;
  while (cursor < lines.length && (lines[cursor] ?? "").includes("|") && (lines[cursor] ?? "").trim() !== "") {
    rows.push(splitTableRow(lines[cursor] ?? ""));
    cursor += 1;
  }
  return { block: { kind: "table", headers, rows }, nextIndex: cursor };
}

// An item marker: the indent before it, and the marker itself.
const LIST_MARKER = /^([ \t]*)(\d+\.|[-*])\s+/;

/** A line's indent in columns, a tab counting as four. */
function indentOf(line: string): number {
  const lead = /^[ \t]*/.exec(line)?.[0] ?? "";
  return lead.replace(/\t/g, "    ").length;
}

function parseList(lines: string[], index: number): { block: MarkdownBlock; nextIndex: number } | null {
  if (!LIST_MARKER.test(lines[index] ?? "")) return null;
  return parseListAt(lines, index);
}

/** The list whose first item is at `index`. An item runs on through every line
 *  indented past its own marker: a line holding a marker there opens a sub-list
 *  inside the item, any other line continues the item's text. A blank line
 *  stays inside the item only when what follows it is indented under the item.
 *  The list ends at a marker of the other kind at its own level, at a line
 *  indented less than it, or at an unindented line with no marker. */
function parseListAt(lines: string[], index: number): { block: MarkdownListBlock; nextIndex: number } {
  const firstLine = lines[index] ?? "";
  const first = LIST_MARKER.exec(firstLine);
  const ordered = /\d/.test(first?.[2] ?? "");
  const level = indentOf(firstLine);
  // Past this column a line belongs to the item above it rather than the list.
  const inside = level + 2;
  const items: string[] = [];
  const nested: MarkdownListBlock[][] = [];
  let cursor = index;
  while (cursor < lines.length) {
    const line = lines[cursor] ?? "";
    const marker = LIST_MARKER.exec(line);
    if (!marker || indentOf(line) >= inside || indentOf(line) < level) break;
    if (/\d/.test(marker[2]) !== ordered) break;
    const text = [line.slice(marker[0].length)];
    const subs: MarkdownListBlock[] = [];
    cursor += 1;
    while (cursor < lines.length) {
      const next = lines[cursor] ?? "";
      if (next.trim() === "") {
        let ahead = cursor + 1;
        while (ahead < lines.length && (lines[ahead] ?? "").trim() === "") ahead += 1;
        if (ahead < lines.length && indentOf(lines[ahead] ?? "") >= inside) {
          cursor = ahead;
          continue;
        }
        break;
      }
      if (indentOf(next) < inside) break;
      if (LIST_MARKER.test(next)) {
        const sub = parseListAt(lines, cursor);
        subs.push(sub.block);
        cursor = sub.nextIndex;
        continue;
      }
      text.push(next.trim());
      cursor += 1;
    }
    items.push(text.join("\n"));
    nested.push(subs);
  }
  const block: MarkdownListBlock = { kind: "list", ordered, items };
  if (nested.some((subs) => subs.length > 0)) block.nested = nested;
  return { block, nextIndex: cursor };
}

function isBlockStart(lines: string[], index: number): boolean {
  const line = lines[index] ?? "";
  if (index === 0) return false;
  return /^```/.test(line)
    || /^\$\$\s*$/.test(line)
    || /^(#{1,3})\s+/.test(line)
    || /^>\s?/.test(line)
    || /^\s*([-*]|\d+\.)\s+/.test(line)
    || parseImageLine(line) !== null
    || Boolean(parseTable(lines, index));
}

function splitTableRow(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}
