/**
 * The filter bar's state: chips in, listing parameters and a URL out.
 *
 * Every chip is a row in one registry, so a new facet is a registration rather
 * than a new branch in three places (the bar that renders it, the serialiser
 * that sends it, the URL codec that makes the view linkable). The registry is
 * also what the empty state reads to name what is filtering the folder away.
 *
 * Three invariants hold the whole thing together:
 *  * a chip's `apply` writes into a `ListFilters`, so `toChildrenParams` in
 *    `api/files.ts` stays the single place a filter becomes a query parameter —
 *    the folder listing and search can never disagree about a chip's spelling;
 *  * chips combine with AND, and two chips in the same exclusive group replace
 *    one another rather than stacking an unsatisfiable pair;
 *  * the state round-trips through the URL, so a filtered view is linkable and
 *    survives a reload.
 */

import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import type { ListFilters, OrderBy, OrderDirection, OrderField } from "@/api/files";

/** Where a search reads from: the folder on screen, or the whole drive. */
export type SearchScope = "folder" | "drive";

/** A range a chip carries with it (modified and size both need one). */
export interface RangeValue {
  from?: string;
  to?: string;
}

export interface FilterState {
  /** The search text, undebounced — the field owns it. */
  text: string;
  scope: SearchScope;
  /** Chip ids that are on, in registration order. */
  chips: readonly string[];
  /** Custom ranges, keyed by the chip id that opens them. */
  ranges: Readonly<Record<string, RangeValue>>;
  /** A person id for the `owner:person` chip. */
  ownerId?: string;
  orderBy: OrderBy;
}

export const DEFAULT_ORDER_BY: OrderBy = { field: "name", direction: "asc" };

export const EMPTY_FILTER_STATE: FilterState = {
  text: "",
  // A search opens on the whole drive: the row a person is looking for is as
  // often one folder over as it is here. Narrowing is the deliberate act, and
  // it is the one the address has to carry.
  scope: "drive",
  chips: [],
  ranges: {},
  orderBy: DEFAULT_ORDER_BY,
};

/** A chip group. Chips inside an exclusive group replace one another; chips in a
 *  non-exclusive group stack (three kind chips are three kinds, not none). */
export interface ChipGroup {
  id: string;
  label: string;
  exclusive: boolean;
}

export const CHIP_GROUPS: readonly ChipGroup[] = [
  { id: "kind", label: "Kind", exclusive: false },
  { id: "owner", label: "Owner", exclusive: true },
  { id: "modified", label: "Modified", exclusive: true },
  { id: "size", label: "Size", exclusive: true },
  { id: "name", label: "Name", exclusive: false },
  { id: "state", label: "State", exclusive: false },
];

export interface ChipContext {
  now: Date;
  range?: RangeValue;
  ownerId?: string;
}

export interface ChipSpec {
  id: string;
  group: string;
  label: string;
  /** Carries a custom range or a person id rather than standing on its own. */
  custom?: boolean;
  /** Writes this chip's contribution into the listing filters. `now` arrives in
   *  the context so a relative window ("today") is computed against an injected
   *  clock and a test can cross a day boundary without waiting for one. */
  apply: (filters: ListFilters, ctx: ChipContext) => void;
}

const DAY_MS = 86_400_000;

function daysAgo(now: Date, days: number): string {
  return new Date(now.getTime() - days * DAY_MS).toISOString();
}

function startOfDay(now: Date): string {
  const day = new Date(now.getTime());
  day.setHours(0, 0, 0, 0);
  return day.toISOString();
}

const MIB = 1024 * 1024;

/**
 * Every chip the bar offers, in the order it appears on screen.
 *
 * The kind row maps onto the listing API's filters: folders and files are `kind=`, the
 * four object kinds are `objectType=`, and the content classes are `mimeClass=`.
 */
