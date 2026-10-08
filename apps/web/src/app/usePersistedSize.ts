// How wide a pane is, and whether it is open, on THIS machine.
//
// Three kinds of fact meet on a laid-out page and they belong in three places.
// What is open is the reader's wherever they sign in, so it lives on the
// server. What a pane is scrolled to is gone the moment the page is, so it
// lives in memory. How wide a pane is sits between them: it is a property of
// the screen in front of the person — a 13" laptop and a 34" monitor want
// different answers from the same account — so it belongs to the browser and to
// nothing else.
//
// Storage is the shared guarded one (`@alkera/ui/storage`): it throws outright
// in some embedded and privacy contexts, comes back empty in a private window,
// and may hold a value written by a build that laid the page out differently or
// by a monitor this one is not. All of those read as "this browser has not been
// told yet", because a layout can always be drawn from that and can never be
// drawn from a broken number.
//
// A key that varies — one entry per chat — would grow without bound, so a
// namespace can declare how many it keeps: the most recent N survive and the
// rest are forgotten along with their entries.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { safeLocalStorage, type SafeStorage } from "@alkera/ui/storage";

const PREFIX = "alkera.size:";
const LRU_PREFIX = "alkera.size.lru:";

/** One pane's range, in CSS pixels. `size` is where it opens. */
export interface SizeBounds {
  min: number;
  max: number;
  size: number;
}

export interface PaneSize {
  collapsed: boolean;
  width: number;
}

/** How many keys a varying namespace keeps. Enough that a reader coming back to
 *  a chat they were in this week finds it as they left it, small enough that the
 *  whole record is a few hundred bytes. */
export const LRU_KEEP = 40;

export interface LruBound {
  /** The namespace every varying key under it belongs to. */
  namespace: string;
  /** How many keys to keep. The rest are dropped, oldest first. */
  keep?: number;
}

export interface PersistedSizeOptions {
  /** Narrow the range — the viewport is usually the only thing that does. */
  bounds?: Partial<SizeBounds>;
  /** What the pane does on a browser that has never been told. Consulted only
   *  when there is nothing stored. */
  collapsedByDefault?: boolean;
  /** Bound a namespace whose keys vary (one per chat), so the browser's copy of
   *  the layout cannot grow without end. */
  lru?: LruBound;
}

/** The guarded store every layout value goes through. Exported because the chat
 *  page's own layout document (`workspace/layoutStorage`) is one entry beside
 *  these and has to be written through the same one. */
export function sizeStore(): SafeStorage {
  return safeLocalStorage();
}

function readRaw(key: string): unknown {
  // An unreadable store and an unparsable value are the same fact: this browser
  // has nothing to say.
  return sizeStore().readJson(key);
}

function writeRaw(key: string, value: unknown): void {
  // A store that will not take the write costs this reader the memory of their
  // layout and nothing else.
  sizeStore().writeJson(key, value);
}

function dropRaw(key: string): void {
  sizeStore().remove(key);
}

/** The bounds a caller actually draws in. */
export function boundsOf(base: SizeBounds, narrowed?: Partial<SizeBounds>): SizeBounds {
  return { ...base, ...narrowed };
}

/**
 * A width inside the range, or where the pane opens when the stored value is not
 * a width at all.
 *
 * A `min` above `max` — a viewport too narrow for the pane's own floor —
 * resolves to `min`: the pane keeps its floor and the layout squeezes, rather
 * than the stored number going negative.
 */
export function clampSize(value: unknown, bounds: SizeBounds): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return clampSize(bounds.size, bounds);
  return Math.max(bounds.min, Math.min(Math.max(bounds.min, bounds.max), value));
}

/** A stored record read as a pane size, whatever it turned out to be. */
export function readPaneSize(raw: unknown, bounds: SizeBounds, collapsedByDefault: boolean): PaneSize {
  const record = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  return {
    collapsed: typeof record.collapsed === "boolean" ? record.collapsed : collapsedByDefault,
    width: clampSize(record.width, bounds),
  };
}

/** Whether this browser has been told anything at all about this key. The
 *  difference between "nothing stored" and "stored, and it happens to be the
 *  default" is what lets a caller fall back to something better than the
 *  default. */
export function hasSize(key: string | null): boolean {
  if (!key) return false;
  return sizeStore().has(`${PREFIX}${key}`);
}

