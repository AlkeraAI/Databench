/**
 * The treegrid's columns: what each one is called, which server order it maps to,
 * and how a row's cell is derived from an `Item`.
 *
 * Sorting is the server's job (`orderBy`), so a column is only ever a *request* for
 * an order — the client never re-sorts a page it was handed, because a keyset page
 * sorted locally would disagree with the next page's boundary.
 */

import { nodeLabel, pathLabel } from "@alkera/ui";

import type { IconName } from "@/app/icons";
import type { Item, OrderDirection, OrderField } from "@/api/files";

import { formatBytes } from "@/lib/format/bytes";

import { UNTITLED_CHAT } from "@/lib/chatTitle";

import { fileTypeOf } from "./fileTypes";
import { shownName } from "./shownName";

export interface FilesColumn {
  /** Stable id, also the cell's `data-column`. */
  readonly id: "name" | "kind" | "size" | "modified" | "owner" | "location";
  readonly label: string;
  /** The `orderBy` field this header asks the server for; absent when the column has
   *  no server-side order. */
  readonly orderField?: OrderField;
  readonly numeric?: boolean;
  /** The narrowest this column is worth drawing at, in px. Below it the column says
   *  less than the space it takes from the name. */
  readonly minWidth: number;
  /** Which column goes first when the listing runs out of room. 0 is the name, which
   *  never goes; the rest drop from the highest number down. */
  readonly priority: number;
}

/** The ids a folder's OWN listing draws — everything but `location`, which only a
 *  listing of rows from many folders asks for. A surface that renders one folder
 *  keys its own layout off this rather than off every id a column can have. */
export type ListingColumnId = Exclude<FilesColumn["id"], "location">;

/** The columns, in the order they are drawn.
 *
 *  `priority` is the order they LEAVE in, and it is a judgement about what a person is
 *  reading a listing for: Owner goes first because almost every row in a drive belongs
 *  to the person looking at it, then Kind because the icon and the extension beside the
 *  name already say it, then Size, and Modified last — it is the column people sort by.
 *  The name is priority 0 and is never dropped. */
export const FILES_COLUMNS: readonly (FilesColumn & { id: ListingColumnId })[] = [
  { id: "name", label: "Name", orderField: "name", minWidth: 180, priority: 0 },
  { id: "kind", label: "Kind", orderField: "kind", minWidth: 96, priority: 3 },
  { id: "size", label: "Size", orderField: "size", numeric: true, minWidth: 80, priority: 2 },
  { id: "modified", label: "Modified", orderField: "mtime", minWidth: 150, priority: 1 },
  { id: "owner", label: "Owner", minWidth: 110, priority: 4 },
];

/** Where a row lives, for the listings that draw rows from more than one folder.
 *
 *  A folder's own listing never shows it — every row there is in the folder the reader
 *  is standing in — so it is not part of `FILES_COLUMNS`; a feed asks for it.
 *
 *  It is the LAST column a feed gives up — after Modified, which every other listing
 *  keeps longest. A feed's rows come from every corner of the drive, so the folder is
 *  what says which of three identically named files this row is, and Recent is ordered
 *  by Modified anyway: the date is the column a reader can infer from the order it is
 *  already reading. Behind the rail and the details pane a 1280 px window leaves the
 *  listing about 430 px, which is room for exactly one of the two. */
export const LOCATION_COLUMN: FilesColumn = {
  id: "location",
  label: "Location",
  minWidth: 140,
  priority: 1,
};

/** What Modified gives up to make room for it — in a FEED only. A folder's own listing
 *  keeps the order it had, because every row in one is already in the folder the reader
 *  is standing in and there is nothing for Location to disambiguate. The fractional
 *  priority is how Modified steps down one place without renumbering the four columns
 *  above it, whose order is a judgement of its own. */
const FEED_MODIFIED_PRIORITY = 1.5;

/** The columns a feed draws: the listing's own, plus where each row lives. */
export const FEED_COLUMNS: readonly FilesColumn[] = FILES_COLUMNS.flatMap((column) =>
  column.id === "modified"
    ? [{ ...column, priority: FEED_MODIFIED_PRIORITY }, LOCATION_COLUMN]
    : [column],
);

/** How much of a column's width is the cell's own padding rather than its text. */
const CELL_GUTTER = 24;

