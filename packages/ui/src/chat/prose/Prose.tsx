// The shared prose renderer: agent markdown through the shipped block model
// (@alkera/ui's parser + inline renderer), every block kind in the design's
// visual voice. While streaming, the live block's new words ease in; no cursor
// marks the edge.

import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import {
  ChatFilesStreaming,
  ChatImage,
  ChatResultLink,
  LanguageIcon,
  ReferenceList,
  parseMarkdownBlocks,
  renderInline,
  renderMathHtml,
  useReferenceActions,
  type MarkdownBlock,
  type ResourceReference,
} from "../sharedUi";
import { IconCheck, IconCopy } from "@tabler/icons-react";

import { StreamText } from "../indicators";
import { codeLeaves, languageFor, paintLeaves } from "../syntax";
import "katex/dist/katex.min.css";
import s from "./prose.module.css";

export interface ProseProps {
  /** Markdown source, the shape production hands every prose surface. */
  content: string;
  /** Render only the first N parsed blocks -- a preview's clip. */
  maxBlocks?: number;
  /** Ease the last block's arriving words in while the host streams into
   *  `content`. */
  streaming?: boolean;
  /** Known resources for inline link routing; an unknown path still resolves. */
  resources?: ResourceReference[];
  onLinkClick?: (path: string) => void;
  onResourceOpen?: (resource: ResourceReference) => void;
}

// The copy control is always visible and flips to a checkmark for a short beat
// after a real clipboard write, then reverts.
function Fence({ language, code }: { language: string; code: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);
  const copy = () => {
    void navigator.clipboard?.writeText(code).catch(() => undefined);
    setCopied(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 1400);
  };
  // Tokenizing is the fence's real cost; at streaming cadence it must not
  // re-run for renders whose code did not change.
  const painted = useMemo(() => paintLeaves(codeLeaves(code, languageFor(language))), [code, language]);
  return (
    <figure className={s.fence} data-lang={language}>
      <figcaption className={s.fenceCap}>
        <span className={s.fenceFile}>
          <span className={s.fenceIcon}>
            <LanguageIcon language={language} size={16} />
          </span>
          <span className={s.fenceLang}>{language}</span>
        </span>
        <button
          type="button"
          className={s.fenceCopy}
          data-copied={copied ? "" : undefined}
          onClick={copy}
          aria-label={copied ? "Copied" : "Copy code"}
        >
          {copied ? (
            <IconCheck className={s.fenceCopyicon} size={15} stroke={1.8} aria-hidden />
          ) : (
            <IconCopy className={s.fenceCopyicon} size={15} stroke={1.8} aria-hidden />
          )}
        </button>
      </figcaption>
      <pre className={`${s.fenceCode} chat-mono`}><code>{painted}</code></pre>
    </figure>
  );
}

/** The live block's inline stream: plain text runs ease in word by word on
 *  stable keys; a formatted run (code, emphasis, a link) arrives whole. */
function streamInline(nodes: ReactNode[]): ReactNode[] {
  return nodes.map((node, i) => (typeof node === "string" ? <StreamText key={`w${i}`} text={node} /> : node));
}


/** An own-line `[label](blob:<handle>)`. Where the shell can open the held
 *  result itself (the editor, whose machine holds it) it is the rich card.
 *  Where it cannot (the web: the result stayed on the machine that made it)
 *  the card would be a chip that answers a click with nothing, so the
 *  reference opens the file the agent wrote the result out to, or reads as its
 *  label when there is none. */
function BlobBlock({ handle, name }: { handle: string; name: string }): ReactNode {
  const opens = useReferenceActions((state) => state.onOpen);
  if (opens) return <ReferenceList references={[{ handle, name }]} variant="card" />;
  return (
    <p className={s.p}>
      <ChatResultLink label={name} handle={handle} title={`blob:${handle}`} />
    </p>
  );
}

/** A list, with each item's sub-lists rendered inside its own `<li>`: a list
 *  indented under an item is that item's, so it indents under it rather than
 *  being dropped or flattened beside it. */
function renderList(
  block: Extract<MarkdownBlock, { kind: "list" }>,
  key: number | string,
  inline: (text: string) => ReactNode,
): ReactNode {
  const nested = block.nested ?? [];
  const items = block.items.map((item, j) => (
    <li key={j}>
      {inline(item)}
      {(nested[j] ?? []).map((sub, k) => renderList(sub, `${key}-${j}-${k}`, inline))}
    </li>
  ));
  return block.ordered ? (
    <ol key={key} className={`${s.list} ${s.listOrdered}`}>{items}</ol>
  ) : (
    <ul key={key} className={s.list}>{items}</ul>
  );
}

function renderBlock(
  block: MarkdownBlock,
  key: number,
  inline: (text: string) => ReactNode[],
  live: boolean,
): ReactNode {
  switch (block.kind) {
    case "heading": {
      const Heading = `h${block.level}` as "h1" | "h2" | "h3";
      return <Heading key={key} className={s.heading} data-level={block.level}>{inline(block.text)}</Heading>;
    }
    case "paragraph":
      return (
        <p key={key} className={s.p}>
          {live ? streamInline(inline(block.text)) : inline(block.text)}
        </p>
      );
    case "quote":
      return <blockquote key={key} className={s.quote}>{inline(block.text)}</blockquote>;
    case "code":
      return <Fence key={key} language={block.language ?? "text"} code={block.text} />;
    case "math":
      return (
        <div
          key={key}
          className={s.math}
          dangerouslySetInnerHTML={{ __html: renderMathHtml(block.text, true) }}
        />
      );
    case "list":
      return renderList(block, key, inline);
    case "table":
      return (
        <div key={key} className={s.tableWrap}>
          <table className={s.table}>
            <thead>
              <tr>{block.headers.map((header, j) => <th key={j}>{inline(header)}</th>)}</tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {block.headers.map((_, cellIndex) => <td key={cellIndex}>{inline(row[cellIndex] ?? "")}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "blob":
      return <BlobBlock key={key} handle={block.handle} name={block.name} />;
    case "image":
      return <ChatImage key={key} alt={block.alt} path={block.path} />;
  }
  return block satisfies never;
}

export function Prose({ content, maxBlocks, streaming, resources, onLinkClick, onResourceOpen }: ProseProps) {
  // The whole-transcript markdown parse is the streaming hot path; hold it on
  // the content string.
  const parsed = useMemo(() => parseMarkdownBlocks(content), [content]);
  const blocks = maxBlocks === undefined ? parsed : parsed.slice(0, maxBlocks);
  const inline = (text: string) => renderInline(text, resources, onLinkClick, onResourceOpen);
  // The files this message names may still be on their way only while it is
  // being written; once it is finished, one the chat does not have says so.
  return (
    <ChatFilesStreaming streaming={streaming === true}>
      <div className={s.prose}>
        {blocks.map((block, i) => renderBlock(block, i, inline, streaming === true && i === blocks.length - 1))}
      </div>
    </ChatFilesStreaming>
  );
}
