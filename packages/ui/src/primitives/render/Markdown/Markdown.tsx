// The chat markdown renderer: the block subset the transcript and tool cards need
// (headings, lists, quotes, fenced code, tables, math, blob reference cards) with
// resource-link routing. Renders to React nodes only — never raw HTML passthrough
// (the one dangerouslySetInnerHTML is KaTeX's own sanitized output).

import type { ReactNode } from "react";

import type { ResourceReference } from "../../../types";
import { LanguageIcon } from "../resources";
import { ChatFilesStreaming, ChatImage, ChatResultLink } from "./ChatFiles";
import { parseMarkdownBlocks, type MarkdownBlock, type MarkdownListBlock } from "./markdownBlocks";
import { renderInline, renderMathHtml } from "./markdownInline";
import "katex/dist/katex.min.css";
import "./markdown.css";

export interface MarkdownProps {
  content: string;
  resources?: ResourceReference[];
  onLinkClick?: (path: string) => void;
  onResourceOpen?: (resource: ResourceReference) => void;
  streaming?: boolean;
}

export function Markdown({
  content,
  resources,
  onLinkClick,
  onResourceOpen,
  streaming = false,
}: MarkdownProps) {
  const blocks = parseMarkdownBlocks(content);
  return (
    <ChatFilesStreaming streaming={streaming}>
      <div className="alk-markdown">
        {blocks.map((block, index) => renderBlock(block, index, resources, onLinkClick, onResourceOpen))}
        {streaming ? <span className="alk-stream-caret" aria-hidden /> : null}
      </div>
    </ChatFilesStreaming>
  );
}

function renderBlock(
  block: MarkdownBlock,
  index: number,
  resources: ResourceReference[] | undefined,
  onLinkClick: ((path: string) => void) | undefined,
  onResourceOpen: ((resource: ResourceReference) => void) | undefined,
) {
  const inline = (text: string) => renderInline(text, resources, onLinkClick, onResourceOpen);
  if (block.kind === "heading") {
    const Heading = `h${block.level}` as "h1" | "h2" | "h3";
    return <Heading key={index}>{inline(block.text)}</Heading>;
  }
  if (block.kind === "quote") {
    return <blockquote key={index}>{inline(block.text)}</blockquote>;
  }
  if (block.kind === "code") {
    const language = block.language ?? "text";
    return (
      <figure key={index} className="alk-markdown-code">
        <figcaption>
          <span className="alk-markdown-code__icon" aria-hidden>
            <LanguageIcon language={language} size="var(--alkIconSm)" />
          </span>
          <span>{language}</span>
        </figcaption>
        <pre className="alk-scroll"><code>{block.text}</code></pre>
      </figure>
    );
  }
  if (block.kind === "math") {
    return (
      <div
        key={index}
        className="alk-markdown-math"
        dangerouslySetInnerHTML={{ __html: renderMathHtml(block.text, true) }}
      />
    );
  }
  if (block.kind === "list") {
    return renderList(block, index, inline);
  }
  if (block.kind === "image") {
    return <ChatImage key={index} alt={block.alt} path={block.path} />;
  }
  if (block.kind === "blob") {
    // An own-line blob reference. This renderer holds no blob store, so it can
    // never open the result itself: where the result was written out to a file
    // in the chat the reference opens that file, and otherwise it is the label
    // — never a chip that looks like it opens and does not.
    return (
      <p key={index} className="alk-markdown-blob">
        <ChatResultLink label={block.name} handle={block.handle} title={`blob:${block.handle}`} />
      </p>
    );
  }
  if (block.kind === "table") {
    return (
      <div key={index} className="alk-markdown-table-wrap alk-scroll">
        <table>
          <thead>
            <tr>{block.headers.map((header) => <th key={header}>{inline(header)}</th>)}</tr>
          </thead>
          <tbody>
            {block.rows.map((row, rowIndex) => (
              <tr key={`${index}-${rowIndex}`}>
                {block.headers.map((header, cellIndex) => (
                  <td key={`${header}-${cellIndex}`}>{inline(row[cellIndex] ?? "")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  return <p key={index}>{inline(block.text)}</p>;
}

/** A list, with each item's sub-lists rendered inside its own `<li>`. */
function renderList(block: MarkdownListBlock, key: number | string, inline: (text: string) => ReactNode) {
  const List = block.ordered ? "ol" : "ul";
  return (
    <List key={key}>
      {block.items.map((item, itemIndex) => (
        <li key={`${key}-${itemIndex}`}>
          {inline(item)}
          {(block.nested?.[itemIndex] ?? []).map((sub, subIndex) =>
            renderList(sub, `${key}-${itemIndex}-${subIndex}`, inline),
          )}
        </li>
      ))}
    </List>
  );
}
