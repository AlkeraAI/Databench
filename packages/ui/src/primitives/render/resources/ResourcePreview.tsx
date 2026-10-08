import { IconExternalLink } from "@tabler/icons-react";
import type { ReactNode } from "react";

import type { BlobReference, ResourceReference } from "../../../types";
import { DEFAULT_FILE_ICON, iconNameForFile } from "./file-icons/map";
import { ensureFileIconSprite } from "./file-icons/spriteHost";
import { PathDisplay } from "./PathDisplay";
import { renderHighlightedCode } from "./highlightCode";
import {
  parseUnifiedDiff,
  previewLanguage,
  previewLanguageLabel,
  previewTitle,
  splitPreviewLines,
} from "./previewModel";
import "./resources.css";

type Preview = NonNullable<ResourceReference["preview"]>;

// Mount at import so the first icon painted already resolves its symbol.
ensureFileIconSprite();

export interface LanguageIconProps {
  language?: string | null;
  path?: string | null;
  fallback?: ReactNode;
  size?: number | string;
}

/** A file/language glyph for a resource row, drawn from the shared file-icon sprite.
 *  The sprite is drawn on a 32 grid for 16px display; below 16 its 1px-scale gaps
 *  anti-alias away and layered glyphs read as broken. */
export function LanguageIcon({ language, path, fallback, size = 16 }: LanguageIconProps) {
  const source = (language ?? path ?? "").toLowerCase();
  const name = iconNameForFile(source);
  const glyph: ReactNode =
    name === null && fallback !== undefined ? (
      fallback
    ) : (
      <svg width={size} height={size}>
        <use href={`#${name ?? DEFAULT_FILE_ICON}`} />
      </svg>
    );
  return (
    <span className="alk-language-icon" aria-hidden>
      {glyph}
    </span>
  );
}

/** The +N / -N insertion/deletion count for a diff resource. */
export function DiffCount({ diff }: { diff: { insertions: number; deletions: number } }) {
  return (
    <span className="alk-diff-count">
      <span>+{diff.insertions}</span>
      <span>-{diff.deletions}</span>
    </span>
  );
}

export interface ReferenceListProps {
  references: BlobReference[] | undefined;
  variant?: "card" | "list";
}

/** Render blob references as inert chips. Pure rendering (no tool-card frame), so it lives here. */
export function ReferenceList({ references, variant }: ReferenceListProps) {
  if (!references || references.length === 0) return null;
  return (
    <div className={variant === "card" ? "alk-ref-list is-card" : "alk-ref-list"} aria-label="Results">
      {references.map((reference) => (
        <span key={reference.handle} className="alk-ref-chip" title={reference.handle}>
          <IconExternalLink size="var(--alkIconSm)" aria-hidden />
          <span className="alk-ref-chip__name">{reference.name}</span>
        </span>
      ))}
    </div>
  );
}

export interface ResourcePreviewProps {
  preview: Preview;
  resource?: ResourceReference;
  compact?: boolean;
  bare?: boolean;
  hideChrome?: boolean;
  rowNumbers?: boolean;
  pageSize?: number;
}

/** Dispatch typed resource previews into lightweight code, diff, and table views. Rendering only —
 *  the highlighting runs through the shared Prism engine, the diff colors through the diff tokens. */
export function ResourcePreview({
  preview,
  resource,
  compact,
  bare,
  hideChrome,
  rowNumbers = false,
}: ResourcePreviewProps) {
  const className = [
    preview.kind === "table" ? "alk-resource-preview" : "alk-code-preview",
    compact && "is-compact",
    bare && "is-bare",
  ].filter(Boolean).join(" ");
  const language = previewLanguage(preview, resource);
  return (
    <div className={className} data-kind={preview.kind}>
      {hideChrome || preview.kind === "table" ? null : (
        <div className="alk-code-preview__bar">
          <span className="alk-code-preview__title">
            <PathDisplay path={previewTitle(preview, resource)} />
          </span>
          <span className="alk-code-preview__language">{previewLanguageLabel(preview, resource)}</span>
        </div>
      )}
      {preview.kind === "table" ? (
        <PreviewTable preview={preview} rowNumbers={rowNumbers} />
      ) : (
        <CodePreview preview={preview} language={language} />
      )}
      {preview.truncated ? <div className="alk-resource-preview__truncated">Preview truncated</div> : null}
    </div>
  );
}

export const ResourcePreviewBlock = ResourcePreview;

function CodePreview({ preview, language }: { preview: Preview; language: string }) {
  const lines = splitPreviewLines(preview.content ?? (preview.kind === "json" ? "{}" : ""));
  if (preview.kind === "diff") {
    const rows = parseUnifiedDiff(preview.content ?? "");
    return (
      <div className="alk-code-preview__scroller alk-scroll">
        {rows.map((row, index) => {
          const lineNumber = row.kind === "remove" ? row.oldLine : row.newLine;
          return (
            <div key={`${index}-${row.kind}-${row.text}`} className="alk-diff-line" data-line-kind={row.kind}>
              <span className="alk-diff-line__marker" aria-hidden>{row.marker}</span>
              <span className="alk-diff-line__number">{lineNumber ?? ""}</span>
              <code>{row.kind === "hunk" ? row.text : renderHighlightedCode(row.text, language)}</code>
            </div>
          );
        })}
      </div>
    );
  }
  return (
    <div className="alk-code-preview__scroller alk-scroll">
      {lines.map((line, index) => (
        <div key={`${index}-${line}`} className="alk-code-line">
          <span className="alk-code-line__number">{index + 1}</span>
          <code>{renderHighlightedCode(line, language)}</code>
        </div>
      ))}
    </div>
  );
}

function PreviewTable({ preview, rowNumbers }: { preview: Preview; rowNumbers: boolean }) {
  const columns = preview.columns?.length
    ? preview.columns
    : [...new Set((preview.rows ?? []).flatMap((row) => Object.keys(row)))];
  const rows = preview.rows ?? [];
  if (columns.length === 0) return <div className="alk-menu-empty">No rows.</div>;
  return (
    <div className="alk-preview-table-wrap alk-scroll">
      <table className="alk-preview-table">
        <thead>
          <tr>
            {rowNumbers ? <th className="alk-preview-table__rownum" aria-hidden /> : null}
            {columns.map((column, index) => <th key={`${column}-${index}`}>{column}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {rowNumbers ? <td className="alk-preview-table__rownum">{rowIndex + 1}</td> : null}
              {columns.map((column, columnIndex) => <td key={columnIndex}>{String(row[column] ?? "")}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
