// Where a chat's queued messages are written down.
//
// A message typed while the agent works is held against the chat until the
// turn ends. Holding it only in memory would lose it to the thing a reader
// most often does while waiting — reload the tab, or close it and come back —
// and a message that vanishes is the same defect as a message that was never
// accepted. So the queue is written beside the chat's own draft state, per
// chat, in this browser, under the signed-in person and the org they are in:
// a queue typed in one org is never offered in another.
//
// Storage is the shared guarded one (`@alkera/ui/storage`): it throws outright
// in some embedded and privacy contexts, and a browser that refuses it opens
// the chat with an empty queue and still holds what is typed for as long as the
// page lives.

import { accountKey, safeLocalStorage } from "@alkera/ui/storage";

import type { AccountScope } from "@/lib/accountScope";

import type { SendOptions } from "./data";

/** One message waiting for the turn to end. */
export interface QueuedMessage {
  id: string;
  text: string;
  /** The picks the message was typed under — the model, effort and mode the
   *  composer was showing — so the send it becomes is the send it would have
   *  been had the reader waited. */
  opts?: SendOptions;
  /** This came back from a PREVIOUS page session, so nobody is watching for
   *  it and the turn it was waiting behind may be hours gone. It never sends
   *  itself: opening a chat must not put words in it. The reader's click is
   *  what clears this. Never persisted — every read stamps it, so a message
   *  that survives a second reload is restored again. */
  restored?: boolean;
  /** The reader stopped the turn this was waiting behind. The end of that turn
   *  is what would have released it, so without this the press to stop is what
   *  sent it — a second later, into the chat the reader had just halted. The
   *  words were never handed to the server, so nothing about this is in the
   *  transcript: it is held here, said to be not sent, and goes only if the
   *  reader says so. */
  stopped?: boolean;
  /** The reader sent this and the server could not take it (a 5xx, or no
   *  answer at all). It is held here, written down, so neither a reload nor a
   *  closed tab loses it, and it goes again on a timer or on the reader's
   *  Retry. Never released by a turn ending. */
  unsent?: boolean;
  /** The id the server dedupes this message by. Kept from the first attempt,
   *  so a retry of a send whose answer was lost is recorded once. */
  clientId?: string;
}

/** The chat id rides after the account key. A queue written before keys named
 *  the org is ignored, not migrated: these are conveniences, and an old one
 *  would only come back as a message the reader no longer expects. */
const keyFor = (scope: AccountScope, chatId: string): string =>
  `${accountKey(scope.userId, scope.orgId, "chat.queued")}:${chatId}`;

function isQueued(value: unknown): value is QueuedMessage {
  if (typeof value !== "object" || value === null) return false;
  const record = value as { id?: unknown; text?: unknown };
  return typeof record.id === "string" && typeof record.text === "string";
}

/** What this chat is holding, or an empty list — no stored value, an
 *  unreadable store, or anything that is not the shape we wrote.
 *
 *  Everything that comes back is marked `restored`: it was written by a page
 *  session that is gone, so it waits for the reader rather than sending itself
 *  the moment the chat is opened. */
export function readQueued(scope: AccountScope | null, chatId: string): QueuedMessage[] {
  // A shell that names nobody has no queue of its own to restore.
  if (!scope) return [];
  const parsed = safeLocalStorage().readJson(keyFor(scope, chatId));
  if (!Array.isArray(parsed)) return [];
  return parsed.filter(isQueued).map((held) => ({ ...held, restored: true }));
}

/** Record what this chat is holding. An empty queue removes the entry rather
 *  than leaving an empty list behind, so a chat that sent everything it was
 *  holding leaves nothing for a later reader to restore. */
export function writeQueued(
  scope: AccountScope | null,
  chatId: string,
  queued: readonly QueuedMessage[],
): void {
  // Nobody to file it under: the queue is held in memory for this page only.
  if (!scope) return;
  const storage = safeLocalStorage();
  // A browser that will not keep it still shows it for this session.
  if (queued.length === 0) storage.remove(keyFor(scope, chatId));
  else storage.writeJson(keyFor(scope, chatId), queued);
}

/** A send on the wire, written down before the request goes. */
export interface InFlightSend {
  clientId: string;
  text: string;
  opts?: SendOptions;
}

/** Filed under the same person and org as the queue, so a send left on the
 *  wire in one org is never offered back in another. */
const inFlightKey = (scope: AccountScope, chatId: string): string =>
  `${accountKey(scope.userId, scope.orgId, "chat.inflight")}:${chatId}`;

function isInFlight(value: unknown): value is InFlightSend {
  if (typeof value !== "object" || value === null) return false;
  const record = value as { clientId?: unknown; text?: unknown };
  return typeof record.clientId === "string" && typeof record.text === "string";
}

/** Sends this chat had on the wire when the page that made them went away. */
export function readInFlight(scope: AccountScope | null, chatId: string): InFlightSend[] {
  if (!scope) return [];
  const parsed = safeLocalStorage().readJson(inFlightKey(scope, chatId));
  if (!Array.isArray(parsed)) return [];
  return parsed.filter(isInFlight);
}

function writeInFlight(scope: AccountScope, chatId: string, sends: readonly InFlightSend[]): void {
  const storage = safeLocalStorage();
  if (sends.length === 0) storage.remove(inFlightKey(scope, chatId));
  else storage.writeJson(inFlightKey(scope, chatId), sends);
}

/** Write a send down before its request goes, so a page that dies while the
 *  request is open leaves the words behind for the next one. Nobody to file
 *  it under: it is not written down. */
export function holdInFlight(scope: AccountScope | null, chatId: string, send: InFlightSend): void {
  if (!scope) return;
  const rest = readInFlight(scope, chatId).filter((held) => held.clientId !== send.clientId);
  writeInFlight(scope, chatId, [...rest, send]);
}

/** The send was answered, and its words are wherever that answer put them. */
export function releaseInFlight(scope: AccountScope | null, chatId: string, clientId: string): void {
  if (!scope) return;
  const held = readInFlight(scope, chatId);
  const rest = held.filter((send) => send.clientId !== clientId);
  if (rest.length !== held.length) writeInFlight(scope, chatId, rest);
}

/** Forget every send a previous page left on the wire: the caller has moved
 *  them into the queue. */
export function clearInFlight(scope: AccountScope | null, chatId: string): void {
  if (!scope) return;
  writeInFlight(scope, chatId, []);
}
