import { IconFileText, IconTable } from "@tabler/icons-react";

import { formatCell, type BlobPage } from "@alkera/chat-model";

import { DataTable } from "../DataTable";
import { BlobTable } from "./BlobTable";
import type { ReferenceRenderer } from "./types";
import "./blob.css";

// The reference-renderer registry. A reference resolves to a renderer by its
// declared `refType`, else by the fetched page's `kind` ("rows" / "text"), else
// the default. New types call `registerReferenceRenderer` once at module load —
// the chat transcript never has to learn about them.

const PREVIEW_ROWS = 5;
const PREVIEW_COLS = 5;
const PREVIEW_CHARS = 600;

// The chip glyph matches the tool cards' operational icon size; the token holds
// its value on every surface (theme root), so the glyph is stable outside a card.
const CHIP_ICON_SIZE = "var(--alkIconLg)";

const registry = new Map<string, ReferenceRenderer>();

export function registerReferenceRenderer(renderer: ReferenceRenderer): void {
  registry.set(renderer.type, renderer);
}

/** Resolve a renderer: prefer the reference's declared type, else the page kind,
 *  else the catch-all text renderer (so an unknown type is never a dead end). */
export function resolveReferenceRenderer(refType: string | undefined, pageKind: string): ReferenceRenderer {
  return (
    (refType ? registry.get(refType) : undefined)
    ?? registry.get(pageKind)
    ?? registry.get("text")
    ?? TEXT_RENDERER
  );
}

function pluralRows(n: number): string {
  return `${n.toLocaleString()} row${n === 1 ? "" : "s"}`;
}

const ROWS_RENDERER: ReferenceRenderer = {
  type: "rows",
  icon: <IconTable size={CHIP_ICON_SIZE} />,
  describe: (page) =>
    `${pluralRows(page.total)} × ${page.columns.length.toLocaleString()} col${page.columns.length === 1 ? "" : "s"}`,
  Preview: ({ page }) => <BlobTable page={page} maxRows={PREVIEW_ROWS} maxCols={PREVIEW_COLS} />,
  FullView: ({ page }: { page: BlobPage }) => (
    <DataTable
      columns={page.columns}
      rows={page.rows.map((row) => page.columns.map((_column, index) => formatCell(row[index])))}
      rowNumbers
      fill
    />
  ),
};

const TEXT_RENDERER: ReferenceRenderer = {
  type: "text",
  icon: <IconFileText size={CHIP_ICON_SIZE} />,
  describe: (page) => `${page.total.toLocaleString()} char${page.total === 1 ? "" : "s"}`,
  Preview: ({ page }) => <pre className="alk-blobv-text is-preview">{page.text.slice(0, PREVIEW_CHARS)}</pre>,
  FullView: ({ page }) => <pre className="alk-blobv-text">{page.text}</pre>,
};

registerReferenceRenderer(ROWS_RENDERER);
registerReferenceRenderer(TEXT_RENDERER);
