// What the browser knows about a lease, read off the item the API already sent.
//
// Nothing here fetches. The `lease` facet rides every item in a listing and the
// delta feed re-delivers it, so the badge is pushed, never polled; the only
// thing that ticks is the local clock, so "synced 12 s ago" keeps counting
// between two frames without a request.

import { useEffect, useState } from "react";

import type { components } from "@alkera/sdk";

import type { Item } from "@/api/files";
import { ApiError, refusalSentence } from "@/api/errors";

import type { FilesErrorContext } from "@/lib/files/errors";

/**
 * The lease facet as the wire carries it, taken FROM the wire's own schema.
 *
 * It was hand-typed here once, and had already drifted: the server had grown a
 * fourth purpose the copy did not list. Deriving it means a field the server
 * adds — or a purpose it stops sending — is a typecheck failure at the reader,
 * not a badge that quietly renders the wrong thing. The generated type keeps the
 * facet's `extra="allow"` index signature, so a field this build predates still
 * reads through it.
 */
export type LeaseFacet = components["schemas"]["LeaseFacet"];

/** Every lease action a capability can gate, in the wire's own spelling. */
export type LeaseAction = "lease" | "lease_request" | "lease_force";

/** The shape of every id this server mints. A facet falls back to an id
 *  whenever the server resolved no name — and a box registers itself under its
 *  allocation id AND holds its own lease, so both halves of a lease line can be
 *  the same uuid. An id is not a thing a reader knows, so it is dropped here,
 *  once, rather than at each surface that renders a lease. */
const ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** What each party to a lease is called when the drive knows no name for it.
 *  A line reading "in use by  on " is worse than a vague one, and one naming a
 *  uuid is worse than both. */
export const SOMEONE = "someone";
export const SOMEWHERE = "the workspace machine";

const text = (value: unknown): string | null =>
  typeof value === "string" && value !== "" ? value : null;

/** The name to show for one party to a lease: the name the server resolved,
 *  else what the holder called itself, else what it IS. Never an id. */
export function leaseName(resolved: unknown, raw: unknown, role: string): string {
  const name = text(resolved);
  if (name !== null) return name;
  const called = text(raw);
  return called === null || ID.test(called) ? role : called;
}

/** The chat whose folder a lease holds, by title, or null when it holds none.
 *  A chat the server named only by id reads as a chat, never as the id. */
function chatName(facet: LeaseFacet): string | null {
  if (text(facet.chat_id) === null) return null;
  return text(facet.chat_title) ?? "a chat";
}

/** The glyph an avatar shows when its name carries no letter or digit. */
export const NO_INITIAL = "?";

/** The one character a holder's avatar carries: the first letter or digit of
 *  the name, so a chat titled "[walkthrough-2] Run" reads "W" rather than "[".
 *  Leading punctuation, symbols and whitespace are skipped. */
export function holderInitial(name: string): string {
  const first = /[\p{L}\p{N}]/u.exec(name);
  return first ? first[0].toLocaleUpperCase() : NO_INITIAL;
}

/**
 * Whether this caller may take `action` on this item.
 *
 * Unlike a listing's read capability, a lease action is refused unless the item
 * says otherwise: acquiring, requesting and forcing all change who owns a
 * folder, so a silent absence must not render a button that 403s on click.
 *
 * Read off the wire's own fields rather than a string looked up in a table.
 * This gate was once spelled against keys the server never sent, and the only
 * thing that noticed was a fixture that minted them; a named field is a
 * typecheck failure the day the wire stops carrying it.
 */
export function canLease(item: Item | undefined | null, action: LeaseAction): boolean {
  const caps = item?.capabilities;
  if (!caps) return false;
  if (action === "lease") return caps.can_lease === true;
  if (action === "lease_request") return caps.can_lease_request === true;
  return caps.can_lease_force === true;
}

/** The facet, when the item carries one. */
export function leaseFacet(item: Item | undefined | null): LeaseFacet | null {
  const facet = item?.lease as LeaseFacet | undefined | null;
  if (!facet) return null;
  return facet;
}

/**
 * What a `files.leased` refusal on this item names, read off its lease facet: the
 * holder and the machine by name — never an id — and whether the holder acts for
 * the reader. For the reader's own chat the holder is the chat's title, for their own
 * box its name; for anyone else the chat a box holds the folder for, else the person.
 * Empty when the item carries no lease, so the copy falls back to its vaguer sentence.
 */
