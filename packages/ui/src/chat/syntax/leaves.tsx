// The one bridge from the shipped tokenizers to the design's syntax classes:
// @alkera/ui's Prism flatten emits `alk-tok--*` class chains, a closed kind map
// names the kinds this package colors, and `paintLeaves` renders them to the
// `chat-tok--*` spans syntax.css styles. No class reaches the DOM that the sheet
// does not color.

import type { ReactNode } from "react";

import { tokenizeInline, tokenizeToLines, type CodeSeg } from "../sharedUi";

import "./syntax.css";

export interface SyntaxLeaf<K extends string = string> {
  text: string;
  kind: K | null;
}

/** The deepest recognized Prism type wins: the class chain carries ancestors
 *  first, so the scan runs from the leaf end. */
function leafKind<K extends string>(seg: CodeSeg, kinds: Record<string, K>): K | null {
  const classes = seg.classes.split(" ");
  for (let i = classes.length - 1; i >= 0; i -= 1) {
    const type = classes[i].replace(/^alk-tok--/, "");
    if (type in kinds) return kinds[type];
  }
  return null;
}

export function tokenLeaves<K extends string>(
  code: string,
  language: string | null,
  kinds: Record<string, K>,
): SyntaxLeaf<K>[] {
  return tokenizeInline(code, language).map((seg) => ({ text: seg.text, kind: leafKind(seg, kinds) }));
}

/** Tokenized whole, split into lines after, so a string or comment spanning
 *  lines reads as one token rather than restarting each row. */
export function tokenLines<K extends string>(
  code: string,
  language: string | null,
  kinds: Record<string, K>,
): SyntaxLeaf<K>[][] {
  return tokenizeToLines(code, language).map((line) =>
    line.map((seg) => ({ text: seg.text, kind: leafKind(seg, kinds) })),
  );
}

/** Render leaves to `chat-tok--<kind>` spans; an unkinded leaf stays plain ink. */
export function paintLeaves(leaves: SyntaxLeaf[]): ReactNode[] {
  return leaves.map((leaf, i) =>
    leaf.kind ? (
      <span key={i} className={`chat-tok--${leaf.kind}`}>
        {leaf.text}
      </span>
    ) : (
      <span key={i}>{leaf.text}</span>
    ),
  );
}
