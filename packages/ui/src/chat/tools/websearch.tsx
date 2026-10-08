// Web search: a search returns RANKED SOURCES, so the card ranks them. Each
// result keeps its rank on a numeral spine, its title is the product's own link
// (UrlLink, so a webview host can route the click), the site sits under it as
// the locator, and the engine's snippet follows. Every field is on the wire.

import type { ReactElement } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";
import { Text, UrlLink } from "../sharedUi";
import { IconExternalLink } from "@tabler/icons-react";

import { count, objectOutput, str } from "./alkeraPayload";
import { Band, Capped, EmptyLine } from "./shared";
import type { CardHead, StepEnvironment } from "./step";
import "./shared.css";
import s from "./websearch.module.css";

interface Hit {
  title: string;
  url: string;
  snippet: string;
}

/** Read defensively: a partial payload can omit any field, and a result with no
 *  title still renders under its URL. */
function hitsOf(output: ToolConversationPart["output"]): Hit[] {
  const rows = objectOutput(output)?.results;
  if (!Array.isArray(rows)) return [];
  return rows.flatMap((row) => {
    if (!row || typeof row !== "object") return [];
    const record = row as Record<string, unknown>;
    const url = typeof record.url === "string" ? record.url : "";
    const title = typeof record.title === "string" && record.title ? record.title : url;
    const snippet = typeof record.snippet === "string" ? record.snippet : "";
    return title || url ? [{ title, url, snippet }] : [];
  });
}

/** The site a result came from. A malformed URL keeps its raw string rather than
 *  disappearing. */
function siteOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

/** The well's interior: the query that ran, then the sources it ranked. It
 *  paints no ground, edge, radius, or outer pad; the group's well owns those. */
function Body({ part, env }: { part: ToolConversationPart; env?: StepEnvironment }): ReactElement {
  const query = str(part.input?.query);
  const hits = hitsOf(part.output);
  return (
    <div data-tool="websearch">
      <Band>
        <span className="chat-tool-band__text">{query}</span>
      </Band>
      {hits.length === 0 ? (
        <EmptyLine>No results came back for this query.</EmptyLine>
      ) : (
        <Capped cap={440} className="chat-tool-ruled">
          {hits.map((hit, i) => (
            <div key={i} className={s.hit}>
              <span className={s.hitRank} aria-hidden="true">
                {i + 1}
              </span>
              <div className={s.hitBody}>
                <p className={s.hitTitle}>
                  {hit.url ? (
                    <UrlLink className={s.link} url={hit.url} onOpen={env?.onOpenUrl}>
                      {hit.title}
                    </UrlLink>
                  ) : (
                    hit.title
                  )}
                </p>
                <p className={s.hitSite}>
                  {/* The site line is a second door to the same source: the
                      mark beside it reads as an action, so it is one. */}
                  {hit.url ? (
                    <UrlLink className={s.siteLink} url={hit.url} onOpen={env?.onOpenUrl}>
                      <Text className="chat-tool-clip" tooltip="truncate">{siteOf(hit.url)}</Text>
                      <IconExternalLink size={12} stroke={1.8} aria-hidden="true" />
                    </UrlLink>
                  ) : (
                    <Text className="chat-tool-clip" tooltip="truncate">{siteOf(hit.url)}</Text>
                  )}
                </p>
                {hit.snippet ? <p className={s.hitSnip}>{hit.snippet}</p> : null}
              </div>
            </div>
          ))}
        </Capped>
      )}
    </div>
  );
}

/** The step this tool contributes to a transcript's tool group. */
export const head: CardHead = (part, env) => {
  const hits = hitsOf(part.output);
  return {
    object: str(part.input?.query),
    data: { kind: "count", text: count(hits.length, "result", "results") },
    body: <Body part={part} env={env} />,
  };
};
