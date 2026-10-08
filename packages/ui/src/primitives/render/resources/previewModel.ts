import type { ResourceReference } from "../../../types";

export type DiffLineKind = "add" | "remove" | "hunk" | "context";

export interface DiffPreviewRow {
  kind: DiffLineKind;
  marker: string;
  oldLine: number | null;
  newLine: number | null;
  text: string;
}

/** Keep preview line rendering stable across Unix and Windows line endings. */
export function splitPreviewLines(content: string): string[] {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  return lines.length > 0 ? lines : [""];
}

const HUNK_HEADER = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/** Parse unified diff text into editor-like rows with stable old/new line numbers. */
export function parseUnifiedDiff(content: string): DiffPreviewRow[] {
  const rows: DiffPreviewRow[] = [];
  let oldLine: number | null = 1;
  let newLine: number | null = 1;
  for (const rawLine of stripDiffPreamble(splitPreviewLines(content))) {
    if (rawLine.startsWith("@@")) {
      const hunk = HUNK_HEADER.exec(rawLine);
      if (hunk) {
        oldLine = Number(hunk[1]);
        newLine = Number(hunk[2]);
      }
      rows.push({ kind: "hunk", marker: "", oldLine: null, newLine: null, text: rawLine });
      continue;
    }
    if (rawLine.startsWith("\\")) continue;
    if (rawLine.startsWith("+")) {
      rows.push({ kind: "add", marker: "+", oldLine: null, newLine, text: rawLine.slice(1) });
      if (newLine !== null) newLine += 1;
      continue;
    }
    if (rawLine.startsWith("-")) {
      rows.push({ kind: "remove", marker: "-", oldLine, newLine: null, text: rawLine.slice(1) });
      if (oldLine !== null) oldLine += 1;
      continue;
    }
    rows.push({ kind: "context", marker: "", oldLine, newLine, text: rawLine.startsWith(" ") ? rawLine.slice(1) : rawLine });
    if (oldLine !== null) oldLine += 1;
    if (newLine !== null) newLine += 1;
  }
  return rows.length > 0 ? rows : [{ kind: "context", marker: "", oldLine: 1, newLine: 1, text: "" }];
}

/** Prefer preview metadata, then resource labels, for compact preview titles. */
export function previewTitle(preview: NonNullable<ResourceReference["preview"]>, resource?: ResourceReference): string {
  return preview.title ?? resource?.label ?? resource?.target ?? "Preview";
}

/** Infer a useful language label from preview kind, resource path, or title. */
export function previewLanguage(preview: NonNullable<ResourceReference["preview"]>, resource?: ResourceReference): string {
  if (preview.kind === "terminal") return "terminal";
  if (preview.kind === "json") return "json";
  const target = resource?.path ?? resource?.target ?? preview.title ?? "";
  const match = /\.([a-z0-9]+)$/i.exec(target);
  if (!match) return preview.kind === "diff" ? "diff" : "text";
  const ext = match[1].toLowerCase();
  if (ext === "mdx") return "mdx";
  if (ext === "md") return "markdown";
  if (ext === "py") return "python";
  if (ext === "ts" || ext === "tsx") return ext;
  if (ext === "js" || ext === "jsx") return ext;
  if (ext === "yml") return "yaml";
  return ext;
}

/** Show the literal file extension in preview chrome instead of a language name. */
export function previewLanguageLabel(preview: NonNullable<ResourceReference["preview"]>, resource?: ResourceReference): string {
  if (preview.kind === "terminal") return "terminal";
  if (preview.kind === "json") return ".json";
  const target = resource?.path ?? resource?.target ?? preview.title ?? "";
  const match = /\.([a-z0-9]+)$/i.exec(target);
  if (!match) return preview.kind === "diff" ? "diff" : "text";
  return `.${match[1].toLowerCase()}`;
}

function stripDiffPreamble(lines: string[]): string[] {
  const firstHunk = lines.findIndex((line) => HUNK_HEADER.test(line) || line.startsWith("@@ "));
  return firstHunk === -1 ? lines : lines.slice(firstHunk);
}
