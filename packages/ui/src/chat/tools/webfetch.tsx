// Web fetch: a fetch returns a DOCUMENT, so the card reads like one. The head's
// argument is the URL with its host bright and the rest dimmed, the way an
// address bar ranks the same string; the figure is the status the server
// answered with. The page renders through the shipped markdown model inside the
// card's own document ground, and the footer states how much of the document
// the payload actually carried.

import type { ReactElement, ReactNode } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";

import { Prose } from "../prose";
import { objectOutput, str } from "./alkeraPayload";
import type { CardHead, StepEnvironment } from "./step";
import "./shared.css";
import s from "./webfetch.module.css";

/** A URL ranked like an address bar: the host carries identity, the scheme and
 *  path only locate it. */
function Url({ url }: { url: string }): ReactNode {
  const match = /^([a-z][a-z0-9+.-]*:\/\/)([^/?#]+)(.*)$/i.exec(url);
  if (!match) return <span className="chat-tool-leaf">{url}</span>;
  return (
    <>
      {match[1]}
      <span className="chat-tool-leaf">{match[2]}</span>
      {match[3]}
    </>
  );
}

/** The readable page the tool carried back, plus what the server said about it.
 *  The alkera web tool lands a JSON object; the vendor tool lands plain text. */
function fetched(part: ToolConversationPart): {
  url: string;
  content: string;
  status: number | null;
  total: number | null;
} {
  const result = objectOutput(part.output);
  return {
    url: str(part.input?.url),
    content:
      typeof result?.content === "string" ? result.content : typeof part.output === "string" ? part.output : "",
    status: typeof result?.status === "number" ? result.status : null,
    total: typeof result?.total_chars === "number" ? result.total_chars : null,
  };
}

/** The well's interior: the URL the fetch went to, then the page it came back
 *  with. The URL is a door to the page itself where the host can open one; a
 *  webview cannot open a tab, so unwired it stays a label. It paints no ground,
 *  edge, radius, or outer pad; the group's well owns those. */
function Body({ part, env }: { part: ToolConversationPart; env?: StepEnvironment }): ReactElement {
  const { url, content } = fetched(part);
  const openUrl = env?.onOpenUrl;
  const band = (
    <span className="chat-tool-band__text">
      <Url url={url} />
    </span>
  );
  return (
    <div data-tool="webfetch">
      {openUrl && url ? (
        <button
          type="button"
          className={`chat-tool-band chat-tool-mono ${s.urlDoor}`}
          aria-label={`Open ${url}`}
          onClick={() => openUrl(url)}
        >
          {band}
        </button>
      ) : (
        <div className="chat-tool-band chat-tool-mono">{band}</div>
      )}
      <div className={`${s.doc} chat-tool-doc`}>
        <Prose content={content} />
      </div>
    </div>
  );
}

/** The step this tool contributes to a transcript's tool group. */
export const head: CardHead = (part, env) => {
  const { url, content, status, total } = fetched(part);
  return {
    object: url,
    data: status === null ? undefined : { kind: "count", text: `HTTP ${status}` },
    body: <Body part={part} env={env} />,
    footer:
      total === null
        ? undefined
        : `${content.length.toLocaleString("en-US")} of ${total.toLocaleString("en-US")} characters`,
  };
};