/**
 * The columns a listing this wide can actually draw, in drawing order.
 *
 * A column that does not fit is not rendered: it is not painted empty, and it is not
 * folded onto a second line as a stray placeholder. Otherwise a phone-width row reads as
 * placeholders around a name, and a wide column falls off the right-hand edge.
 *
 * The name always survives, however narrow the listing gets — a row with no name is not
 * a row.
 */
export function visibleColumns(
  width: number,
  options: { location?: boolean } = {},
): readonly FilesColumn[] {
  const all = options.location ? FEED_COLUMNS : FILES_COLUMNS;
  const byPriority = [...all].sort((a, b) => a.priority - b.priority);
  const kept = new Set<FilesColumn["id"]>();
  let used = 0;
  for (const column of byPriority) {
    const cost = column.minWidth + CELL_GUTTER;
    // The name is taken whether it fits or not; everything after it has to earn its room.
    if (column.priority !== 0 && used + cost > width) break;
    kept.add(column.id);
    used += cost;
  }
  return all.filter((column) => kept.has(column.id));
}

/** The `grid-template-columns` those columns lay down: each one no narrower than it is
 *  worth drawing, sharing what is left over in proportion to how much it carries. */
export function gridTemplateFor(columns: readonly FilesColumn[]): string {
  return columns
    .map((column) => `minmax(${column.minWidth}px, ${(column.minWidth / 100).toFixed(2)}fr)`)
    .join(" ");
}

export function orderFieldFor(column: FilesColumn): OrderField | undefined {
  return column.orderField;
}

/** Clicking the active column flips the direction; a new column starts ascending —
 *  except the two columns people always want newest/biggest first. */
export function nextDirection(
  field: OrderField,
  current: { field: OrderField; direction?: OrderDirection },
): OrderDirection {
  if (current.field !== field) return field === "mtime" || field === "size" ? "desc" : "asc";
  return current.direction === "desc" ? "asc" : "desc";
}

/** `aria-sort` for a header: only the active column carries a direction. */
export function ariaSortFor(
  column: FilesColumn,
  current: { field: OrderField; direction?: OrderDirection },
): "ascending" | "descending" | "none" {
  const field = orderFieldFor(column);
  if (field === undefined || field !== current.field) return "none";
  return current.direction === "desc" ? "descending" : "ascending";
}

/** Directory stats ride the item as an additive facet the OpenAPI document does not
 *  yet describe (the wire models allow unknown fields). Read it defensively: a folder
 *  with no aggregation yet shows no size rather than a wrong one. */
export interface DirStats {
  readonly totalBytes?: number;
  readonly directChildren?: number;
}

