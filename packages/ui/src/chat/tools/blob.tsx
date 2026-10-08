// A large result spills to a content-addressed handle, and these three calls mint
// one (`blob.create`), read its shape (`blob.info`), or reclaim it
// (`blob.delete`). Every blob well leads with that handle, because it is what the
// agent quotes and what makes two steps about the same result recognizable at a
// glance. The band, the byte scale, and the column list here are the whole
// family's; blob.materialize, blob.profile, and blob.query read them from this
// module and render into the same `blob` scope.
import type { ReactElement, ReactNode } from "react";
import type { ToolConversationPart } from "@alkera/chat-model";
import { IconStack2, IconStackPush, IconTrash } from "@tabler/icons-react";
import { Text } from "../sharedUi";
import { count, num, objectOutput, readResult, str, strings } from "./alkeraPayload";
import { Band } from "./shared";
import type { CardHead } from "./step";
import "./shared.css";
import "./blob.css";
/** How much of an authored text a card shows before the reader should open the
 *  result itself. */
const TEXT_PEEK = 320;
const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"];
/** Bytes at the scale a reader thinks in. */
export function bytes(size: number): string {
  let value = size;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? String(Math.round(value)) : value.toFixed(value < 10 ? 1 : 0)} ${BYTE_UNITS[unit]}`;
}
/** A handle is a 64-character sha. Twelve is enough to recognize the same result
 *  across two steps, and the band's tip keeps the whole value. */
export function shortHandle(handle: string): string {
  return handle.length > 12 ? handle.slice(0, 12) : handle;
}
/** The band every blob well opens with: the tool's glyph, the handle the call is
 *  about, and whatever single parameter shaped it. */
export function HandleBand({
  handle,
  glyph,
  param,
}: {
  handle: string;
  glyph: ReactNode;
  param?: ReactNode;
}): ReactElement | null {
  if (!handle) return null;
  const short = shortHandle(handle);
  return (
    <Band>
      <span className="chat-tool-band__icon" aria-hidden="true">
        {glyph}
      </span>
      {/* A sha cut to twelve characters appears nowhere on the page in full, so its
          tip opens on every hover. One short enough to read whole waits for a clip. */}
      <Text
        className="chat-tool-band__text chat-tool-band__text--one chat-tool-mono"
        tooltip={short === handle ? "truncate" : "always"}
        tooltipLabel={handle}
      >
        <span className="chat-blob-hash">#</span>
        <span className="chat-tool-leaf">{short}</span>
      </Text>
      {param ? <span className="chat-tool-band__param">{param}</span> : null}
    </Band>
  );
}
/** A spilled result's schema is a list of names, so it renders as names rather
 *  than as an empty table. */
export function ColumnChips({ columns }: { columns: string[] }): ReactElement | null {
  if (columns.length === 0) return null;
  return (
    <p className="chat-blob-cols">
      {columns.map((column, index) => (
        <span key={`${column}-${index}`} className="chat-blob-col chat-tool-chip chat-tool-mono">
          {column}
        </span>
      ))}
    </p>
  );
}
type BlobAct = "create" | "info" | "delete";
/** A call can arrive under an MCP prefix, so the family matches on the suffix. */
function actOf(name: string): BlobAct {
  if (name.endsWith("blob.create")) return "create";
  if (name.endsWith("blob.delete")) return "delete";
  return "info";
}
interface BlobView {
  act: BlobAct;
  handle: string;
  /** "rows", "text", or "opaque" for a blob that carries no result envelope. */
  kind: string;
  /** Rows for a tabular result, characters for text. */
  total: number;
  size: number;
  columns: string[];
  contentType: string;
  resultName: string;
  text: string;
  deleted: boolean;
  freed: number;
  message: string;
}
function deriveBlob(part: ToolConversationPart): BlobView {
  const result = readResult(part.output);
  const input = part.input ?? {};
  const act = actOf(part.name);
  const minted = objectOutput(result?.blob);
  const authored = str(input.text);
  const authoredRows = Array.isArray(input.rows) ? input.rows.length : 0;
  return {
    act,
    // A create is the one call whose handle arrives on the way out.
    handle: act === "create" ? str(minted?.sha256) : str(input.handle),
    // A create that has not answered yet still knows what it was handed.
    kind: str(result?.kind) || (act === "create" ? (authored ? "text" : "rows") : ""),
    total: num(result?.total) ?? (authored ? authored.length : authoredRows),
    size: num(minted?.size) ?? num(result?.size_bytes) ?? 0,
    columns: act === "create" ? strings(input.columns) : strings(result?.columns),
    contentType: str(result?.content_type),
    resultName: str(result?.result_name) || str(input.result_name),
    text: authored,
    deleted: result?.deleted === true,
    freed: num(result?.freed_bytes) ?? 0,
    message: str(result?.message),
  };
}
function ActGlyph({ act }: { act: BlobAct }): ReactElement {
  if (act === "create") return <IconStackPush size={14} stroke={1.6} />;
  if (act === "delete") return <IconTrash size={14} stroke={1.6} />;
  return <IconStack2 size={14} stroke={1.6} />;
}
/** What the result IS, in the facts the step's own figure does not already
 *  carry: its kind, the bytes it occupies, and how it is encoded. */
function Shape({ view }: { view: BlobView }): ReactElement {
  const stated = view.kind || view.size > 0 || view.contentType;
  return (
    <>
      {stated ? (
        <p className="chat-blob-facts">
          {view.kind ? <span className="chat-blob-kind chat-tool-chip">{view.kind}</span> : null}
          {view.size > 0 ? <span className="chat-tool-num">{bytes(view.size)}</span> : null}
          {view.contentType ? <span className="chat-tool-num">{view.contentType}</span> : null}
        </p>
      ) : null}
      <ColumnChips columns={view.columns} />
      {view.text ? (
        <Text as="p" className="chat-blob-text chat-tool-mono" tooltip="truncate">
          {view.text.slice(0, TEXT_PEEK)}
        </Text>
      ) : null}
    </>
  );
}
/** What became of the result, in the words the tool answered with: a delete can
 *  reclaim the bytes, keep them for another chat that still points at them, or
 *  find nothing left to reclaim. */
function Outcome({ message }: { message: string }): ReactElement | null {
  if (!message) return null;
  return <p className="chat-blob-outcome">{message}</p>;
}
/** The well's interior: the handle, then either what the result holds or what
 *  became of it. It paints no ground, edge, radius, or outer pad; the group's
 *  well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const view = deriveBlob(part);
  return (
    <div data-tool="blob">
      <HandleBand
        handle={view.handle}
        glyph={<ActGlyph act={view.act} />}
        param={view.resultName || undefined}
      />
      {view.act === "delete" ? <Outcome message={view.message} /> : <Shape view={view} />}
    </div>
  );
}
/** The extent of a result, worded by what it holds. */
function extentOf(view: BlobView): string {
  if (view.kind === "text") return count(view.total, "character", "characters");
  if (view.kind === "opaque") return bytes(view.size || view.total);
  return count(view.total, "row", "rows");
}
function figureOf(view: BlobView): string {
  if (view.act === "delete") return view.deleted ? `freed ${bytes(view.freed)}` : "nothing freed";
  if (view.act === "info" && view.columns.length > 0) {
    return `${extentOf(view)}, ${count(view.columns.length, "column", "columns")}`;
  }
  return extentOf(view);
}
/** What a step calls the result: the name the caller gave it, else its handle. */
function named(view: BlobView): string {
  return view.resultName || shortHandle(view.handle);
}
/** The step each of the three calls contributes to the transcript's tool group.
 *  Store and inspect read the same, and only their rows differ. */
export const head: CardHead = (part) => {
  const view = deriveBlob(part);
  return {
    object: named(view),
    data: { kind: "count", text: figureOf(view) },
    body: <Body part={part} />,
  };
};
export const headDelete: CardHead = (part) => {
  const view = deriveBlob(part);
  return {
    object: named(view),
    data: { kind: "count", text: figureOf(view) },
    // A delete that did not happen owes the reader a reason, so it opens; one
    // that did is fully told by its figure.
    expanded: !view.deleted,
    body: <Body part={part} />,
  };
};