/** The keys a namespace currently remembers, most recent first. */
function lruKeys(namespace: string): string[] {
  const raw = readRaw(`${LRU_PREFIX}${namespace}`);
  return Array.isArray(raw) ? raw.filter((k): k is string => typeof k === "string") : [];
}

/**
 * Record that `key` was just written, and forget the oldest keys past the bound
 * — the entries as well as the index, so the store does not keep paying for a
 * chat nobody has opened in months.
 */
export function touchLru(bound: LruBound, key: string): void {
  const keep = Math.max(1, bound.keep ?? LRU_KEEP);
  const current = lruKeys(bound.namespace).filter((k) => k !== key);
  const next = [key, ...current];
  for (const dropped of next.slice(keep)) dropRaw(`${PREFIX}${dropped}`);
  writeRaw(`${LRU_PREFIX}${bound.namespace}`, next.slice(0, keep));
}

/** What this browser remembers for this key, already clamped to the range the
 *  caller can draw in. Never throws and never answers a width a pane cannot
 *  take. */
export function readSize(
  key: string | null,
  base: SizeBounds,
  options: PersistedSizeOptions = {},
): PaneSize {
  const bounds = boundsOf(base, options.bounds);
  if (!key) return { collapsed: options.collapsedByDefault ?? false, width: clampSize(base.size, bounds) };
  return readPaneSize(readRaw(`${PREFIX}${key}`), bounds, options.collapsedByDefault ?? false);
}

/** Remember this size for this key. Clamped on the way in as well as on the way
 *  out, so a width squeezed by a narrow window is not stored as a preference the
 *  reader never expressed. */
export function writeSize(
  key: string | null,
  value: PaneSize,
  base: SizeBounds,
  options: PersistedSizeOptions = {},
): void {
  if (!key) return;
  const bounds = boundsOf(base, options.bounds);
  writeRaw(`${PREFIX}${key}`, { collapsed: value.collapsed, width: clampSize(value.width, bounds) });
  if (options.lru) touchLru(options.lru, key);
}

/**
 * How long this browser's copy may lag the screen.
 *
 * A drag settles a width on every pointer frame, and `localStorage.setItem` is
 * synchronous: writing each of those frames spends the same main thread that has
 * to redraw the columns under the cursor. The width the reader meant is the one
 * the gesture stops on, so the page lays out from every frame and the browser is
 * told once the frames stop.
 */
export const SIZE_COMMIT_MS = 120;

/**
 * One remembered pane size.
 *
 * Returns what to draw and one way to change it. The value is read once, for the
 * key the hook was first given; when the key changes — a different chat — the
 * hook re-reads, because a width remembered for one chat is not another's.
 */
export function usePersistedSize(
  key: string | null,
  base: SizeBounds,
  options: PersistedSizeOptions = {},
): [PaneSize, (part: Partial<PaneSize>) => void] {
  const latest = useRef<PersistedSizeOptions>(options);
  latest.current = options;
  const [size, setSize] = useState<PaneSize>(() => readSize(key, base, options));

  // The key that produced the value on screen. A change to it is a different
  // pane, so what is owed for the old one is written before the new one is read.
  const readFor = useRef<string | null>(key);
  const owed = useRef<PaneSize | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const commit = useCallback(
    (forKey: string | null): void => {
      if (timer.current !== null) {
        clearTimeout(timer.current);
        timer.current = null;
      }
      const value = owed.current;
      owed.current = null;
      if (value) writeSize(forKey, value, base, latest.current);
    },
    [base],
  );

  // Before the frame: a key that arrives a render late (an id from a read) would
  // otherwise paint one frame at the default before the stored value lands.
  useLayoutEffect(() => {
    if (readFor.current === key) return;
    commit(readFor.current);
    readFor.current = key;
    setSize(readSize(key, base, latest.current));
  }, [key, base, commit]);

  // A width chosen a moment before leaving the page is still a width the reader
  // chose, so what is owed is written on the way out rather than dropped.
  useEffect(() => () => commit(readFor.current), [commit]);

  const apply = useCallback(
    (part: Partial<PaneSize>): void => {
      setSize((current) => {
        const next = { ...current, ...part };
        owed.current = next;
        if (timer.current !== null) clearTimeout(timer.current);
        timer.current = setTimeout(() => commit(readFor.current), SIZE_COMMIT_MS);
        return next;
      });
    },
    [commit],
  );

  return [size, apply];
}
