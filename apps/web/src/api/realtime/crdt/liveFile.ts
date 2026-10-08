// A file co-edited live, held for as long as somebody shows it.
//
// One live channel per file node for the whole tab, shared by every editor
// that shows the file, and released half a minute after the last one lets go
// so switching between two file tabs does not re-sync either. The handle
// starts `pending`, becomes `live` (with the channel and Loro, which an editor
// binds to) once the server has granted the channel and the document has
// synced, and ends in `fallback` when the file cannot be edited live: the
// server will not open it (`not_editable`, a refused subscribe, the lane off)
// or the browser cannot load Loro. The tab then shows the file read-only.

import type { AccountScope } from "../../../lib/accountScope";
import { holdLiveNode } from "../../events/machineRefresh";

import type { LiveDocChannel, LiveSocket } from "./channel";
import { LiveDocChannel as Channel } from "./channel";
import { liveHandleRegistry } from "./liveHandles";
import { loadLoro as defaultLoadLoro, type LoroApi } from "./loro";

/** How long an unused file's live channel lingers before it is let go. */
export const LIVE_FILE_LINGER_MS = 30_000;

export type LiveFileState =
  | { kind: "pending" }
  | { kind: "live"; channel: LiveDocChannel; loro: LoroApi }
  /** `unacknowledged`: what was typed here that the server never took, for
   *  the host to offer back (empty when nothing was). */
  | { kind: "fallback"; reason: string; unacknowledged: string };

export interface LiveFileDeps {
  socket: LiveSocket;
  loadLoro?: () => Promise<LoroApi>;
  /** Who is signed in, in which org: the edits a page leaves unacknowledged
   *  are kept for the next page under them. Never nobody: a host that does
   *  not know its reader yet does not open the file live. */
  account: AccountScope;
}

export class LiveFile {
  readonly channel: LiveDocChannel;
  private readonly loadLoro: () => Promise<LoroApi>;
  private _state: LiveFileState = { kind: "pending" };
  private readonly listeners = new Set<(state: LiveFileState) => void>();
  private releaseHold: (() => void) | null = null;

  constructor(
    readonly nodeId: string,
    deps: LiveFileDeps,
  ) {
    this.loadLoro = deps.loadLoro ?? defaultLoadLoro;
    this.channel = new Channel({
      socket: deps.socket,
      loadLoro: this.loadLoro,
      docType: "file",
      docId: nodeId,
      account: deps.account,
    });
    this.channel.listen({
      phase: (phase, fallback) => {
        // Loro failing to load here is the same answer as failing to load
        // in the channel: the file is shown the ordinary way.
        if (phase === "live") {
          this.goLive().catch(() => this.set({ kind: "fallback", reason: "load_failed", unacknowledged: "" }));
        } else if (phase === "fallback") {
          this.set({ kind: "fallback", reason: fallback?.reason ?? "closed", unacknowledged: fallback?.unacknowledged ?? "" });
        }
      },
    });
    this.channel.start();
  }

  get state(): LiveFileState {
    return this._state;
  }

  private async goLive(): Promise<void> {
    if (this.isLive()) return;
    const loro = await this.loadLoro();
    // The channel may have fallen back, or another sync gone live, meanwhile.
    if (this.channel.phase !== "live" || this.isLive()) return;
    this.set({ kind: "live", channel: this.channel, loro });
  }

  private isLive(): boolean {
    return this._state.kind === "live";
  }

  /** Whether letting this file go would lose something typed in it: see
   *  `LiveDocChannel.holdsUnacknowledged`, and a fallback's offer of text no
   *  subscriber has been handed yet. */
  holdsUnacknowledged(): boolean {
    return this.channel.holdsUnacknowledged || this.unseenOffer;
  }

  private unseenOffer = false;

  private set(state: LiveFileState): void {
    // While live, the document shows its own write-backs: the drive's frames
    // for them fetch nothing in this tab (the item is only marked stale).
    if (state.kind === "live" && this.releaseHold === null) this.releaseHold = holdLiveNode(this.nodeId);
    if (state.kind !== "live") this.dropHold();
    this._state = state;
    this.unseenOffer = state.kind === "fallback" && state.unacknowledged !== "" && this.listeners.size === 0;
    this.listeners.forEach((l) => l(state));
  }

  subscribe(listener: (state: LiveFileState) => void): () => void {
    this.listeners.add(listener);
    this.unseenOffer = false;
    listener(this._state);
    return () => void this.listeners.delete(listener);
  }

  private dropHold(): void {
    this.releaseHold?.();
    this.releaseHold = null;
  }

  close(): void {
    this.dropHold();
    this.channel.close();
    this.listeners.clear();
  }
}

const files = liveHandleRegistry<LiveFile>({
  lingerMs: LIVE_FILE_LINGER_MS,
  isSpent: (file) => file.state.kind === "fallback",
});

/** Hold `nodeId`'s live file; the returned release lets go. A file that fell
 *  back is opened afresh on the next hold (it may be editable again). */
export function acquireLiveFile(nodeId: string, deps: LiveFileDeps): { file: LiveFile; release: () => void } {
  const { handle, release } = files.acquire(nodeId, () => new LiveFile(nodeId, deps));
  return { file: handle, release };
}

/** Let go of every live file (tests, and the end of a session). */
export function closeAllLiveFiles(): void {
  files.closeAll();
}
