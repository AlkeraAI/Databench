// A markdown file, read the way its author wrote it.
//
// The document model is the shared one every transcript uses — the same block
// parser, the same inline pass, the same chat-folder image and file links — so a
// note reads identically beside a chat and on the Files page. Two things differ
// here, because a previewed file is bytes someone else produced rather than a
// message this app composed:
//
//  - A link to another site is an `<a target="_blank" rel="noopener noreferrer">`
//    with a visible mark, not a routed button. The reader can see where it goes
//    and the page it opens gets no handle on this one.
//  - Nothing in the file ever becomes markup. Raw HTML in the source stays the
//    text it is (React escapes it), and a display-math block renders as its own
//    source rather than through a formatter's innerHTML.

import { Fragment, type ReactNode } from "react";

import {
  ChatFilesProvider,
  ChatImage,
  parseMarkdownBlocks,
  renderInline,
  type MarkdownBlock,
} from "../../primitives/render";
import type { PreviewFacts, PreviewProps, PreviewRenderer } from "../types";
import { previewKindFor } from "../kind";
import { MoreWindowBar, useMoreWindow } from "./MoreWindow";

/** Mimes a server hands back for markdown outright. */

/** A plain-text file whose name says markdown. The mime decides first; the name
 *  only picks between the readers that share `text/plain`. */

/** A link out of this page. Only `http`/`https` — a `mailto:`, a `javascript:`
 *  or anything else keeps the shared renderer's inert treatment. */
const EXTERNAL_LINK = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;

/** The type a server gives bytes it could not place. A note is still a note when
 *  the sniffer had nothing to say about it — an older version stored before the
 *  sniffer learned to call quoted markup text still reads `.md` — so the name is
 *  allowed to decide here, where it decides nothing else. */

function claims(facts: PreviewFacts): boolean {
  return previewKindFor(facts) === "markdown";
}

/** One link to another site: where it goes is visible, and the tab it opens
 *  cannot reach back through `window.opener` or carry this URL as a referrer. */
function ExternalLink({ label, href }: { label: string; href: string }): ReactNode {
  return (
    <a
      className="alk-markdown-link alk-preview-markdown__external"
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={href}
      aria-label={`${label} (opens in a new tab)`}
    >
      {label}
      <span className="alk-preview-markdown__external-marker" aria-hidden>
        ↗
      </span>
    </a>
  );
}

/** The shared inline pass, with links to other sites lifted out of it. Everything
 *  between them — emphasis, code spans, chat-folder links and images — is rendered
 *  by the one inline renderer the transcript uses. */
function inlineNodes(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const shared = (slice: string, offset: number): void => {
    if (slice === "") return;
    nodes.push(
      <Fragment key={`${keyPrefix}-${offset}-text`}>
        {renderInline(slice, undefined, undefined, undefined)}
      </Fragment>,
    );
  };
  let cursor = 0;
  const pattern = new RegExp(EXTERNAL_LINK.source, "g");
  for (let match = pattern.exec(text); match !== null; match = pattern.exec(text)) {
    // `![alt](https://…)` is an image the block parser declined to make — it is
    // not a link to that target either, so it goes back through the shared pass
    // and stays the text the author wrote.
    if (match.index > 0 && text[match.index - 1] === "!") continue;
    shared(text.slice(cursor, match.index), cursor);
    nodes.push(<ExternalLink key={`${keyPrefix}-${match.index}-link`} label={match[1]} href={match[2]} />);
    cursor = match.index + match[0].length;
  }
  shared(text.slice(cursor), cursor);
  return nodes;
}

function renderBlock(block: MarkdownBlock, index: number): ReactNode {
  const inline = (text: string): ReactNode[] => inlineNodes(text, String(index));
  switch (block.kind) {
    case "heading": {
      const Heading = `h${block.level}` as "h1" | "h2" | "h3";
      return <Heading key={index}>{inline(block.text)}</Heading>;
    }
    case "quote":
      return <blockquote key={index}>{inline(block.text)}</blockquote>;
    case "code":
      return (
        <pre key={index} className="alk-scroll">
          <code>{block.text}</code>
        </pre>
      );
    case "math":
      // The source, not a rendering of it: a formatter's output would have to be
      // written into this page as markup, and a previewed file is never trusted
      // that far.
      return (
        <pre key={index} className="alk-preview-markdown__math">
          <code>{block.text}</code>
        </pre>
      );
    case "list": {
      const List = block.ordered ? "ol" : "ul";
      return (
        <List key={index}>
          {block.items.map((item, itemIndex) => (
            <li key={`${index}-${itemIndex}`}>{inline(item)}</li>
          ))}
        </List>
      );
    }
    case "image":
      return <ChatImage key={index} alt={block.alt} path={block.path} />;
    case "blob":
      // A tool-result handle points at nothing outside the chat that produced it,
      // so in a file it is just the name it spells.
      return <p key={index}>{block.name}</p>;
    case "table":
      return (
        <div key={index} className="alk-markdown-table-wrap alk-scroll">
          <table>
            <thead>
              <tr>
                {block.headers.map((header, headerIndex) => (
                  <th key={`${index}-h-${headerIndex}`}>{inline(header)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={`${index}-${rowIndex}`}>
                  {block.headers.map((_header, cellIndex) => (
                    <td key={`${index}-${rowIndex}-${cellIndex}`}>{inline(row[cellIndex] ?? "")}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    default:
      return <p key={index}>{inline(block.text)}</p>;
  }
}

export function MarkdownPreview(props: PreviewProps): ReactNode {
  const tail = useMoreWindow(props.content);
  if (props.content.kind !== "text") return null;
  // The whole of what has landed is parsed as one document, so a window is never
  // a document of its own: a link or a heading reads the same whichever window
  // brought it. The host cuts windows at blank lines, so no block is half here.
  const blocks = parseMarkdownBlocks(props.content.text);
  return (
    <ChatFilesProvider resolver={props.resolver ?? null}>
      <div className="alk-markdown alk-preview-markdown" onScrollCapture={tail.onScrollCapture}>
        {blocks.map((block, index) => renderBlock(block, index))}
        <MoreWindowBar tail={tail} />
      </div>
    </ChatFilesProvider>
  );
}

export const markdownRenderer: PreviewRenderer = {
  id: "markdown",
  // Above the code and plain-text readers: a `.md` file has a grammar and is
  // text, and both of those would otherwise claim it.
  priority: 50,
  match: claims,
  needs: () => "text",
  Component: MarkdownPreview,
};
