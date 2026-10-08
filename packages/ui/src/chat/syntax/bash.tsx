// Bash commands render through the shipped shell tokenizer, which owns
// command-position tracking (`uv run pytest` reads as command words, not one).
// This module only maps its kinds onto the `chat-btok--*` palette: numbers wear
// the string ink, variables the flag ink, and an argument holding a path takes
// the muted path ink -- other args stay plain.

import type { ReactNode } from "react";

import { tokenizeShell, type ShellSeg } from "../sharedUi";

import "./syntax.css";

type BashKind = "command" | "flag" | "string" | "path" | "comment" | "operator";

function bashKind(seg: ShellSeg): BashKind | null {
  switch (seg.kind) {
    case "command":
      return "command";
    case "flag":
    case "variable":
      return "flag";
    case "string":
    case "number":
      return "string";
    case "comment":
      return "comment";
    case "operator":
      return "operator";
    case "arg":
      return seg.text.includes("/") ? "path" : null;
    default:
      return null;
  }
}

export function highlightBash(command: string): ReactNode[] {
  return tokenizeShell(command).map((seg, i) => {
    const kind = bashKind(seg);
    return kind ? (
      <span key={i} className={`chat-btok--${kind}`}>
        {seg.text}
      </span>
    ) : (
      <span key={i}>{seg.text}</span>
    );
  });
}
