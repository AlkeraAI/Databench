import { Fragment, type ReactNode } from "react";

import { langForExtension, tokenizeInline } from "../highlight";

// The resource preview names its language loosely (a file extension, a grammar name, or
// "text"/"diff"/"terminal"); the shared extension map resolves it, else the line renders plain.

/** Highlight one preview line with the shared Prism engine, emitting the same `alk-tok--*` classes as
 *  CodeBlock so the preview reads in the one warm code palette. Terminal/diff rows render plain. */
export function renderHighlightedCode(line: string, language: string): ReactNode[] {
  if (language === "terminal" || language === "diff") return [line];
  const grammar = langForExtension(language);
  const segs = tokenizeInline(line, grammar);
  if (segs.length === 0) return [line];
  return segs.map((s, i) =>
    s.classes ? (
      <span key={i} className={s.classes}>
        {s.text}
      </span>
    ) : (
      <Fragment key={i}>{s.text}</Fragment>
    ),
  );
}
