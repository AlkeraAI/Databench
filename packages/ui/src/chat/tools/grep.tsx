// Grep: a content search answers with places, so the card is an index of
// places and the needle stays lit in every one of them. Hits stack under the
// file that holds them with the file's own line numbers; inside each line the
// searched pattern is marked, which is the one thing a flat printout hides.
import type { ReactElement, ReactNode } from "react";
import { LanguageIcon, Text } from "../sharedUi";
import type { ToolConversationPart } from "@alkera/chat-model";
import { count, str } from "./alkeraPayload";
import { Band, EmptyLine, LeafPath } from "./shared";
import type { CardHead } from "./step";
import "./shared.css";
import s from "./grep.module.css";
interface Hit {
  line: string;
  code: string;
}
interface FileHits {
  path: string;
  hits: Hit[];
}
interface Parsed {
  files: FileHits[];
  /** The count the tool itself reported, which can exceed what it listed. */
  reported: number | null;
}
/** opencode's grep prints `Found N matches`, then a `<path>:` header per file
 *  followed by indented `Line n: text` rows. */
function parseGrep(output: string): Parsed {
  const files: FileHits[] = [];
  let reported: number | null = null;
  for (const raw of output.split("\n")) {
    if (raw.trim().length === 0) continue;
    const found = /^Found (\d+) match/.exec(raw);
    if (found) {
      reported = Number(found[1]);
      continue;
    }
    const hit = /^\s+Line (\d+):\s?(.*)$/.exec(raw);
    if (hit && files.length > 0) {
      files[files.length - 1].hits.push({ line: hit[1], code: hit[2] });
      continue;
    }
    if (!raw.startsWith(" ") && raw.trimEnd().endsWith(":")) {
      files.push({ path: raw.trimEnd().slice(0, -1), hits: [] });
    }
  }
  return { files, reported };
}
function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
/** Light every occurrence of the pattern the caller searched for. */
function markNeedle(code: string, pattern: string): ReactNode {
  if (pattern.length === 0) return code;
  const parts = code.split(new RegExp(`(${escapeRegExp(pattern)})`, "gi"));
  return parts.map((chunk, i) =>
    i % 2 === 1 ? (
      <mark key={i} className={s.needle}>
        {chunk}
      </mark>
    ) : (
      <span key={i}>{chunk}</span>
    ),
  );
}
function payload(part: ToolConversationPart): string {
  return str(part.output) || str(part.content);
}
/** The well's interior: what the caller searched for and where, then the hits
 *  under the file that holds them. It paints no ground, edge, or radius; the
 *  group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const pattern = str(part.input?.pattern) || str(part.input?.query);
  const scope = str(part.input?.path);
  const include = str(part.input?.include);
  const parsed = parseGrep(payload(part));
  return (
    <div data-tool="grep">
      <Band>
        <span className="chat-tool-band__text">
          <span className="chat-tool-mono">{pattern}</span>
          {scope ? <span className="chat-tool-quiet"> in {scope}</span> : null}
          {include ? <span className="chat-tool-quiet"> matching {include}</span> : null}
        </span>
      </Band>
      {parsed.files.length === 0 ? (
        <EmptyLine>No line matched this pattern.</EmptyLine>
      ) : (
        <div className={s.hits} data-cap="260">
          {parsed.files.map((file) => (
            <div key={file.path} className={s.group}>
              <p className={s.file}>
                <span className="chat-tool-band__icon" aria-hidden="true">
                  <LanguageIcon path={file.path} size={16} />
                </span>
                {/* LeafPath renders nodes, so the tip's reading is explicit. */}
                <Text className="chat-tool-mono chat-tool-clip" tooltip="truncate" tooltipLabel={file.path}>
                  <LeafPath path={file.path} />
                </Text>
                <span className="chat-tool-num chat-tool-quiet chat-tool-pin">{file.hits.length}</span>
              </p>
              {file.hits.map((hit) => (
                <div key={hit.line} className={`${s.hit} chat-tool-mono`}>
                  <span className={s.hitN} aria-hidden="true">
                    {hit.line}
                  </span>
                  <span className="chat-tool-src__code">{markNeedle(hit.code, pattern)}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
/** The step this tool contributes to a transcript's tool group. */
export const head: CardHead = (part) => {
  const parsed = parseGrep(payload(part));
  const shown = parsed.files.reduce((sum, file) => sum + file.hits.length, 0);
  const total = parsed.reported ?? shown;
  return {
    object: str(part.input?.pattern) || str(part.input?.query),
    data: {
      kind: "count",
      text: `${count(total, "match", "matches")} in ${count(parsed.files.length, "file", "files")}`,
    },
    body: <Body part={part} />,
    footer: shown < total ? `${shown} of ${count(total, "match", "matches")}` : undefined,
  };
};
