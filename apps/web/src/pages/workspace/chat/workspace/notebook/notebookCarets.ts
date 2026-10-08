// Everybody's carets in a notebook: one entry per Loro peer for the whole
// document (the server accepts only the sender's own key), whose cursors name
// the cell text they stand in. This tab publishes the caret of whichever cell
// editor has focus, and draws each remote caret in the one cell whose text its
// cursor names. Who is in which cell is read from the same entries.

import type { EphemeralStamp, LiveDocChannel, Timers } from "@/api/realtime/crdt/channel";
import { DEFAULT_TIMERS } from "@/api/realtime/crdt/channel";
import type { EditorCaret, LoroCodeMirrorBinding } from "@/api/realtime/crdt/codeMirrorBinding";
import type { LoroApi } from "@/api/realtime/crdt/loro";
import { ANONYMOUS, CARET_REFRESH_MS, CARET_TIMEOUT_MS } from "@/api/realtime/crdt/textBinding";

type EphemeralStore = InstanceType<LoroApi["EphemeralStore"]>;

interface CaretValue {
  anchor?: Uint8Array;
  focus?: Uint8Array;
  /** The cell the caret stands in, so presence needs no cursor resolution. */
  cell?: string;
}

/** Someone's caret, and the cell it stands in. */
export interface CellPresence {
  peer: string;
  cellId: string;
  stamp: EphemeralStamp;
}

export class NotebookCarets {
  private store: EphemeralStore;
  private readonly bindings = new Map<string, LoroCodeMirrorBinding>();
  private readonly stamps = new Map<string, EphemeralStamp>();
  private readonly hidden = new Set<string>();
  private readonly moves = new Map<string, { where: string; count: number }>();
  private readonly listeners = new Set<() => void>();
  private stopStore: (() => void) | null = null;
  private stopLocal: (() => void) | null = null;
  private readonly unlisten: () => void;
  private refresh: unknown = null;
  private presenceCache: readonly CellPresence[] = [];
  private readonly timers: Timers;

  constructor(
    private readonly channel: LiveDocChannel,
    private readonly loro: LoroApi,
    private readonly hueOf: (who: { userId: string; email: string }) => number,
    timers: Timers = DEFAULT_TIMERS,
  ) {
    this.timers = timers;
    this.store = this.newStore();
    this.unlisten = channel.listen({
      ephemeral: (data, stamp) => {
        this.stamps.set(String(stamp.loroPeer), stamp);
        this.store.apply(data);
        this.redraw();
      },
      gone: (peer) => {
        this.hidden.add(String(peer));
        this.redraw();
      },
      replaced: () => {
        this.stopStore?.();
        this.stopLocal?.();
        this.store = this.newStore();
        this.publish();
      },
    });
    this.arm();
  }

  private newStore(): EphemeralStore {
    const store = new this.loro.EphemeralStore(CARET_TIMEOUT_MS);
    this.stopStore = store.subscribe((event) => {
      if (event.by === "local") return;
      if (event.by === "import") for (const key of [...event.added, ...event.updated]) this.hidden.delete(key);
      this.redraw();
    });
    this.stopLocal = store.subscribeLocalUpdates((bytes: Uint8Array) => this.channel.sendEphemeral(bytes));
    return store;
  }

  private arm(): void {
    this.refresh = this.timers.setTimeout(() => {
      this.publish();
      this.arm();
    }, CARET_REFRESH_MS);
  }

  /** A cell's editor joined: its selection is published while it has focus,
   *  and remote carets in its text are drawn in it. */
  attach(cellId: string, binding: LoroCodeMirrorBinding): () => void {
    this.bindings.set(cellId, binding);
    this.redraw();
    return () => {
      if (this.bindings.get(cellId) === binding) this.bindings.delete(cellId);
      this.publish();
    };
  }

  /** This tab's caret: the focused cell's selection, or none. */
  publish(): void {
    const peer = this.channel.peer;
    if (peer === null || !this.channel.canWrite) return;
    const key = String(peer);
    for (const [cellId, binding] of this.bindings) {
      const cursors = binding.selectionCursors();
      if (cursors !== null) {
        this.store.set(key, { anchor: cursors.anchor, focus: cursors.focus, cell: cellId });
        return;
      }
    }
    if (this.store.get(key) !== undefined) this.store.delete(key);
  }

  private remote(): [string, CaretValue, EphemeralStamp][] {
    const mine = this.channel.peer === null ? null : String(this.channel.peer);
    const states = this.store.getAllStates() as Record<string, CaretValue | undefined>;
    const out: [string, CaretValue, EphemeralStamp][] = [];
    for (const [key, value] of Object.entries(states)) {
      if (key === mine || !value?.focus || this.hidden.has(key)) continue;
      const stamp = this.stamps.get(key);
      if (stamp !== undefined) out.push([key, value, stamp]);
    }
    return out;
  }

  /** Who stands in which cell now: the same array until it changes, so a
   *  React store can read it. */
  presence(): readonly CellPresence[] {
    return this.presenceCache;
  }

  private computePresence(): CellPresence[] {
    const out: CellPresence[] = [];
    for (const [peer, value, stamp] of this.remote()) {
      if (typeof value.cell === "string") {
        out.push({ peer, cellId: value.cell, stamp });
        continue;
      }
      for (const [cellId, binding] of this.bindings) {
        if (binding.editorPosition(value.focus!) !== null) {
          out.push({ peer, cellId, stamp });
          break;
        }
      }
    }
    return out;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => void this.listeners.delete(listener);
  }

  private redraw(): void {
    const remote = this.remote();
    for (const binding of this.bindings.values()) {
      const carets: EditorCaret[] = [];
      for (const [peer, value, stamp] of remote) {
        const head = binding.editorPosition(value.focus!);
        if (head === null) continue;
        const anchor = value.anchor ? (binding.editorPosition(value.anchor) ?? head) : head;
        const where = `${value.focus!.join(",")}|${value.anchor?.join(",") ?? ""}`;
        const seen = this.moves.get(peer);
        const count = seen === undefined ? 1 : seen.where === where ? seen.count : seen.count + 1;
        this.moves.set(peer, { where, count });
        carets.push({
          id: peer,
          name: stamp.displayName.trim() || ANONYMOUS,
          hue: this.hueOf({ userId: stamp.userId, email: stamp.email }),
          head,
          anchor,
          moves: count,
        });
      }
      binding.showCarets(carets);
    }
    const presence = this.computePresence();
    const same =
      presence.length === this.presenceCache.length &&
      presence.every((p, i) => p.peer === this.presenceCache[i]!.peer && p.cellId === this.presenceCache[i]!.cellId);
    if (same) return;
    this.presenceCache = presence;
    this.listeners.forEach((l) => l());
  }

  dispose(): void {
    // The tab is leaving the notebook: its caret goes now, so nobody waits
    // out the timeout for a person who is no longer in a cell.
    const peer = this.channel.peer;
    if (peer !== null && this.channel.canWrite && this.store.get(String(peer)) !== undefined) {
      this.store.delete(String(peer));
    }
    this.unlisten();
    if (this.refresh !== null) this.timers.clearTimeout(this.refresh);
    this.stopStore?.();
    this.stopLocal?.();
    this.bindings.clear();
    this.listeners.clear();
  }
}
