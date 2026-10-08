// LSP: every operation lands `JSON.stringify(result)`, and the result is
// whatever the language server answered with -- Locations, SymbolInformation,
// DocumentSymbols with children, or call-hierarchy items wrapped in `from`/`to`.
// They share one readable core (a uri, a range, sometimes a name and a numeric
// kind), so the body reads that core and indexes the answers under the file
// holding them. LSP counts lines from zero and editors count from one, so a
// printed line is the server's plus one. `hover` is the exception: it answers
// with documentation and no location, so it renders as the document it is.

import type { CSSProperties, ReactElement } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";
import { LanguageIcon, Text } from "../sharedUi";

import { Prose } from "../prose";
import { count, groupRuns, isRecord, num, records, str } from "./alkeraPayload";
import { Band, LeafPath } from "./shared";
import type { CardHead, StepEnvironment } from "./step";
import "./shared.css";
import s from "./lsp.module.css";

/** How each operation is spoken, and what its answers are called. A position
 *  operation's verb ends at the place it ran, which is what the head names. */
interface Voice {
  verb: string;
  one: string;
  many: string;
}

const VOICES: Record<string, Voice> = {
  goToDefinition: { verb: "Looked up the definition at", one: "definition", many: "definitions" },
  findReferences: { verb: "Looked up references at", one: "reference", many: "references" },
  goToImplementation: { verb: "Looked up implementations at", one: "implementation", many: "implementations" },
  prepareCallHierarchy: { verb: "Opened the call hierarchy at", one: "item", many: "items" },
  incomingCalls: { verb: "Traced callers at", one: "caller", many: "callers" },
  outgoingCalls: { verb: "Traced calls at", one: "call", many: "calls" },
  documentSymbol: { verb: "Outlined", one: "symbol", many: "symbols" },
  workspaceSymbol: { verb: "Searched symbols for", one: "symbol", many: "symbols" },
  hover: { verb: "Inspected", one: "note", many: "notes" },
};

const UNKNOWN_VOICE: Voice = { verb: "Queried", one: "result", many: "results" };

/** The LSP SymbolKind numbers, in the protocol's own order from 1. */
const SYMBOL_KINDS = [
  "",
  "file",
  "module",
  "namespace",
  "package",
  "class",
  "method",
  "property",
  "field",
  "constructor",
  "enum",
  "interface",
  "function",
  "variable",
  "constant",
  "string",
  "number",
  "boolean",
  "array",
  "object",
  "key",
  "null",
  "enum member",
  "struct",
  "event",
  "operator",
  "type parameter",
];

/** One answer: where it is, and what the server called it. */
interface Site {
  path: string;
  line: number | null;
  symbol: string;
  kind: string;
  detail: string;
  /** Nesting inside a document outline; a flat answer is all at zero. */
  depth: number;
}

function operationOf(part: ToolConversationPart): string {
  return str(part.input?.operation);
}

function voiceOf(operation: string): Voice {
  return VOICES[operation] ?? UNKNOWN_VOICE;
}

function payload(part: ToolConversationPart): string {
  return str(part.output) || str(part.content);
}

function parsed(part: ToolConversationPart): unknown {
  try {
    return JSON.parse(payload(part));
  } catch {
    return null;
  }
}

