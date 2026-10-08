// Am I looking at what the machine has right now, or at the last copy that
// reached storage?
//
// A lease means a live machine is holding a folder and writing it back. That is
// one row on the server and several different things to a reader: the machine
// may be writing continuously (live), it may be holding the folder but only
// checkpointing it (a saved copy), it may have stopped answering (a saved copy,
// and say so), or it may be in the middle of handing the folder back. Painting
// "Live" over any of the last three is a promise the page cannot keep, and the
// person edits a file that the hand-back then overwrites.
//
// So the judgement lives here, once, as a pure function over the item the API
// already sent. Nothing fetches; the rail, the folder header and the preview
// modal all read the same verdict, and a rule can be inverted in a test.

import type { Item } from "@/api/files";

import { SOMEONE, SOMEWHERE, leaseName, type LeaseFacet } from "../useLeaseFacet";

export type FolderLiveness =
  | {
      state: "live";
      holder: string;
      machine: string;
      since: string | null;
      /** Files under the lease whose bytes have not reached the drive yet. */
      landing: number;
      /** Files that are on the machine and have NOT been sent — too big, or
       *  past the bandwidth window. */
      onBox: number;
    }
  | {
      state: "persisted";
      /** When the copy on screen was written, when that is known. */
      asOf: string | null;
      reason: "no-lease" | "not-live" | "stale" | "machine-unreachable" | "offline";
      /** The machine that went quiet, for the one reason that names it. */
      machine?: string;
    }
  | { state: "handing-back"; holder: string; machine: string };

const at = (iso: unknown): number | null => {
  if (typeof iso !== "string" || iso === "") return null;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms;
};

const text = (value: unknown): string | null =>
  typeof value === "string" && value !== "" ? value : null;

/**
 * Whether a machine holds this node right now, so that the Files page treats
 * it as read-only.
 *
 * Read off the item's own lease facet, which every node under a leased folder
 * carries, so a subfolder answers the same as the folder the lease is on. The
 * lease has to be one a machine writes on the live plane, and it has to be
 * unfinished: a `live` lease until it expires, and a hand-back until the flush
 * lands. Whether the holder is behind (`stale`) changes nothing: the lease on
 * the folder has not ended, the machine is still its writer, and a write made
 * from here still lands on top of whatever the machine sends next. A lease on
 * the checkpoint plane (a mount on somebody's laptop) is not this: the server
 * fences those itself and the page keeps offering the write.
 *
 * It is not the badge's verdict. A dead event stream makes the badge show the
 * saved copy, but the machine still owns the folder and the folder is still
 * read-only here.
 */
export function isHeldByMachine(item: Item | undefined | null, now: number): boolean {
  const lease = item?.lease as LeaseFacet | null | undefined;
  if (!lease) return false;
  const expires = at(lease.expires_at);
  if (expires !== null && expires <= now) return false;
  const grantable = at(lease.grantable_after);
  if (grantable !== null && expires !== null && grantable >= expires) return true;
  return lease.live === true;
}

/**
 * Whether a write from this page into the node is refused because a machine
 * holds it. A lease that admits inbound writes takes them from the web and
 * hands them to the machine, and the drive keeps both sides when they cross,
 * so only a lease that does not admit them turns the page read-only: the
 * server refuses every such write at the door, and offering it would only
 * lead to that refusal.
 */
export function refusesWebWrites(item: Item | undefined | null, now: number): boolean {
  if (!isHeldByMachine(item, now)) return false;
  const lease = item?.lease as LeaseFacet | null | undefined;
  return lease?.inbound !== true;
}

/** The chat whose folder a lease holds, as the facet names it. */
export interface ChatLease {
  chatId: string;
  /** The chat's current title; empty when the server did not resolve one. */
  title: string;
  /** Whether the reader may open the chat, by the chat's own policy. */
  canOpen: boolean;
}

/**
 * The chat holding this node's folder, or `null` when its lease is not a
 * chat's (or there is none). Read field by field off the facet's open bag
 * rather than off typed slots, so a build that predates the fields reads
 * `null` here instead of rendering a link to nowhere.
 */
export function chatLeaseOf(item: Item | undefined | null): ChatLease | null {
  const lease = item?.lease as LeaseFacet | null | undefined;
  if (!lease) return null;
  const chatId = text(lease.chat_id);
  if (chatId === null) return null;
  return {
    chatId,
    title: text(lease.chat_title) ?? "",
    canOpen: lease.can_open_chat === true,
  };
}

/**
 * What this node's lease means to a reader, at `now`.
 *
 * The verdict is the server's: the lease facet carries the status it decided
 * (live, sync paused and why, or a saved copy), from the lease's beat, its
 * last sync and whether the holder's machine still answers. Nothing here
 * judges those raw fields again; the state picks which of the sentences in
 * `liveCopy.ts` a reader is shown. A facet with no status (a server that
 * predates it) is never shown live.
 *
 * `onBox` is counted by the caller from the rows it has in view — the facet on
 * one folder cannot know what its children are holding back, and the header is
 * describing the listing under it.
 */
export function liveState(item: Item, now: number, onBox = 0): FolderLiveness {
  const lease = item.lease as LeaseFacet | null | undefined;
  if (!lease) return { state: "persisted", asOf: text(item.attrs?.mtime), reason: "no-lease" };

  const synced = text(lease.last_sync_at);
  const holder = leaseName(lease.holder_name, lease.holder, SOMEONE);
  const machine = leaseName(lease.machine_name, lease.machine, SOMEWHERE);

  // The item this page holds outlived its lease: whatever the server said
  // about the lease when it sent the item, the lease is over now. A facet with
  // no expiry at all is not expired.
  const expires = at(lease.expires_at);
  if (expires !== null && expires <= now) {
    return { state: "persisted", asOf: synced, reason: "not-live" };
  }

  const status = lease.status ?? null;
  switch (status?.state) {
    case "live":
      return {
        state: "live",
        holder,
        machine,
        since: text(lease.since),
        landing: landingOf(item),
        onBox,
      };
    case "sync_paused":
      return {
        state: "persisted",
        asOf: synced,
        reason: pausedReason(status?.reason_code),
        machine,
      };
    default:
      return { state: "persisted", asOf: synced, reason: "not-live" };
  }
}

/** Which of the saved-copy sentences a paused sync reads as, by the server's
 *  reason. A reason this build does not know reads as the machine gone quiet. */
function pausedReason(reason: string | undefined): "stale" | "machine-unreachable" | "offline" {
  switch (reason) {
    case "behind":
      return "stale";
    case "machine_unreachable":
      return "machine-unreachable";
    default:
      return "offline";
  }
}

/** The files still landing under a live lease: the server's own count, and
 *  from a server that predates it, the in-flight rows it did count. */
function landingOf(item: Item): number {
  const lease = item?.lease ?? null;
  if (typeof lease?.landing_count === "number") return lease.landing_count;
  return typeof lease?.pending === "number" ? lease.pending : 0;
}
