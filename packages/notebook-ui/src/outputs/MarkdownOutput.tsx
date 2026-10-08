// `text/markdown`: sanitized Markdown with KaTeX math, drawn in the app.

import "katex/dist/katex.min.css";

import { useMemo } from "react";

import { toText } from "./AnsiText";
import { renderMarkdown } from "./markdown";
import type { OutputRenderer, OutputRendererProps } from "./types";

export function MarkdownView({ markdown }: { markdown: string }) {
  // The HTML is the sanitizer's output (an allowlist of tags and attributes,
  // with KaTeX's markup sanitized on its own), never the raw text.
  const html = useMemo(() => renderMarkdown(markdown), [markdown]);
  return <div className="nb-output-markdown" dangerouslySetInnerHTML={{ __html: html }} />;
}

function MarkdownOutput({ data }: OutputRendererProps) {
  return <MarkdownView markdown={toText(data)} />;
}

export const markdownRenderer: OutputRenderer = {
  id: "alkera.markdown",
  mimes: ["text/markdown"],
  rank: 0,
  place: "app",
  Component: MarkdownOutput,
};
