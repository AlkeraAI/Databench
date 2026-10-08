// The generic code vocabulary: which Prism types the package colors, and the
// one seam that decides a grammar for a file path or a fence tag.

import { langForPath } from "../sharedUi";

import { tokenLeaves, tokenLines, type SyntaxLeaf } from "./leaves";

export type CodeKind = "keyword" | "function" | "string" | "number" | "comment" | "operator";

const CODE_KIND: Record<string, CodeKind> = {
  keyword: "keyword",
  boolean: "keyword",
  function: "function",
  builtin: "function",
  "class-name": "function",
  decorator: "function",
  string: "string",
  char: "string",
  "template-string": "string",
  "triple-quoted-string": "string",
  number: "number",
  comment: "comment",
  operator: "operator",
  punctuation: "operator",
};

/** Grammar ids the extension table maps to but does not key. */
const GRAMMAR_IDS = new Set(["javascript", "typescript", "python", "markup"]);

/** One decision for "what language is this": a path resolves by its extension,
 *  a fence tag by itself or as an extension alias. Null renders plain ink. */
export function languageFor(source: string): string | null {
  const tag = source.trim().toLowerCase();
  if (GRAMMAR_IDS.has(tag)) return tag;
  return langForPath(tag) ?? langForPath(`file.${tag}`);
}

export function codeLeaves(code: string, language: string | null): SyntaxLeaf<CodeKind>[] {
  return tokenLeaves(code, language, CODE_KIND);
}

export function codeLines(code: string, language: string | null): SyntaxLeaf<CodeKind>[][] {
  return tokenLines(code, language, CODE_KIND);
}
