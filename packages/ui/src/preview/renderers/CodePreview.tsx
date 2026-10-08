// Source code and JSON, on the plate the rest of the product reads code on.
//
// The grammar comes from the file's own name, so a `.py` sniffed as plain text
// reads as python rather than as a wall of ink. JSON arrives minified as often as
// not, so a small document is laid out before it is shown; a large one is left
// exactly as it came, because re-serializing megabytes to look at them costs more
// than reading them, and a large one arrives in windows no parser could close.

import type { ReactNode } from "react";

import { CodeBlock, langForPath } from "../../primitives/render";
import type { PreviewFacts, PreviewProps, PreviewRenderer } from "../types";
import { previewKindFor } from "../kind";
import { MoreWindowBar, useMoreWindow } from "./MoreWindow";

/** The largest JSON document that gets laid out before it is drawn. */
const JSON_PRETTY_MAX_BYTES = 512 * 1024;

const JSON_MIME = "application/json";

function claims(facts: PreviewFacts): boolean {
  return previewKindFor(facts) === "code";
}

/** The document laid out over lines, or the text untouched when it is past the
 *  ceiling or is not the JSON the server claimed (a truncated file still has to
 *  be readable). */
function laidOut(text: string, size: number): string {
  if (size > JSON_PRETTY_MAX_BYTES) return text;
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

export function CodePreview(props: PreviewProps): ReactNode {
  const tail = useMoreWindow(props.content);
  if (props.content.kind !== "text") return null;
  const { facts } = props;
  const isJson = facts.mime === JSON_MIME || langForPath(facts.name) === "json";
  const code = isJson ? laidOut(props.content.text, facts.size) : props.content.text;
  return (
    <div className="alk-preview-code" onScrollCapture={tail.onScrollCapture}>
      {/* One block for every window that has landed, so the line numbers run on
          across them. The block's own copy would copy only what is here, so a
          file still arriving leaves copying to the host's Copy. */}
      <CodeBlock
        code={code}
        language={isJson ? "json" : undefined}
        path={facts.name}
        lineNumbers
        size="md"
        copyButton={tail.partial === null ? "hover" : "none"}
        aria-label={`Contents of ${facts.name}`}
      />
      <MoreWindowBar tail={tail} />
    </div>
  );
}

export const codeRenderer: PreviewRenderer = {
  id: "code",
  // Under markdown and the spreadsheet reader, over the plain-text reader: a file
  // with a grammar is better read with it than without.
  priority: 30,
  match: claims,
  needs: () => "text",
  Component: CodePreview,
};
