// A chat's shared draft on the live lane, held for as long as somebody shows it.
//
// One live channel per chat for the whole tab, shared by every composer that
// shows the chat (the dock and anything else) and released half a minute
// after the last one lets go, so flipping between two chats does not re-sync
// either. The handle starts `pending`, becomes `live` with a binding once the
// server has granted the channel and the document has synced, and ends in
// `fallback` when the lane cannot run (the server cannot serve it, or the
// browser cannot load Loro) — the caller then keeps the field to the tab,
// starting from what the live draft held so nothing typed is lost.

import { safeSessionStorage, type SafeStorage } from "@alkera/ui/storage";

import { isAccountKeyOf, type AccountScope } from "../../../lib/accountScope";

import type { LiveFallback, LiveSocket } from "./channel";
import { LiveDocChannel, UNLOAD_STASH_FAMILY } from "./channel";
import { liveHandleRegistry } from "./liveHandles";
import { loadLoro as defaultLoadLoro, type LoroApi } from "./loro";
import { LoroTextBinding } from "./textBinding";

/** How long an unused chat's live draft lingers before it is let go. */
export const LIVE_DRAFT_LINGER_MS = 30_000;

export type LiveDraftState =
  | { kind: "pending" }
  | { kind: "live"; binding: LoroTextBinding }
  | { kind: "fallback"; fallback: LiveFallback };

export interface LiveDraftDeps {
  socket: LiveSocket;
  loadLoro?: () => Promise<LoroApi>;
  hueOf: (who: { userId: string; email: string }) => number;
  /** Who is signed in and in which org; the unsent-edits stash is kept under
   *  them. */
  account: AccountScope | null;
}

export class LiveDraft {
  private readonly channel: LiveDocChannel;
  private readonly loadLoro: () => Promise<LoroApi>;
  private readonly hueOf: LiveDraftDeps["hueOf"];
  private _state: LiveDraftState = { kind: "pending" };
  private readonly listeners = new Set<(state: LiveDraftState) => void>();
  private binding: LoroTextBinding | null = null;
  /** The channel has been live once, so the grant it was opened with is
   *  known and a change from here on is a change of role. */
  private everLive = false;

  constructor(
    readonly chatId: string,
    deps: LiveDraftDeps,
  ) {
    this.loadLoro = deps.loadLoro ?? defaultLoadLoro;
    this.hueOf = deps.hueOf;
    this.channel = new LiveDocChannel({
      socket: deps.socket,
      loadLoro: this.loadLoro,
      docType: "chat_draft",
      docId: chatId,
      account: deps.account,
    });
    this.channel.listen({
      phase: (phase, fallback) => {
        if (phase === "live") {
          this.everLive = true;
          void this.goLive();
        }
        else if (phase === "fallback") {
          this.binding?.dispose();
          this.binding = null;
          this.set({
            kind: "fallback",
            fallback: fallback ?? { reason: "closed", localText: "", ackedText: "", unacknowledged: "" },
          });
        }
      },
    });
    this.channel.start();
  }

  get state(): LiveDraftState {
    return this._state;
  }

  private async goLive(): Promise<void> {
    if (this.binding !== null) return;
    const loro = await this.loadLoro();
    if (this.binding !== null || this.channel.phase !== "live") return;
    this.binding = new LoroTextBinding({ channel: this.channel, loro, hueOf: this.hueOf });
    this.set({ kind: "live", binding: this.binding });
  }

  private set(state: LiveDraftState): void {
    this._state = state;
    this.unseenFallback =
      state.kind === "fallback" && state.fallback.unacknowledged !== "" && this.listeners.size === 0;
    this.listeners.forEach((l) => l(state));
  }

  /** The draft fell back holding text the server never took while no composer
   *  showed it: nobody has been handed that text yet. */
  private unseenFallback = false;

  /** The server changed whether this reader may write in the chat, after the
   *  grant the channel opened with: an owner raised or lowered their role. The
   *  first grant is not reported, only a change to it. */
  onGrantChange(listener: (canWrite: boolean) => void): () => void {
    return this.channel.listen({
      writable: (canWrite) => {
        if (this.everLive) listener(canWrite);
      },
    });
  }

  subscribe(listener: (state: LiveDraftState) => void): () => void {
    this.listeners.add(listener);
    this.unseenFallback = false;
    listener(this._state);
    return () => void this.listeners.delete(listener);
  }

  /** Whether letting this draft go would lose something typed in it: see
   *  `LiveDocChannel.holdsUnacknowledged`, and a fallback whose text no
   *  composer has been handed yet (the composer that takes it keeps it). */
  holdsUnacknowledged(): boolean {
    return this.channel.holdsUnacknowledged || this.unseenFallback;
  }

  close(): void {
    this.binding?.dispose();
    this.binding = null;
    this.channel.close();
    this.listeners.clear();
  }
}

const drafts = liveHandleRegistry<LiveDraft>({
  lingerMs: LIVE_DRAFT_LINGER_MS,
  isSpent: (draft) => draft.state.kind === "fallback",
});

/** Hold `chatId`'s live draft; the returned release lets go. A draft that
 *  already fell back is opened afresh on the next hold (the server may be back). */
export function acquireLiveDraft(chatId: string, deps: LiveDraftDeps): { draft: LiveDraft; release: () => void } {
  const { handle, release } = drafts.acquire(chatId, () => new LiveDraft(chatId, deps));
  return { draft: handle, release };
}

/** Forget every live draft (tests, sign-out). */
export function closeAllLiveDrafts(): void {
  drafts.closeAll();
}

/** Sign-out: nothing of the account that left survives for the next one —
 *  not a live draft held open in memory, nor edits stashed for the next page. */
export function forgetLiveDrafts(storage: Pick<SafeStorage, "keys" | "remove"> = safeSessionStorage()): void {
  closeAllLiveDrafts();
  for (const key of storage.keys()) if (isAccountKeyOf(key, UNLOAD_STASH_FAMILY)) storage.remove(key);
}
