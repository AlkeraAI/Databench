// Inline markdown rendering: links (routed through ResourceReference), bold,
// emphasis, inline code, and inline math via KaTeX. Never trusts raw HTML.
//
// Single-`*`/`_` emphasis is deliberately NARROW, because this domain's prose
// is full of `select *`, `count(*)` and snake_case identifiers that a greedy
// rule would mangle. A run only emphasizes when the opening mark sits at the
// start of the text or after whitespace, hugs its content on both sides, and
// the closing mark is followed by whitespace, end of text, or closing
// punctuation — or, for `*`, a slash on either side of a wordy run
// (`*a*/*b*`, see `emphasized`). Anything else renders verbatim. Slack's
// outbound translator (`to_mrkdwn`) applies the same rule, so a reply reads
// the same in both places.

import type { ReactNode } from "react";
import katex from "katex";

import type { ResourceReference } from "../../../types";
import { ChatFileLink, ChatResultLink } from "./ChatFiles";

// An emphasis run may hold whole code spans, and a code span binds tighter
// than emphasis (as in CommonMark): the marks inside `a**b` never close a bold
// run around it. A lone backtick in an emphasis run is still plain text.
//
// Inline math follows Pandoc's rule, because prose about money is full of
// dollar signs: the opening `$` must be followed at once by a non-space, the
// closing `$` preceded at once by a non-space and not followed by a digit.
// "$22.82 vs $3.04" and "$5–$10" are therefore prices, not math. `\$` is a
// literal dollar sign.
const INLINE_PATTERN =
  /\[([^\]]+)\]\(([^)]+)\)|\*\*((?:`[^`]+`|[^*`]|`)+)\*\*|`([^`]+)`|\$([^$\s](?:[^$\n]*[^$\s])?)\$(?!\d)|\*((?:`[^`\n]+`|[^*`\n]|`)+)\*|_((?:`[^`\n]+`|[^_`\n]|`)+)_|(\\\$)/g;

/** An emphasis run's content: its code spans render as code, everything
 *  else — marks included — as the text it is. */
function withCodeSpans(run: string, key: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let cursor = 0;
  const pattern = /`([^`]+)`/g;
  for (let match = pattern.exec(run); match !== null; match = pattern.exec(run)) {
    if (match.index > cursor) nodes.push(run.slice(cursor, match.index));
    nodes.push(<code key={`${key}-${match.index}-code`}>{match[1]}</code>);
    cursor = match.index + match[0].length;
  }
  if (cursor < run.length) nodes.push(run.slice(cursor));
  return nodes;
}

/** Punctuation a closing emphasis mark may butt against. */
const AFTER_EMPHASIS = /^[\s.,!?;:)\]}"'’]/;

/** A run that starts and ends on a letter or digit: the only kind a slash may
 *  sit against (see `emphasized`). */
const WORDY_RUN = /^[\p{L}\p{N}](?:.*[\p{L}\p{N}])?$/u;

/** Whether a `*run*` / `_run_` match is really emphasis rather than two
 *  unrelated marks the prose happens to contain. `text` is the whole line and
 *  `index` the offset of the opening mark, so the rule can read what sits on
 *  either side of the run.
 *
 *  A `*` run may also sit against a slash: three words each wrapped in stars
 *  and joined by slashes are alternatives set side by side, each emphasized,
 *  and CommonMark reads them that way too (a slash is punctuation, which flanks
 *  a mark). Only a run that starts and ends on a letter or digit, so a glob
 *  whose stars sit between slashes stays the text it is. `_` gets no such
 *  allowance: `a/_private_/b` is a path far more often than it is emphasis. */
export function emphasized(text: string, index: number, run: string, matchLength: number): boolean {
  const mark = text[index];
  const before = index === 0 ? "" : (text[index - 1] ?? "");
  const after = text.slice(index + matchLength);
  const slashed = mark === "*" && (before === "/" || after.startsWith("/"));
  if (slashed && !WORDY_RUN.test(run)) return false;
  if (before !== "" && !/\s/.test(before) && !(slashed && before === "/")) return false;
  if (/^\s|\s$/.test(run)) return false;
  return after === "" || AFTER_EMPHASIS.test(after) || (slashed && after.startsWith("/"));
}

/** Whether a link target is a `blob:` URL — a content handle the agent emits to
 *  reference a tool result. It points to nothing a browser can open, so it must
 *  never render as a navigable hyperlink. */
function isBlobUrl(url: string): boolean {
  return /^blob:(?:\/\/)?/.test(url);
}

/** The handle a `blob:` link names (`blob:<sha>` or the tolerated `blob://<sha>`). */
function blobHandle(url: string): string {
  return url.replace(/^blob:(?:\/\/)?/, "");
}

/** Build a resource reference from a markdown path target. Local to the renderer —
 *  promote beside the resource model if a second consumer appears. */
export function resourceFromPath(label: string, path: string): ResourceReference {
  const isExternal = /^https?:\/\//i.test(path);
  return {
    kind: isExternal ? "external" : "file",
    label,
    target: path,
    path: isExternal ? undefined : path,
  };
}

/** The links in one run of inline text that `renderInline` opens as chat
 *  files (`ChatFileLink`), in order: every `[label](target)` the tokenizer
 *  reads as a link that is not written as an image, not a `blob:` handle and
 *  not a web URL. Which of them are files in THIS chat is the path rule's call
 *  (`chatRelativePath`). */
export function inlineFileLinks(text: string): { label: string; target: string }[] {
  const out: { label: string; target: string }[] = [];
  const pattern = new RegExp(INLINE_PATTERN.source, "g");
  for (let match = pattern.exec(text); match !== null; match = pattern.exec(text)) {
    if (match[1] === undefined || match[2] === undefined) continue;
    if (match.index > 0 && text[match.index - 1] === "!") continue;
    if (isBlobUrl(match[2])) continue;
    if (resourceFromPath(match[1], match[2]).kind !== "file") continue;
    out.push({ label: match[1], target: match[2] });
  }
  return out;
}

export function renderInline(
  text: string,
  resources: ResourceReference[] | undefined,
  onLinkClick: ((path: string) => void) | undefined,
  onResourceOpen: ((resource: ResourceReference) => void) | undefined,
): ReactNode[] {
  const nodes: ReactNode[] = [];
  let cursor = 0;
  const pattern = new RegExp(INLINE_PATTERN.source, "g");
  for (let match = pattern.exec(text); match !== null; match = pattern.exec(text)) {
    if (match.index > cursor) nodes.push(text.slice(cursor, match.index));
    if (match[1] !== undefined && match[2] !== undefined) {
      const label = match[1];
      const url = match[2];
      if (match.index > 0 && text[match.index - 1] === "!") {
        // `![alt](target)` that the block parser did NOT make an image — a web
        // URL, a data URI, an escape, or one buried mid-sentence — is not a
        // link to that target either. It stays the text the model wrote.
        nodes.push(match[0]);
      } else if (isBlobUrl(url)) {
        // A blob: link is never a navigable hyperlink. Where the result was
        // written out to a file in the chat it opens that file; otherwise it is
        // the label as plain styled text rather than a button to nothing. The
        // rich, paginated blob preview lives in the reference chips beneath a
        // tool card.
        nodes.push(
          <ChatResultLink key={`${match.index}-blobref`} label={label} handle={blobHandle(url)} title={url} />,
        );
      } else {
        const resource = resources?.find((ref) => ref.target === url || ref.path === url || ref.label === label)
          ?? resourceFromPath(label, url);
        const open = onResourceOpen || onLinkClick
          ? () => {
              if (onResourceOpen) onResourceOpen(resource);
              else onLinkClick?.(resource.path ?? resource.target);
            }
          : undefined;
        if (resource.kind === "file") {
          // A path may name a file inside the chat folder (a pasted upload, a
          // produced report, the box's absolute path to either). The shell's
          // resolver decides; a path it cannot open is its label.
          nodes.push(<ChatFileLink key={`${match.index}-link`} label={label} target={url} onOpen={open} />);
        } else {
          nodes.push(
            <button
              key={`${match.index}-link`}
              type="button"
              className="alk-markdown-link"
              onClick={open}
              title={resource.target}
            >
              {label}
            </button>,
          );
        }
      }
    } else if (match[3] !== undefined) {
      nodes.push(<strong key={`${match.index}-strong`}>{withCodeSpans(match[3], `${match.index}-strong`)}</strong>);
    } else if (match[4] !== undefined) {
      nodes.push(<code key={`${match.index}-code`}>{match[4]}</code>);
    } else if (match[8] !== undefined) {
      nodes.push("$");
    } else if (match[5] !== undefined) {
      nodes.push(
        <span
          key={`${match.index}-math`}
          className="alk-markdown-inline-math"
          dangerouslySetInnerHTML={{ __html: renderMathHtml(match[5], false) }}
        />,
      );
    } else {
      const run = match[6] ?? match[7];
      // A run that fails the narrow rule is not emphasis: the marks are the
      // prose's own (`count(*) … count(*)`, `snake_case`), so it goes back out
      // verbatim, marks included.
      if (run !== undefined && emphasized(text, match.index, run, match[0].length)) {
        nodes.push(<em key={`${match.index}-em`}>{withCodeSpans(run, `${match.index}-em`)}</em>);
      } else {
        nodes.push(match[0]);
      }
    }
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

/** Render TeX through KaTeX without trusting model-provided HTML-like commands. */
export function renderMathHtml(text: string, displayMode: boolean): string {
  return katex.renderToString(text, {
    displayMode,
    throwOnError: false,
    trust: false,
    strict: "warn",
  });
}