export function leaseRefusalContext(
  item: Item | undefined | null,
): Pick<FilesErrorContext, "holder" | "machine" | "yours"> {
  const facet = leaseFacet(item);
  if (!facet) return {};
  const machine = nameOrNothing(facet.machine_name, facet.machine);
  const yours = facet.yours;
  if (yours === "you") return defined({ yours, machine });
  if (yours === "chat") return defined({ yours, holder: nameOrNothing(facet.chat_title) });
  if (yours === "box") return defined({ yours, holder: nameOrNothing(facet.machine_name) });
  const chat = chatName(facet);
  const holder = chat === null ? nameOrNothing(facet.holder_name, facet.holder) : `the chat ${chat}`;
  return defined({ holder, machine });
}

/** The same fields, without the ones that came to nothing — so a page's own context
 *  is not overwritten with a blank by a facet that had no name to give. */
function defined<T extends object>(fields: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(fields).filter(([, value]) => value !== undefined),
  ) as Partial<T>;
}

/** A name to put in a sentence, or undefined for an empty value or a bare id. */
function nameOrNothing(...candidates: unknown[]): string | undefined {
  for (const candidate of candidates) {
    const value = text(candidate);
    if (value !== null && !ID.test(value)) return value;
  }
  return undefined;
}

/** A local clock that re-renders on an interval, so a relative time stays live
 *  while the network stays quiet. One timer per mount; cleared on unmount. */
export function useNow(everyMs = 1_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(timer);
  }, [everyMs]);
  return now;
}

/** "12 s ago", "3 min ago", "2 h ago", "4 d ago" — the coarsest unit that still
 *  reads as a duration, so a badge does not flicker digit by digit for an hour.
 *  A time in the future reads as "just now": a clock skewed forward is not news. */
export function relativeTime(iso: string | null | undefined, now: number): string | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  const seconds = Math.floor((now - then) / 1000);
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.floor(hours / 24)} d ago`;
}

/** The wall-clock time a lease started, as the badge shows it ("10:12"). */
export function clockTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  return at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/** Everything the badge, the read-only state and the right pane render. */
export interface LeaseView {
  facet: LeaseFacet;
  /** Held by the person looking at it. */
  mine: boolean;
  /** The server's own verdict that the holder is alive but behind. */
  stale: boolean;
  /** The holder, by name. Never an id. */
  holder: string;
  /** The machine it is held on, by name. Never an id. */
  machine: string;
  /** The chat whose folder the lease holds, by title; null when it holds none. */
  chat: string | null;
  /** The party every sentence names: the chat a box holds the folder for, else
   *  the holder. Never an id. */
  holderLabel: string;
  since: string | null;
  synced: string | null;
  expires: string | null;
  /** "In use by chat Revenue model" — the badge's whole sentence. */
  summary: string;
}

/**
 * The badge's whole sentence: who has the folder, and nothing else.
 *
 * The party named is the chat the box holds the folder for, else the holder;
 * the machine, when the lease started and how long ago it synced are for the
 * pane and the leases list, not for a banner over every listing — "In use by
 * the chat … on demo-box · since 10:33 PM · synced 7 min ago" was a
 * line nobody read to the end. A box registers under its allocation id and
 * holds its own lease, so a lease with no chat whose holder IS the machine
 * names the machine once rather than "X on X".
 */
function leaseSentence(mine: boolean, holder: string, machine: string, chat: string | null): string {
  if (mine) return "In use by you";
  if (chat !== null) return `In use by chat ${chat}`;
  if (holder === machine) return `In use on ${machine}`;
  return `In use by ${holder}`;
}

/** The item's lease as one render-ready shape, or null when it is not leased.
 *  `stale` comes off the item, not the facet: it is server-computed from the
 *  holder's heartbeat against its last sync, which the client cannot recompute. */
export function useLeaseView(item: Item | undefined | null, everyMs = 1_000): LeaseView | null {
  const now = useNow(everyMs);
  const facet = leaseFacet(item);
  if (!facet) return null;
  const holder = leaseName(facet.holder_name, facet.holder, SOMEONE);
  const machine = leaseName(facet.machine_name, facet.machine, SOMEWHERE);
  const chat = chatName(facet);
  const holderLabel = chat === null ? holder : `the chat ${chat}`;
  const mine = facet.mine === true;
  return {
    facet,
    mine,
    stale: item?.stale === true,
    holder,
    machine,
    chat,
    holderLabel,
    since: clockTime(facet.since),
    synced: relativeTime(facet.last_sync_at, now),
    expires: clockTime(facet.expires_at),
    summary: leaseSentence(mine, holder, machine, chat),
  };
}

/**
 * The inline message for a write refused because the folder is leased.
 *
 * The server's `files.leased` envelope names the holder, and `ApiError` has
 * already parsed it — so the browser shows that sentence instead of its own
 * generic failure. Any other failure returns null and keeps its own handling.
 */
export function leasedRefusal(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  if (error.code !== "files.leased") return null;
  return refusalSentence(error);
}