export const CHIPS: readonly ChipSpec[] = [
  {
    id: "kind:folders",
    group: "kind",
    label: "Folders",
    apply: (f) => {
      f.kind = "folder";
    },
  },
  {
    id: "kind:files",
    group: "kind",
    label: "Files",
    apply: (f) => {
      f.kind = "file";
    },
  },
  {
    id: "kind:chats",
    group: "kind",
    label: "Chats",
    apply: (f) => {
      f.objectType = "chat";
    },
  },
  {
    // A saved query is retired as a thing of its own — the SQL it held now lives in
    // a template's README — so the chip that narrowed a folder to queries narrows it
    // to templates instead. A stale link carrying `kind:queries` drops the chip on
    // read, which is what `filterStateFromParams` already does with any id it does
    // not know.
    id: "kind:templates",
    group: "kind",
    label: "Templates",
    apply: (f) => {
      f.objectType = "chat_template";
    },
  },
  {
    id: "kind:results",
    group: "kind",
    label: "Results",
    apply: (f) => {
      f.objectType = "result";
    },
  },
  {
    id: "kind:boards",
    group: "kind",
    label: "Boards",
    apply: (f) => {
      f.objectType = "board";
    },
  },
  {
    id: "kind:images",
    group: "kind",
    label: "Images",
    apply: (f) => {
      f.mimeClass = "image";
    },
  },
  {
    id: "kind:tabular",
    group: "kind",
    label: "Tabular",
    apply: (f) => {
      f.mimeClass = "tabular";
    },
  },
  {
    id: "kind:code",
    group: "kind",
    label: "Code",
    apply: (f) => {
      f.mimeClass = "code";
    },
  },
  {
    id: "kind:archives",
    group: "kind",
    label: "Archives",
    apply: (f) => {
      f.mimeClass = "archive";
    },
  },
  {
    id: "kind:other",
    group: "kind",
    label: "Other",
    apply: (f) => {
      f.mimeClass = "other";
    },
  },

  {
    id: "owner:me",
    group: "owner",
    label: "Me",
    apply: (f) => {
      f.owner = "me";
    },
  },
  {
    id: "owner:anyone",
    group: "owner",
    label: "Anyone",
    apply: () => {
      /* the absence of an owner filter IS "anyone" — the chip only clears the group */
    },
  },
  {
    id: "owner:person",
    group: "owner",
    label: "A person",
    custom: true,
    apply: (f, ctx) => {
      if (ctx.ownerId) f.owner = ctx.ownerId;
    },
  },

  {
    id: "modified:today",
    group: "modified",
    label: "Today",
    apply: (f, ctx) => {
      f.modifiedAfter = startOfDay(ctx.now);
    },
  },
  {
    id: "modified:7d",
    group: "modified",
    label: "Last 7 days",
    apply: (f, ctx) => {
      f.modifiedAfter = daysAgo(ctx.now, 7);
    },
  },
  {
    id: "modified:30d",
    group: "modified",
    label: "Last 30 days",
    apply: (f, ctx) => {
      f.modifiedAfter = daysAgo(ctx.now, 30);
    },
  },
  {
    id: "modified:custom",
    group: "modified",
    label: "Custom range",
    custom: true,
    apply: (f, ctx) => {
      if (ctx.range?.from) f.modifiedAfter = ctx.range.from;
      if (ctx.range?.to) f.modifiedBefore = ctx.range.to;
    },
  },

  {
    id: "size:small",
    group: "size",
    label: "Under 1 MB",
    apply: (f) => {
      f.sizeMax = MIB;
    },
  },
  {
    id: "size:medium",
    group: "size",
    label: "1 MB to 100 MB",
    apply: (f) => {
      f.sizeMin = MIB;
      f.sizeMax = 100 * MIB;
    },
  },
  {
    id: "size:large",
    group: "size",
    label: "Over 100 MB",
    apply: (f) => {
      f.sizeMin = 100 * MIB;
    },
  },
  {
    id: "size:custom",
    group: "size",
    label: "Custom size",
    custom: true,
    apply: (f, ctx) => {
      const min = Number(ctx.range?.from);
      const max = Number(ctx.range?.to);
      if (ctx.range?.from && Number.isFinite(min)) f.sizeMin = min;
      if (ctx.range?.to && Number.isFinite(max)) f.sizeMax = max;
    },
  },

  {
    id: "name:windows",
    group: "name",
    label: "Unsafe on Windows",
    apply: (f) => {
      f.nameFlag = "windows_safe";
    },
  },
  {
    id: "name:macos",
    group: "name",
    label: "Unsafe on macOS",
    apply: (f) => {
      f.nameFlag = "macos_safe";
    },
  },
  {
    id: "name:warning",
    group: "name",
    label: "Display warnings",
    apply: (f) => {
      f.nameFlag = "display_warning";
    },
  },

  {
    id: "state:starred",
    group: "state",
    label: "Starred",
    apply: (f) => {
      f.starred = true;
    },
  },
  {
    id: "state:shared",
    group: "state",
    label: "Shared",
    apply: (f) => {
      f.shared = true;
    },
  },
  {
    id: "state:leased",
    group: "state",
    label: "In use",
    apply: (f) => {
      f.leased = true;
    },
  },
  {
    id: "state:trashed",
    group: "state",
    label: "Trashed",
    apply: (f) => {
      f.trashed = true;
    },
  },
];