function uriToPath(uri: string): string {
  const trimmed = uri.replace(/^file:\/\//, "");
  const drive = /^\/[A-Za-z]:/.test(trimmed) ? trimmed.slice(1) : trimmed;
  try {
    return decodeURIComponent(drive);
  } catch {
    return drive;
  }
}

function kindName(kind: number | null): string {
  return kind === null ? "" : (SYMBOL_KINDS[kind] ?? "");
}

function toSite(item: Record<string, unknown>, fallbackPath: string, depth: number): Site | null {
  const holder = isRecord(item.location) ? item.location : item;
  const uri = str(holder.uri);
  const range = isRecord(item.selectionRange) ? item.selectionRange : isRecord(holder.range) ? holder.range : null;
  const start = range && isRecord(range.start) ? range.start : null;
  const zeroBased = start ? num(start.line) : null;
  const symbol = str(item.name);
  if (!symbol && zeroBased === null) return null;
  return {
    path: uri ? uriToPath(uri) : fallbackPath,
    line: zeroBased === null ? null : zeroBased + 1,
    symbol,
    kind: kindName(num(item.kind)),
    detail: str(item.detail),
    depth,
  };
}

/** Read every answer, following a document outline's children so the nesting the
 *  server reported survives. */
function readSites(value: unknown, fallbackPath: string, depth = 0): Site[] {
  if (depth > 3) return [];
  return records(value).flatMap((entry) => {
    const item = isRecord(entry.from) ? entry.from : isRecord(entry.to) ? entry.to : entry;
    const site = toSite(item, fallbackPath, depth);
    const children = readSites(item.children, fallbackPath, depth + 1);
    return site ? [site, ...children] : children;
  });
}

/** A hover's markup, whichever of the protocol's three content shapes it used. */
function markup(contents: unknown): string {
  if (typeof contents === "string") return contents;
  if (Array.isArray(contents)) return contents.map(markup).filter(Boolean).join("\n\n");
  if (isRecord(contents)) return str(contents.value);
  return "";
}

function hoverDoc(value: unknown): string {
  return records(value)
    .map((entry) => markup(entry.contents))
    .filter(Boolean)
    .join("\n\n");
}

/** Everything the head and the body read off one payload, so the two never
 *  disagree about what came back. */
interface LookupView {
  operation: string;
  location: string;
  sites: Site[];
  hover: string;
  found: number;
}

function deriveLookup(part: ToolConversationPart): LookupView {
  const operation = operationOf(part);
  const filePath = str(part.input?.filePath) || str(part.input?.path);
  const line = num(part.input?.line);
  const character = num(part.input?.character);
  const result = parsed(part);
  const hover = operation === "hover" ? hoverDoc(result) : "";
  const sites = operation === "hover" ? [] : readSites(result, filePath);
  return {
    operation,
    location:
      operation === "workspaceSymbol"
        ? str(part.input?.query)
        : operation === "documentSymbol" || line === null
          ? filePath
          : `${filePath}:${line}${character === null ? "" : `:${character}`}`,
    sites,
    hover,
    found: operation === "hover" ? (hover ? 1 : 0) : sites.length,
  };
}

function SiteRow({ site, onOpen }: { site: Site; onOpen?: () => void }): ReactElement {
  const inner = (
    <>
      {site.kind ? <span className="chat-lsp-site__kind chat-tool-quiet chat-tool-pin">{site.kind}</span> : null}
      <Text className={`${s.siteName} chat-tool-mono chat-tool-clip`} tooltip="truncate">
        {site.symbol || `line ${site.line}`}
      </Text>
      {site.symbol && site.line !== null ? <span className="chat-lsp-site__line chat-tool-num chat-tool-quiet chat-tool-pin">:{site.line}</span> : null}
      {site.detail ? (
        <Text className="chat-lsp-site__detail chat-tool-mono chat-tool-clip chat-tool-quiet" tooltip="truncate">
          {site.detail}
        </Text>
      ) : null}
    </>
  );
  const style = { "--chat-lsp-depth": site.depth } as CSSProperties;
  if (!onOpen) {
    return (
      <div className={s.site} style={style}>
        {inner}
      </div>
    );
  }
  return (
    <button type="button" className={s.site} style={style} onClick={onOpen}>
      {inner}
    </button>
  );
}

function Index({ sites, env }: { sites: Site[]; env?: StepEnvironment }): ReactElement {
  return (
    <div data-cap="300">
      {groupRuns(sites, (site) => site.path).map((group, index) => (
        <div key={`${group.key}-${index}`} className={s.group}>
          <p className={s.file}>
            <span className="chat-tool-band__icon" aria-hidden="true">
              <LanguageIcon path={group.key} size={16} />
            </span>
            {/* LeafPath renders nodes, so the tip's reading is explicit. */}
            <Text className="chat-lsp-file__path chat-tool-mono chat-tool-clip" tooltip="truncate" tooltipLabel={group.key}>
              <LeafPath path={group.key} />
            </Text>
            <span className="chat-lsp-file__n chat-tool-num chat-tool-quiet chat-tool-end">{group.items.length}</span>
          </p>
          {group.items.map((site, i) => (
            <SiteRow
              key={`${site.symbol}-${site.line}-${i}`}
              site={site}
              onOpen={env?.onOpenPath && site.path ? () => env.onOpenPath?.(site.path) : undefined}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

/** What the server said when it had nothing: its own sentence when it wrote one. */
function emptyMessage(part: ToolConversationPart): string {
  const text = payload(part).trim();
  return text.startsWith("No results found") ? text : "The lookup came back empty.";
}

/** The well's interior: the operation and where it ran, then what came back. It
 *  paints no ground, edge, radius, or outer pad; the group's well owns those. */
function Body({ part, env }: { part: ToolConversationPart; env?: StepEnvironment }): ReactElement {
  const { operation, location, sites, hover } = deriveLookup(part);
  return (
    <div data-tool="lsp">
      <Band>
        <span className={s.op}>{operation}</span>
        <Text className="chat-tool-band__text chat-tool-band__text--one chat-tool-mono" tooltip="truncate">
          {location}
        </Text>
      </Band>
      {hover ? (
        <div className="chat-tool-doc" data-cap="300">
          <Prose content={hover} />
        </div>
      ) : null}
      {sites.length > 0 ? <Index sites={sites} env={env} /> : null}
      {!hover && sites.length === 0 ? <p className="chat-tool-note">{emptyMessage(part)}</p> : null}
    </div>
  );
}

/** The step this tool contributes to a transcript's tool group. */
export const head: CardHead = (part, env) => {
  const { operation, location, found } = deriveLookup(part);
  const voice = voiceOf(operation);
  return {
    verb: voice.verb,
    object: location,
    // A workspace search names a query, everything else names a place in a file.
    objectKind: operation === "workspaceSymbol" ? "pattern" : "path",
    data: { kind: "count", text: count(found, voice.one, voice.many) },
    disclosure: voice.many,
    body: <Body part={part} env={env} />,
  };
};
