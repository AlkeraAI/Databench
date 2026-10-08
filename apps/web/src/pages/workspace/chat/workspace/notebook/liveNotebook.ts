// A notebook held live for as long as somebody shows it: one channel per
// notebook node for the whole browser tab, released half a minute after the
// last view lets go (switching tabs does not re-sync). The same lifecycle as a
// live text file, on the `notebook` document type.

import { LiveDocChannel, type LiveSocket } from "@/api/realtime/crdt/channel";
import type { AccountScope } from "@/lib/accountScope";
import { liveHandleRegistry, type LiveHandleRegistry } from "@/api/realtime/crdt/liveHandles";
import { loadLoro as defaultLoadLoro, type LoroApi } from "@/api/realtime/crdt/loro";
import { NotebookDocument, notebookUnacknowledged } from "@/api/realtime/crdt/notebookDoc";

export const LIVE_NOTEBOOK_LINGER_MS = 30_000;

export type LiveNotebookState =
  | { kind: "pending" }
  | { kind: "live"; channel: LiveDocChannel; loro: LoroApi; doc: NotebookDocument }
  /** `unacknowledged`: the cells typed here the server never took, for the
   *  view to offer back (empty when nothing was). */
  | { kind: "fallback"; reason: string; detail?: string; unacknowledged: string };

export interface LiveNotebookDeps {
  socket: LiveSocket;
  loadLoro?: () => Promise<LoroApi>;
  /** Who is signed in, in which org: the edits a page leaves unacknowledged
   *  are kept for the next page under them. */
  account: AccountScope | null;
}

export class LiveNotebook {
  readonly channel: LiveDocChannel;
  private readonly loadLoro: () => Promise<LoroApi>;
  private _state: LiveNotebookState = { kind: "pending" };
  private readonly listeners = new Set<(state: LiveNotebookState) => void>();

  constructor(
    readonly nodeId: string,
    deps: LiveNotebookDeps,
  ) {
    this.loadLoro = deps.loadLoro ?? defaultLoadLoro;
    this.channel = new LiveDocChannel({
      socket: deps.socket,
      loadLoro: this.loadLoro,
      docType: "notebook",
      docId: nodeId,
      account: deps.account,
      // A reset or a new epoch offers back the cells it would take.
      unacknowledged: notebookUnacknowledged,
    });
    this.channel.listen({
      phase: (phase, fallback) => {
        if (phase === "live") {
          this.goLive().catch(() => this.set({ kind: "fallback", reason: "load_failed", unacknowledged: "" }));
        } else if (phase === "fallback") {
          const detail = fallback?.detail;
          this.set({
            kind: "fallback",
            reason: fallback?.reason ?? "closed",
            ...(detail !== undefined ? { detail } : {}),
            unacknowledged: fallback?.unacknowledged ?? "",
          });
        }
      },
    });
    this.channel.start();
  }

  get state(): LiveNotebookState {
    return this._state;
  }

  private async goLive(): Promise<void> {
    if (this._state.kind === "live") return;
    const loro = await this.loadLoro();
    if (this.channel.phase !== "live" || this.isLive()) return;
    this.set({ kind: "live", channel: this.channel, loro, doc: new NotebookDocument({ loro, source: this.channel }) });
  }

  private isLive(): boolean {
    return this._state.kind === "live";
  }

  /** Whether letting this notebook go would lose something typed in it: see
   *  `LiveDocChannel.holdsUnacknowledged`, and a fallback's offer of cells no
   *  subscriber has been handed yet. */
  holdsUnacknowledged(): boolean {
    return this.channel.holdsUnacknowledged || this.unseenOffer;
  }

  private unseenOffer = false;

  private set(state: LiveNotebookState): void {
    const previous = this._state;
    if (previous.kind === "live" && state.kind !== "live") previous.doc.dispose();
    this._state = state;
    this.unseenOffer = state.kind === "fallback" && state.unacknowledged !== "" && this.listeners.size === 0;
    this.listeners.forEach((l) => l(state));
  }

  subscribe(listener: (state: LiveNotebookState) => void): () => void {
    this.listeners.add(listener);
    this.unseenOffer = false;
    listener(this._state);
    return () => void this.listeners.delete(listener);
  }

  close(): void {
    if (this._state.kind === "live") this._state.doc.dispose();
    this.channel.close();
    this.listeners.clear();
  }
}

/** Open notebooks by socket, then node: a notebook is shared by every view on
 *  the same socket, never across sockets (each socket is its own peer). */
const registries = new Map<LiveSocket, LiveHandleRegistry<LiveNotebook>>();

function registryFor(socket: LiveSocket): LiveHandleRegistry<LiveNotebook> {
  let registry = registries.get(socket);
  if (registry === undefined) {
    registry = liveHandleRegistry<LiveNotebook>({
      lingerMs: LIVE_NOTEBOOK_LINGER_MS,
      isSpent: (notebook) => notebook.state.kind === "fallback",
    });
    registries.set(socket, registry);
  }
  return registry;
}

/** Hold `nodeId`'s live notebook on `deps.socket`; the returned release lets go.
 *  A notebook that fell back is opened afresh on the next hold. */
export function acquireLiveNotebook(nodeId: string, deps: LiveNotebookDeps): { notebook: LiveNotebook; release: () => void } {
  const { handle, release } = registryFor(deps.socket).acquire(nodeId, () => new LiveNotebook(nodeId, deps));
  return { notebook: handle, release };
}

export function closeAllLiveNotebooks(): void {
  for (const registry of registries.values()) registry.closeAll();
  registries.clear();
}
