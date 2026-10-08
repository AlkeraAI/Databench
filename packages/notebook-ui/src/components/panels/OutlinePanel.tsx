// The notebook's outline: the headings in its Markdown cells and its named
// cells, in document order, each a way to jump there.

import { useMemo } from "react";
import type { DocCell } from "../../model/types";
import "./panels.css";

export interface OutlineEntry {
  cell_id: string;
  kind: "heading" | "cell";
  text: string;
  /** 1 to 6. A named cell sits one level under the heading before it. */
  level: number;
}

export interface OutlinePanelProps {
  cells: readonly DocCell[];
  /** The cell drawn as current. */
  activeId?: string | null;
  onJump: (cellId: string) => void;
}

const ATX = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const SETEXT = /^ {0,3}(=+|-+)[ \t]*$/;

function plain(text: string): string {
  return text
    .replace(/`([^`]*)`/g, "$1")
    .replace(/(\*\*|__)(.+?)\1/g, "$2")
    .replace(/(\*|_)(.+?)\1/g, "$2")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .trim();
}

/** The headings of one Markdown text: ATX (`# Title`) and setext (a line
 *  underlined with `===` or `---`), not inside fenced code. */
export function markdownHeadings(source: string): { text: string; level: number }[] {
  const out: { text: string; level: number }[] = [];
  let fence: string | null = null;
  let previous: string | null = null;
  for (const line of source.split(/\r?\n/)) {
    const fenceMatch = FENCE.exec(line);
    if (fence !== null) {
      if (fenceMatch && fenceMatch[1][0] === fence[0] && fenceMatch[1].length >= fence.length) fence = null;
      previous = null;
      continue;
    }
    if (fenceMatch) {
      fence = fenceMatch[1];
      previous = null;
      continue;
    }
    const atx = ATX.exec(line);
    if (atx) {
      const text = plain(atx[2] ?? "");
      if (text) out.push({ text, level: atx[1].length });
      previous = null;
      continue;
    }
    const setext = SETEXT.exec(line);
    if (setext && previous !== null && previous.trim() !== "") {
      out.push({ text: plain(previous), level: setext[1][0] === "=" ? 1 : 2 });
      previous = null;
      continue;
    }
    previous = line;
  }
  return out;
}

export function outlineEntries(cells: readonly DocCell[]): OutlineEntry[] {
  const entries: OutlineEntry[] = [];
  let level = 0;
  for (const cell of cells) {
    if (cell.kind === "markdown") {
      for (const heading of markdownHeadings(cell.source)) {
        entries.push({ cell_id: cell.id, kind: "heading", text: heading.text, level: heading.level });
        level = heading.level;
      }
    }
    if (cell.name && cell.name !== "_") {
      entries.push({ cell_id: cell.id, kind: "cell", text: cell.name, level: Math.min(level + 1, 6) });
    }
  }
  return entries;
}

export function OutlinePanel({ cells, activeId = null, onJump }: OutlinePanelProps) {
  const entries = useMemo(() => outlineEntries(cells), [cells]);
  return (
    <nav className="nb-panel" aria-label="Outline">
      {entries.length === 0 ? (
        <p className="nb-panel__empty">No headings or named cells</p>
      ) : (
        <ul className="nb-outline">
          {entries.map((entry, i) => (
            <li key={`${entry.cell_id}:${i}`} style={{ paddingLeft: (entry.level - 1) * 12 }}>
              <button
                type="button"
                className={entry.kind === "cell" ? "nb-link nb-mono" : "nb-link"}
                aria-current={entry.cell_id === activeId ? "location" : undefined}
                onClick={() => onJump(entry.cell_id)}
              >
                {entry.text}
              </button>
            </li>
          ))}
        </ul>
      )}
    </nav>
  );
}