function numberAt(source: Record<string, unknown>, ...names: string[]): number | undefined {
  for (const name of names) {
    const value = source[name];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return undefined;
}

export function dirStatsOf(item: Item): DirStats | null {
  const bag = item as unknown as Record<string, unknown>;
  const facet = bag.dirStats ?? bag.dir_stats;
  if (typeof facet !== "object" || facet === null) return null;
  const source = facet as Record<string, unknown>;
  return {
    totalBytes: numberAt(source, "totalBytes", "total_bytes", "bytes"),
    directChildren: numberAt(source, "directChildren", "direct_children"),
  };
}

/** Whether a file's bytes have landed: a content hash is written with its head
 *  version, so an empty one is a node the drive has only heard of. */
function hasHead(item: Item): boolean {
  return (item.file?.content_hash ?? "") !== "";
}

/** A row's size in bytes: a file's own size, a folder's aggregated `dir_stats`.
 *  A file the machine holding its folder has and the drive does not yet reads
 *  the size on the machine's disk. */
export function sizeOf(item: Item): number | null {
  if (item.kind === "folder") return dirStatsOf(item)?.totalBytes ?? null;
  const onMachine = item.live?.holder_size;
  if (!hasHead(item) && typeof onMachine === "number") return onMachine;
  const size = item.file?.size;
  return typeof size === "number" ? size : null;
}

/** A row's size, read the way every other storage figure in the product is read
 *  — one formatter, so a file's size and the ceiling it counts against are in
 *  the same units. A row with no size (a folder whose stats have not landed) is
 *  a dash, not a zero. */
export function formatSize(bytes: number | null): string {
  return formatBytes(bytes) ?? "—";
}

export const KIND_LABELS: Readonly<Record<Item["kind"], string>> = {
  folder: "Folder",
  file: "File",
  symlink: "Link",
  object: "Object",
  special: "Special",
};

/** What an object type is CALLED, where the word a person uses and the word the wire
 *  uses are not the same. A registry rather than a branch: teaching the Kind column
 *  about a new object type is a row here, beside the icon registry that reads the
 *  same facet. Anything absent is title-cased from the wire name, which is already
 *  right for `chat`, `query` and `report`. */
export const OBJECT_KIND_LABELS: Readonly<Record<string, string>> = {
  // `chat_template` is the facet's spelling; title-cased it reads "Chat_template",
  // and nobody calls one that.
  chat_template: "Template",
};

function objectLabel(type: string): string {
  return OBJECT_KIND_LABELS[type] ?? type.charAt(0).toUpperCase() + type.slice(1);
}

/** What the Kind column reads: an object shows what it is (Chat, Template, Report…), a
 *  file its extension, everything else its node kind.
 *
 *  The object facet is read before the kind, as the icon does: a chat, a template
 *  or a report arrives as a FOLDER carrying the facet, and reading the kind first
 *  called every one of them "Folder". */
export function kindLabel(item: Item): string {
  const objectType = item.object?.type ?? "";
  if (objectType !== "") return objectLabel(objectType);
  if (item.kind === "object") {
    const type = item.subtype ?? "";
    return type === "" ? KIND_LABELS.object : objectLabel(type);
  }
  if (item.kind === "file") {
    const name = item.nameDisplay || item.name;
    const named = fileTypeOf(name);
    if (named !== null) return named.label;
    const dot = name.lastIndexOf(".");
    if (dot > 0 && dot < name.length - 1) return `${name.slice(dot + 1).toUpperCase()} file`;
  }
  return KIND_LABELS[item.kind];
}

/** The hint the shared file-icon sprite resolves: an object's type, a folder's own
 *  glyph, otherwise the file name (so the sprite map can match `package.json` as
 *  well as `.tsx`).
 *
 *  The object facet is read before the kind, because a chat arrives as a FOLDER
 *  carrying the facet — reading the kind first drew the generic folder glyph on
 *  the one row that is not a folder at all. */
export function iconHintFor(item: Item): string {
  const objectType = item.object?.type;
  if (objectType !== undefined && objectType !== "") return objectType;
  if (item.kind === "folder") return "folder";
  if (item.kind === "object") return item.subtype ?? "document";
  if (item.kind === "symlink") return "url";
  return item.nameDisplay || item.name;
}

/** The glyph an object type wears in a listing — a registry for the same reason the
 *  labels are one: a new kind of object with a page of its own is a row here rather
 *  than a third branch. */
export const OBJECT_NAV_ICONS: Readonly<Record<string, IconName>> = {
  chat: "chats",
  chat_template: "template",
  workspace: "workspaceFolder",
};

/** The nav glyph a row borrows when the row IS something the portal has a page for.
 *
 *  A chat in the tree is the same thing as a chat in the nav, so it wears the same
 *  mark rather than a file-sprite lookalike; a template wears its own, because the
 *  two rows answer to different gestures and a template that looked like a chat is
 *  the one a person opens by mistake. `null` for an ordinary node, which keeps the
 *  file sprite as the default for everything else. */
export function navIconFor(item: Item): IconName | null {
  const type = item.object?.type;
  if (type === undefined) return null;
  return OBJECT_NAV_ICONS[type] ?? null;
}

export interface ModifiedCell {
  /** ISO timestamp, or null when the row carries no mtime. */
  readonly at: string | null;
  /** Who last wrote it, when the server named an actor. */
  readonly actor: string | null;
}

/** When the row was last modified. A file with no bytes on the drive yet reads
 *  the time on the machine's disk, as its size does. */
export function modifiedOf(item: Item): ModifiedCell {
  const attrs = item.attrs ?? null;
  const onMachine = item.kind === "file" && !hasHead(item) ? item.live?.holder_mtime : null;
  const own = typeof attrs?.mtime === "string" && attrs.mtime !== "" ? attrs.mtime : null;
  const at = typeof onMachine === "string" && onMachine !== "" ? onMachine : own;
  const metadata = (attrs?.metadata ?? {}) as Record<string, unknown>;
  const actor = metadata.modifiedBy;
  return { at, actor: typeof actor === "string" && actor !== "" ? actor : null };
}

/** Absolute, unambiguous and locale-formatted; the treegrid is a working surface, so
 *  "2 days ago" is exactly the wrong answer when you are hunting a version. */
export function formatModified(cell: ModifiedCell): string {
  if (cell.at === null) return "—";
  const date = new Date(cell.at);
  if (Number.isNaN(date.getTime())) return "—";
  const when = date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
  return cell.actor === null ? when : `${when} · ${cell.actor}`;
}

/** The item's path as a person reads it. The server fills `pathBytes` on every row and
 *  leaves `path` for a client that resolved it itself; reading only `path` left the pane's
 *  Path row a dash and every search hit "in /".
 *
 *  The path is the raw names joined, not the escaped ones, so it goes through the same
 *  escaping as every other name on screen: a right-to-left override in one segment made
 *  the pane read `…exe.png` for a `.exe`. */
export function displayPath(
  item: Pick<Item, "path" | "pathBytes"> & Partial<Pick<Item, "pathHome">>,
): string | undefined {
  const raw = item.path ?? item.pathBytes ?? undefined;
  // A home is stored under its owner's id; the path names the owner instead.
  return raw === undefined ? undefined : shownName(pathLabel(raw, item.pathHome));
}

/** Whether the row IS a member's home, which wears the home mark and its owner's name. */
export function isHome(item: Pick<Item, "home">): boolean {
  return item.home !== undefined && item.home !== null;
}

/** The folder a row lives in: what it is called, and the node a click opens.
 *
 *  `null` when the server named none — a listing that is already the folder, or a
 *  caller holding a grant on the file and nothing above it. The cell then draws
 *  nothing, rather than a link into a folder that would answer 404. */
export function locationOf(item: Item): { id: string; name: string; home: boolean } | null {
  const name = item.parentName;
  const id = item.parentId;
  if (typeof name !== "string" || name === "") return null;
  if (typeof id !== "string" || id === "") return null;
  // A row directly in a member's home: the server already names the home by its
  // owner, and the cell wears the home mark beside it.
  const home =
    item.pathHome !== undefined && item.pathHome !== null && item.pathHome.node_id === id;
  return { id, name: shownName(name), home };
}

/** The folders the product itself files things in, inside a member's home,
 *  stored under the names the server gives them. Display only: the stored name
 *  is untouched, and a folder of the same name anywhere else is the person's
 *  own and keeps the casing they gave it. */
const HOME_PLACE_NAMES: Readonly<Record<string, string>> = {
  "Chat Templates": "Chat templates",
};

function homePlaceName(item: Item, name: string): string {
  const shown = HOME_PLACE_NAMES[name];
  if (shown === undefined || item.kind !== "folder") return name;
  // A folder directly in a member's home: the home the path runs through is its parent.
  const inAHome =
    item.pathHome !== undefined &&
    item.pathHome !== null &&
    item.pathHome.node_id === item.parentId;
  return inAHome ? shown : name;
}

/** The folder name a chat with no title was minted with. */
const UUID_CHAT_FOLDER =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.alkerachat$/i;

export function displayNameOf(item: Item): string {
  // A node that IS an object shows the object's CURRENT title. Its name is a
  // filesystem name minted from the title the day the node was made: an untitled
  // chat is named after its uuid (`3952c9e2-….alkerachat`), and renaming the chat
  // afterwards never rewrites the folder. Rendering the name showed people a
  // string they had never typed for a conversation they had already named.
  //
  // Escaped on the way out, whichever it is: a title is whatever a person typed,
  // and the escaped `nameDisplay` is only what the server sends when it sends one.
  //
  // A member's home shows its owner's CURRENT name: it is stored under their id.
  // The rule is the shared one every surface renders through (`nodeLabel`).
  const label = nodeLabel(item);
  const titled = (item.object?.title ?? "").trim() !== "";
  if (label.home || titled) return shownName(label.text);
  // A chat the server has not titled yet is called what every other surface
  // calls it, never the uuid its folder was minted with. A folder named from
  // a title keeps that name.
  if (item.object?.type === "chat" && UUID_CHAT_FOLDER.test(item.name)) return UNTITLED_CHAT;
  return shownName(homePlaceName(item, label.text));
}