const CHIP_BY_ID: ReadonlyMap<string, ChipSpec> = new Map(CHIPS.map((chip) => [chip.id, chip]));
const GROUP_BY_ID: ReadonlyMap<string, ChipGroup> = new Map(
  CHIP_GROUPS.map((group) => [group.id, group]),
);
const CHIP_ORDER: readonly string[] = CHIPS.map((chip) => chip.id);

export function chipById(id: string): ChipSpec | undefined {
  return CHIP_BY_ID.get(id);
}

export function chipsOfGroup(groupId: string): readonly ChipSpec[] {
  return CHIPS.filter((chip) => chip.group === groupId);
}

/** Toggle a chip. An exclusive group's other chips come off with it, so "today"
 *  and "last 7 days" can never both be on and silently cancel each other. */
export function toggleChip(state: FilterState, chipId: string): FilterState {
  const spec = CHIP_BY_ID.get(chipId);
  if (!spec) return state;
  if (state.chips.includes(chipId)) {
    return { ...state, chips: state.chips.filter((id) => id !== chipId) };
  }
  const group = GROUP_BY_ID.get(spec.group);
  const kept = group?.exclusive
    ? state.chips.filter((id) => CHIP_BY_ID.get(id)?.group !== spec.group)
    : state.chips;
  const next = [...kept, chipId].sort((a, b) => CHIP_ORDER.indexOf(a) - CHIP_ORDER.indexOf(b));
  return { ...state, chips: next };
}

/**
 * The chips, folded into the listing filters both `useChildren` and the search
 * hook send. Chips AND together because each writes its own parameter into one
 * record and the server applies every parameter it is given.
 */
export function toListFilters(state: FilterState, now: Date = new Date()): ListFilters {
  const filters: ListFilters = {};
  for (const id of state.chips) {
    CHIP_BY_ID.get(id)?.apply(filters, { now, range: state.ranges[id], ownerId: state.ownerId });
  }
  return filters;
}

/** Whether anything is narrowing the view — what the empty state asks. */
export function hasActiveFilters(state: FilterState): boolean {
  return state.chips.length > 0 || state.text.trim() !== "";
}

/** Human labels for everything currently narrowing the view, so the empty state
 *  can name the filters instead of shrugging. */
export function activeFilterLabels(state: FilterState): readonly string[] {
  const labels = state.chips
    .map((id) => CHIP_BY_ID.get(id)?.label)
    .filter((label): label is string => Boolean(label));
  const text = state.text.trim();
  return text ? [`“${text}”`, ...labels] : labels;
}

/** Clear the chips and the text but keep where the user is looking and how the
 *  rows are ordered — clearing a filter is not a navigation. */
export function clearFilters(state: FilterState): FilterState {
  return { ...state, text: "", chips: [], ranges: {}, ownerId: undefined };
}

// --- URL round trip ---------------------------------------------------------

