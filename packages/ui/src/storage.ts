// The one way a browser is asked to remember something.
//
// `localStorage` and `sessionStorage` are the only Web APIs in the tree that
// throw for a reason that is nobody's mistake. A private window, a profile with
// site data blocked, an embedded webview, an `about:` page, a thumbnailer: in
// all of them merely *naming* the store throws a `SecurityError`, before any
// key is read. A reader in one of those is not asking for less product — they
// are asking for the page, and a page that will not paint because the browser
// declined to remember a pane width is a defect and not a privacy feature.
//
// So every access in the product goes through here, and nothing anywhere else
// touches the globals (an ESLint rule and a scan test hold that line). Two
// promises come out of it:
//
//   * **Nothing here throws.** A read from a store that refuses is "this
//     browser has not been told"; a write it refuses is "this browser will not
//     remember".
//   * **A refused write still holds for this page session.** What could not be
//     written down is kept in memory under the same key and read back from
//     there, so the feature the value belongs to keeps working until the tab
//     closes. That covers the quota case as well as the blocked-store case: a
//     `QuotaExceededError` on one key does not silently roll a preference back
//     to what it was three months ago.
//
// JSON goes through `readJson` / `writeJson` so a value written by an older
// build, a half-written entry, or a store that answers with something that is
// not JSON at all all read as "nothing stored" rather than throwing into a
// render.

/** Which of the two stores a caller wants. `local` outlives the tab; `session`
 *  is gone when it closes. */
export type StorageKind = "local" | "session";

/** A store that cannot throw. Every method is total. */
export interface SafeStorage {
  /** The stored string, or `null` — nothing stored, or a store that refuses. */
  get(key: string): string | null;
  /**
   * Remember `value` under `key`.
   *
   * Returns whether the browser took it. `false` means the value is held in
   * memory for this page session only — the caller usually has nothing to do
   * about that, which is why the return is ignorable.
   */
  set(key: string, value: string): boolean;
  /** Forget `key`, in the browser and in memory. */
  remove(key: string): void;
  /** Whether anything is stored under `key`. */
  has(key: string): boolean;
  /** Every key this store currently answers for, in no particular order. */
  keys(): string[];
  /** The stored JSON, or `null` — nothing stored, an unreadable store, or a
   *  value that is not JSON. Callers validate the shape; this only promises it
   *  parsed. */
  readJson(key: string): unknown;
  /** Remember `value` as JSON. Returns what `set` returns; a value that cannot
   *  be stringified (a cycle, a BigInt) is `false` and nothing is written. */
  writeJson(key: string, value: unknown): boolean;
}

/** Reaching for the store is itself what throws in a blocked context, so even
 *  the lookup is guarded — and it is re-done on every access rather than probed
 *  once, because a store can start refusing mid-session (the quota fills, the
 *  reader blocks the site) and a cached "it works" would then throw. */
function backing(kind: StorageKind): Storage | null {
  try {
    const store = kind === "local" ? globalThis.localStorage : globalThis.sessionStorage;
    return store ?? null;
  } catch {
    return null;
  }
}

/** Keys whose last write did not reach the browser. Holding them here is what
 *  keeps a feature working in a window that refuses storage: the value is read
 *  back from memory for as long as the page lives. Only refused writes land
 *  here, so on a browser that works it stays empty. */
const mirrors: Record<StorageKind, Map<string, string>> = {
  local: new Map(),
  session: new Map(),
};

function makeStore(kind: StorageKind): SafeStorage {
  const mirror = mirrors[kind];

  // Plain closures rather than `this`: a caller that pulls one method off the
  // store (`const { get } = safeLocalStorage()`) gets a function that still works.
  const get = (key: string): string | null => {
    const held = mirror.get(key);
    if (held !== undefined) return held;
    try {
      return backing(kind)?.getItem(key) ?? null;
    } catch {
      return null;
    }
  };

  const set = (key: string, value: string): boolean => {
    try {
      const store = backing(kind);
      if (!store) throw new Error("this browser has no store");
      store.setItem(key, value);
      // The browser holds it now, so the memory copy would only go stale.
      mirror.delete(key);
      return true;
    } catch {
      mirror.set(key, value);
      return false;
    }
  };

  const remove = (key: string): void => {
    mirror.delete(key);
    try {
      backing(kind)?.removeItem(key);
    } catch {
      // The entry stays until the browser clears it; nothing to undo.
    }
  };

  const keys = (): string[] => {
    const found = new Set(mirror.keys());
    try {
      const store = backing(kind);
      if (store) {
        for (let index = 0; index < store.length; index += 1) {
          const key = store.key(index);
          if (key !== null) found.add(key);
        }
      }
    } catch {
      // Whatever memory holds is the whole answer.
    }
    return [...found];
  };

  const readJson = (key: string): unknown => {
    const raw = get(key);
    if (raw === null || raw === "") return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  };

  const writeJson = (key: string, value: unknown): boolean => {
    let encoded: string | undefined;
    try {
      encoded = JSON.stringify(value);
    } catch {
      return false;
    }
    return encoded === undefined ? false : set(key, encoded);
  };

  return { get, set, remove, has: (key) => get(key) !== null, keys, readJson, writeJson };
}

const stores: Record<StorageKind, SafeStorage> = {
  local: makeStore("local"),
  session: makeStore("session"),
};

/** The browser's long-lived store, guarded. */
export function safeLocalStorage(): SafeStorage {
  return stores.local;
}

/** The browser's per-tab store, guarded. */
export function safeSessionStorage(): SafeStorage {
  return stores.session;
}

/**
 * The key for something a browser remembers about one person in one org.
 *
 * A person can belong to several orgs, and what the product remembers for them
 * (the chat they last had open, a queued message, an unfinished upload) belongs
 * to the org it was made in. Built through here, every such key names both, so
 * switching orgs in this browser can never offer one org's leftovers in another:
 * `accountKey("u1", "o1", "chat.last")` is `alkera.chat.last:u1:o1`.
 *
 * `name` is the key's family (`chat.last`, `files.uploads`, ...); `orgId` may be
 * empty where a shell does not know the org yet, which keys apart from every
 * real org. A key written before keys named the org is simply never read again:
 * these are conveniences, not records, so nothing migrates them.
 */
export function accountKey(userId: string, orgId: string, name: string): string {
  return `alkera.${name}:${userId}:${orgId}`;
}

/** Drop every value held only in memory because the browser refused to keep it.
 *
 *  The memory copy is the one place a refused write survives, so it is also the
 *  one place that has to be emptied when a page session's values should not
 *  outlive it — between test cases, and on a sign-out in a window whose store
 *  is refusing. */
export function clearStorageMirror(): void {
  for (const mirror of Object.values(mirrors)) mirror.clear();
}
