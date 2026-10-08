// One live handle per key for the whole tab, shared by every holder.
//
// A live draft, a live file and a live notebook are each a channel kept open
// while something on screen shows it: every holder of the same key shares one
// handle, the last one to let go starts a linger, and a hold inside the linger
// keeps the handle without re-syncing it. A handle that has given up (the
// server would not serve it, the browser could not load Loro) and that nobody
// holds is opened afresh on the next hold, since the reason it gave up may
// have passed.
//
// A handle that still holds something its person typed and the server has not
// taken (or an offer of it nobody has seen) is never closed by the linger nor
// replaced: it lingers on, still trying, until it holds nothing. Only an end of
// the session (`closeAll`, on sign-out) lets it go regardless.

/** What the registry needs of a handle: a way to let it go for good, and
 *  whether doing so now would lose something the person typed. */
export interface LiveHandle {
  close(): void;
  holdsUnacknowledged(): boolean;
}

export interface LiveHandleRegistryOptions<T extends LiveHandle> {
  /** How long an unheld handle lingers before it is closed. */
  lingerMs: number;
  /** The handle has given up; an unheld one is replaced on the next hold. */
  isSpent: (handle: T) => boolean;
}

export interface LiveHandleRegistry<T extends LiveHandle> {
  /** Hold `key`'s handle, opening it with `open` when there is none (or only a
   *  spent, unheld one). The release lets go once; calling it again does nothing. */
  acquire(key: string, open: () => T): { handle: T; release: () => void };
  /** Close every handle, held or lingering, and forget them all. */
  closeAll(): void;
}

interface Entry<T> {
  handle: T;
  holders: number;
  lingerTimer: ReturnType<typeof setTimeout> | null;
}

export function liveHandleRegistry<T extends LiveHandle>({
  lingerMs,
  isSpent,
}: LiveHandleRegistryOptions<T>): LiveHandleRegistry<T> {
  const open = new Map<string, Entry<T>>();

  const stopLinger = (entry: Entry<T>): void => {
    if (entry.lingerTimer !== null) clearTimeout(entry.lingerTimer);
    entry.lingerTimer = null;
  };

  return {
    acquire(key, create) {
      let entry = open.get(key);
      if (entry !== undefined && entry.holders === 0 && isSpent(entry.handle) && !entry.handle.holdsUnacknowledged()) {
        stopLinger(entry);
        entry.handle.close();
        open.delete(key);
        entry = undefined;
      }
      if (entry === undefined) {
        entry = { handle: create(), holders: 0, lingerTimer: null };
        open.set(key, entry);
      }
      const held = entry;
      held.holders += 1;
      stopLinger(held);
      let released = false;
      return {
        handle: held.handle,
        release: () => {
          if (released) return;
          released = true;
          held.holders -= 1;
          if (held.holders > 0) return;
          const linger = (): void => {
            held.lingerTimer = setTimeout(() => {
              held.lingerTimer = null;
              if (held.holders !== 0 || open.get(key) !== held) return;
              // Edits nobody has acknowledged keep it open, and trying.
              if (held.handle.holdsUnacknowledged()) {
                linger();
                return;
              }
              held.handle.close();
              open.delete(key);
            }, lingerMs);
          };
          linger();
        },
      };
    },
    closeAll() {
      for (const entry of open.values()) {
        stopLinger(entry);
        entry.handle.close();
      }
      open.clear();
    },
  };
}