const PARAM_Q = "q";
const PARAM_SCOPE = "scope";
const PARAM_CHIPS = "chips";
const PARAM_RANGE_PREFIX = "r.";
const PARAM_OWNER = "ownerId";
const PARAM_SORT = "sort";
const OWNED_PARAMS: readonly string[] = [
  PARAM_Q,
  PARAM_SCOPE,
  PARAM_CHIPS,
  PARAM_OWNER,
  PARAM_SORT,
];

const ORDER_FIELDS: readonly OrderField[] = ["name", "size", "mtime", "kind"];

function parseOrder(raw: string | null): OrderBy {
  if (!raw) return DEFAULT_ORDER_BY;
  const [field, direction] = raw.split(" ");
  if (!ORDER_FIELDS.includes(field as OrderField)) return DEFAULT_ORDER_BY;
  const dir: OrderDirection = direction === "desc" ? "desc" : "asc";
  return { field: field as OrderField, direction: dir };
}

/** Read a filter state out of the URL. An unknown chip id is dropped rather than
 *  carried, so a stale link cannot send the server a parameter it will refuse. */
export function filterStateFromParams(params: URLSearchParams): FilterState {
  const chipsRaw = params.get(PARAM_CHIPS);
  const chips = (chipsRaw ? chipsRaw.split(",") : []).filter((id) => CHIP_BY_ID.has(id));
  const ranges: Record<string, RangeValue> = {};
  for (const [key, value] of params.entries()) {
    if (!key.startsWith(PARAM_RANGE_PREFIX)) continue;
    const [chipId, edge] = key.slice(PARAM_RANGE_PREFIX.length).split("|");
    if (!CHIP_BY_ID.has(chipId) || (edge !== "from" && edge !== "to")) continue;
    ranges[chipId] = { ...ranges[chipId], [edge]: value };
  }
  return {
    text: params.get(PARAM_Q) ?? "",
    scope: params.get(PARAM_SCOPE) === "folder" ? "folder" : "drive",
    chips,
    ranges,
    ownerId: params.get(PARAM_OWNER) ?? undefined,
    orderBy: parseOrder(params.get(PARAM_SORT)),
  };
}

/**
 * Write a filter state into a URL, preserving every parameter this module does
 * not own (a page that adds `?pane=details` keeps it across a chip click).
 * Defaults are omitted so an unfiltered view has a clean address.
 */
export function filterStateToParams(state: FilterState, base?: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(base ? base.toString() : undefined);
  for (const name of OWNED_PARAMS) next.delete(name);
  for (const key of [...next.keys()]) {
    if (key.startsWith(PARAM_RANGE_PREFIX)) next.delete(key);
  }
  if (state.text) next.set(PARAM_Q, state.text);
  if (state.scope === "folder") next.set(PARAM_SCOPE, "folder");
  if (state.chips.length) next.set(PARAM_CHIPS, state.chips.join(","));
  if (state.ownerId) next.set(PARAM_OWNER, state.ownerId);
  for (const [chipId, range] of Object.entries(state.ranges)) {
    if (range.from) next.set(`${PARAM_RANGE_PREFIX}${chipId}|from`, range.from);
    if (range.to) next.set(`${PARAM_RANGE_PREFIX}${chipId}|to`, range.to);
  }
  const { field, direction } = state.orderBy;
  if (field !== DEFAULT_ORDER_BY.field || direction !== DEFAULT_ORDER_BY.direction) {
    next.set(PARAM_SORT, direction ? `${field} ${direction}` : field);
  }
  return next;
}

/**
 * The filter state, kept in the URL rather than in component state: that is what
 * makes a filtered view linkable and reload-proof, and it leaves one source of
 * truth for the bar, the search field and the listing hooks.
 */
export function useFilterState(): [FilterState, (next: FilterState) => void] {
  const [params, setParams] = useSearchParams();
  const state = useMemo(() => filterStateFromParams(params), [params]);
  const set = useCallback(
    (next: FilterState) => {
      setParams(filterStateToParams(next, params), { replace: true });
    },
    [params, setParams],
  );
  return [state, set];
}
