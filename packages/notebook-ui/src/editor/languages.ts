// What a cell's editor knows about its language.
//
// Python cells get Python; SQL cells get SQL plus the two things the format
// adds to it: the body is a raw f-string, so `{expr}` interpolates Python
// (shown as such) and a literal brace is written doubled (`{{`, `}}`); a lone
// unmatched brace is marked, since the file cannot hold it as SQL. Markdown
// cells get Markdown (the cell draws its rendering beside the source).
// Registered by kind, so a new kind is one registration.

import { closeBrackets } from "@codemirror/autocomplete";
import { indentWithTab } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
import { PostgreSQL, sql } from "@codemirror/lang-sql";
import { HighlightStyle, bracketMatching, indentOnInput, syntaxHighlighting } from "@codemirror/language";
import { RangeSetBuilder, type Extension } from "@codemirror/state";
import {
  Decoration,
  EditorView,
  ViewPlugin,
  drawSelection,
  highlightSpecialChars,
  keymap,
  placeholder,
  type DecorationSet,
  type ViewUpdate,
} from "@codemirror/view";

import { tags as t } from "@lezer/highlight";

import type { CellKind } from "../model/types";

/** A cell's syntax colours, as classes the notebook's stylesheet colours from
 *  its tokens. A class rather than a colour, so the code follows a theme
 *  change with the rest of the page and no editor is rebuilt for it. */
export const cellHighlightStyle = HighlightStyle.define([
  { tag: [t.keyword, t.operatorKeyword, t.modifier, t.controlKeyword, t.definitionKeyword, t.moduleKeyword], class: "nb-tok-keyword" },
  { tag: [t.string, t.special(t.string), t.character, t.regexp, t.escape], class: "nb-tok-string" },
  { tag: [t.number, t.bool, t.null, t.atom], class: "nb-tok-number" },
  { tag: [t.comment, t.lineComment, t.blockComment, t.docComment, t.meta], class: "nb-tok-comment" },
  { tag: [t.punctuation, t.bracket, t.operator, t.separator, t.processingInstruction], class: "nb-tok-punct" },
  { tag: [t.name, t.variableName, t.propertyName, t.typeName, t.className, t.function(t.variableName), t.labelName], class: "nb-tok-name" },
  { tag: t.heading, class: "nb-tok-heading" },
  { tag: t.strong, class: "nb-tok-strong" },
  { tag: t.emphasis, class: "nb-tok-emphasis" },
  { tag: [t.link, t.url], class: "nb-tok-link" },
  { tag: t.strikethrough, class: "nb-tok-strike" },
  { tag: t.invalid, class: "nb-tok-invalid" },
]);

export interface BracePart {
  from: number;
  to: number;
  kind: "interpolation" | "literal" | "unmatched";
}

/** The braces of a SQL body read as an f-string: interpolations `{expr}`
 *  (nested braces inside one are part of it), doubled literal braces, and
 *  lone braces the file cannot hold. */
export function sqlBraces(text: string): BracePart[] {
  const out: BracePart[] = [];
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "{" && text[i + 1] === "{") {
      out.push({ from: i, to: i + 2, kind: "literal" });
      i += 2;
      continue;
    }
    if (ch === "}" && text[i + 1] === "}") {
      out.push({ from: i, to: i + 2, kind: "literal" });
      i += 2;
      continue;
    }
    if (ch === "{") {
      let depth = 1;
      let j = i + 1;
      while (j < text.length && depth > 0) {
        if (text[j] === "{") depth += 1;
        else if (text[j] === "}") depth -= 1;
        j += 1;
      }
      if (depth === 0 && j - i > 2) {
        out.push({ from: i, to: j, kind: "interpolation" });
        i = j;
        continue;
      }
      out.push({ from: i, to: i + 1, kind: "unmatched" });
      i += 1;
      continue;
    }
    if (ch === "}") {
      out.push({ from: i, to: i + 1, kind: "unmatched" });
    }
    i += 1;
  }
  return out;
}

const MARKS = {
  interpolation: Decoration.mark({ class: "nb-sql-interp", attributes: { title: "Python value" } }),
  literal: Decoration.mark({ class: "nb-sql-brace", attributes: { title: "A literal brace" } }),
  unmatched: Decoration.mark({ class: "nb-sql-unmatched", attributes: { title: "Write a literal brace twice: {{ or }}" } }),
};

function braceDecorations(view: EditorView): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>();
  for (const part of sqlBraces(view.state.doc.toString())) builder.add(part.from, part.to, MARKS[part.kind]);
  return builder.finish();
}

const sqlBraceMarks = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) {
      this.decorations = braceDecorations(view);
    }
    update(update: ViewUpdate): void {
      if (update.docChanged) this.decorations = braceDecorations(update.view);
    }
  },
  { decorations: (v) => v.decorations },
);

const languages = new Map<string, () => Extension>([
  ["python", () => python()],
  ["setup", () => python()],
  ["function", () => python()],
  ["class", () => python()],
  ["unparsable", () => python()],
  ["sql", () => [sql({ dialect: PostgreSQL, upperCaseKeywords: true }), sqlBraceMarks]],
  ["markdown", () => markdown()],
]);

/** Teach cells of a kind their language. */
export function registerCellLanguage(kind: string, extension: () => Extension): void {
  languages.set(kind, extension);
}

/** A kind this build does not know is edited as Python. */
export function languageFor(kind: CellKind): Extension {
  return (languages.get(kind) ?? languages.get("python")!)();
}

const PLACEHOLDER: Record<string, string> = {
  python: "Python",
  sql: "SELECT ...",
  markdown: "Markdown",
  setup: "Imports the other cells share",
};

/** The editing basics every cell has, whatever its language. */
export function cellBasics(kind: CellKind): Extension {
  return [
    highlightSpecialChars(),
    drawSelection(),
    indentOnInput(),
    bracketMatching(),
    closeBrackets(),
    syntaxHighlighting(cellHighlightStyle),
    EditorView.lineWrapping,
    placeholder(PLACEHOLDER[kind] ?? ""),
    keymap.of([indentWithTab]),
    EditorView.contentAttributes.of({ "aria-label": `${kind === "sql" ? "SQL" : kind === "markdown" ? "Markdown" : "Python"} cell` }),
  ];
}
