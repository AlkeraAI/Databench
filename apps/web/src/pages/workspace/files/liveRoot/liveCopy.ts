// The sentences a reader is shown about a leased folder.
//
// Separate from the judgement that produces them (`liveness.ts`) so the wording
// can be read, reviewed and changed in one place, and so every surface that
// describes the same state says the same thing. Pure: a timestamp is formatted
// by a function the caller supplies, which is also what keeps these sentences
// pinnable without a locale.

import { capitalize } from "@/lib/format/text";

import { clockTime } from "../useLeaseFacet";

import type { ChatLease, FolderLiveness } from "./liveness";

export interface LiveCopyOptions {
  /** The server event stream is known to be down. With no stream the browser
   *  cannot learn that the folder changed, so it must stop claiming to be
   *  showing it live — whatever the lease itself says. */
  streamDown?: boolean;
  /** How a timestamp is spelled inside a sentence. */
  formatTime?: (iso: string) => string;
  /** The chat whose machine holds the folder, when one does. It is named in
   *  place of the machine: a chat is a thing the reader knows, can open and can
   *  end; a box's hostname is only where the work happens to be running. */
  chat?: ChatLease | null;
  /** The listing under the sentence IS that chat's own files, so naming the
   *  chat as a link would point the reader at what they are already reading. */
  inChat?: boolean;
}

/** Where a chat opens. The lease names the chat by id and nothing else, so the
 *  route is spelled here rather than read off an object facet the reader may
 *  not hold. */
export function chatHref(chatId: string): string {
  return `/chat/${encodeURIComponent(chatId)}`;
}

/** What the sentence calls a chat whose title the server did not resolve. */
const UNTITLED = "a chat";

/** What the sentence calls the chat when the reader is already inside it. */
export const THIS_CHAT = "this chat";

const defaultTime = (iso: string): string => clockTime(iso) ?? iso;

/** What a reader is told when a machine still holds the folder and the rows on
 *  screen are the copy that last reached storage.
 *
 *  One sentence for both ways that happens — the holder is behind, or the event
 *  stream is down — because the reader's position is the same in either: what is
 *  listed is real, it is not this second's, and it settles when the lease does. */
export const SAVED_COPY_LINE =
  "This is the last synced copy to your Files and is not always up to date until the lease on the folder ends.";

/** What the holder is working on, in the sentence's last slot. */
export interface LiveSubject {
  /** The words that fill the slot. */
  text: string;
  /** Where those words lead, or null when they are not a link. */
  href: string | null;
}

/**
 * The one sentence that leads, in the two pieces a renderer needs: everything
 * up to the thing being worked on, and that thing — which is a link when it is
 * a chat the reader may open and is not already inside.
 *
 * Split rather than returned whole because the sentence carries the only way to
 * the chat holding the folder, and a link cannot live inside a string. Every
 * state that names nothing to open puts the whole line in `lead`, so a caller
 * that only wants words joins the two and never branches.
 */
export interface LiveLine {
  lead: string;
  subject: LiveSubject | null;
}

/** The chat, when one holds the folder; else the machine, which is never a
 *  link — a hostname is not somewhere a reader can go. */
function subjectOf(machine: string, opts: LiveCopyOptions): LiveSubject {
  const chat = opts.chat ?? null;
  if (chat === null) return { text: machine, href: null };
  if (opts.inChat === true) return { text: THIS_CHAT, href: null };
  const name = chat.title === "" ? UNTITLED : chat.title;
  return { text: name, href: chat.canOpen ? chatHref(chat.chatId) : null };
}

export function livenessLine(liveness: FolderLiveness, opts: LiveCopyOptions = {}): LiveLine {
  const when = opts.formatTime ?? defaultTime;
  const settled = (lead: string): LiveLine => ({ lead, subject: null });
  if (opts.streamDown === true) return settled(SAVED_COPY_LINE);
  switch (liveness.state) {
    case "live":
      // The holder opens the sentence, and the drive's fallback for a holder it
      // resolved no name for is the word "someone" — so the first letter is
      // raised here, where the sentence begins, rather than spelled twice in the
      // fallback. Only the first letter moves, so a name the server did resolve
      // keeps its own casing.
      return {
        lead: `Live. ${capitalize(liveness.holder)} is working on `,
        subject: subjectOf(liveness.machine, opts),
      };
    case "handing-back":
      return settled("Handing back…");
    default:
      // The holder is alive and behind. Same sentence: the reader is looking at
      // the last synced copy either way, and the lease ending is what fixes it.
      if (liveness.reason === "stale") return settled(SAVED_COPY_LINE);
      if (liveness.reason === "offline") return settled(offlineLine(liveness.asOf, when));
      return settled(liveness.asOf === null ? "Last saved" : `Last saved ${when(liveness.asOf)}`);
  }
}

/** What is on screen while the machine holding the folder is not answering. */
const SHOWING_LAST_SYNC = "showing last sync";

/** What a folder whose machine stopped answering reads: since when, and what
 *  is on screen instead. */
function offlineLine(asOf: string | null, when: (iso: string) => string): string {
  return asOf === null
    ? `Offline · ${SHOWING_LAST_SYNC}`
    : `Offline since ${when(asOf)} · ${SHOWING_LAST_SYNC}`;
}

/** The whole of the leading sentence as words, for the surfaces that render it
 *  as text and for an accessible name. */
export function livenessLabel(liveness: FolderLiveness, opts: LiveCopyOptions = {}): string {
  const line = livenessLine(liveness, opts);
  return line.subject === null ? line.lead : `${line.lead}${line.subject.text}`;
}

function filesCount(n: number): string {
  return `${n} ${n === 1 ? "file" : "files"}`;
}

/** Files the machine wrote whose bytes are still on their way to the drive. */
export function landingCopy(n: number): string {
  return `${filesCount(n)} syncing`;
}

/** Files the machine holds that the drive has not been sent yet. */
export function onMachineCopy(n: number): string {
  return `${filesCount(n)} on the machine`;
}

/** The counts that ride under the label while a folder is live, when there is
 *  anything to count. Nothing in flight and nothing held back is the normal
 *  case, and it says nothing rather than "0". */
export function livenessNotes(liveness: FolderLiveness): string[] {
  if (liveness.state !== "live") return [];
  const notes: string[] = [];
  if (liveness.landing > 0) notes.push(`Live · ${landingCopy(liveness.landing)}`);
  if (liveness.onBox > 0) notes.push(`Live · ${onMachineCopy(liveness.onBox)}`);
  return notes;
}

/** The state a status bar reports. Finer than the liveness itself: a machine
 *  whose files are still landing and one whose files are all on the drive are
 *  one lease and two different things to watch, a machine that stopped
 *  answering is not a folder at rest, and a dead event stream is its own state
 *  rather than a saved copy with an asterisk. */
export type LiveStatusState =
  | "live"
  | "landing"
  | "handing-back"
  | "persisted"
  | "offline"
  | "stream-down";

/** What a status bar puts in each of its slots. Slots, not a sentence: the bar
 *  is one row under a listing a reader can drag narrow, so each part is
 *  rendered in a fixed place and dropped when it has nothing to say, rather
 *  than joined into prose that reflows into three lines. */
export interface LiveStatus {
  state: LiveStatusState;
  /** The state in one or two words. Never a sentence, never punctuated. */
  label: string;
  /** The machine doing the work, by name. Empty when no machine holds it. */
  machine: string;
  /** When the copy on screen was written. Empty unless it is settled and the
   *  drive knows when. */
  when: string;
  /** The counts worth saying, in a fixed order. A count of zero is not news. */
  chips: string[];
}

export interface LiveStatusOptions extends Pick<LiveCopyOptions, "streamDown" | "formatTime"> {
  /** The browser says it has no network. Said as such, calmly, rather than as
   *  a stream that went down: the reader knows what offline means and that it
   *  comes back on its own. */
  offline?: boolean;
  /** How long ago the copy on screen was written, already spelled ("3 min
   *  ago"). Passed in rather than computed so this stays pure and locale-free. */
  savedAgo?: string | null;
}

/**
 * The whole of a status bar's content, from the same verdict the sentences
 * above are written from.
 *
 * Nothing here names the holder. A folder held by the box running the chat is
 * held by the reader's own conversation, so saying who is working is saying
 * what they already know; what they cannot see is WHICH machine and HOW MUCH
 * is still moving, which is what the slots carry.
 */
export function liveStatus(liveness: FolderLiveness, opts: LiveStatusOptions = {}): LiveStatus {
  const machine = liveness.state === "persisted" ? (liveness.machine ?? "") : liveness.machine;
  const ago = opts.savedAgo ?? null;
  const when = liveness.state === "persisted" && ago !== null ? `Saved ${ago}` : "";
  // With no stream the browser cannot learn that anything changed, so every
  // count it holds is a number from some earlier moment. It says the one thing
  // it still knows is true.
  if (opts.offline === true) {
    return { state: "stream-down", label: "Offline", machine, when: "Reconnecting…", chips: [] };
  }
  if (opts.streamDown === true) {
    return { state: "stream-down", label: "Stream down", machine, when, chips: [] };
  }
  if (liveness.state === "handing-back") {
    return { state: "handing-back", label: "Handing back", machine, when: "", chips: [] };
  }
  if (liveness.state === "live") {
    // "Live" either way: the machine is the writer whether or not its bytes
    // have caught up, and the count says how far behind they are.
    const chips: string[] = [];
    if (liveness.landing > 0) chips.push(landingCopy(liveness.landing));
    if (liveness.onBox > 0) chips.push(onMachineCopy(liveness.onBox));
    return {
      state: liveness.landing > 0 ? "landing" : "live",
      label: "Live",
      machine,
      when: "",
      chips,
    };
  }
  if (liveness.reason === "offline") {
    const at = liveness.asOf === null ? null : (opts.formatTime ?? defaultTime)(liveness.asOf);
    const since =
      at === null ? capitalize(SHOWING_LAST_SYNC) : `Since ${at} · ${SHOWING_LAST_SYNC}`;
    return { state: "offline", label: "Offline", machine, when: since, chips: [] };
  }
  return { state: "persisted", label: "Saved copy", machine, when, chips: [] };
}

/** The chip on ONE row, from the live state the server put on that node.
 *  A change arriving from the workspace (a save, a move, a removal on its way
 *  to the machine) says nothing: it settles on its own within moments, and a
 *  chip for it read as something wrong. An unknown state shows nothing too: a
 *  build that predates a state must not render its raw spelling at a person. */
export function liveRowChip(state: string, size?: string | null): string | null {
  switch (state) {
    case "writing":
      return "writing…";
    case "uploading":
      return "uploading…";
    case "on_box":
    case "deferred":
      // Written by the machine and deliberately not sent yet — too big, or past
      // the bandwidth window. Saying how big is what makes that legible.
      return size ? `on the machine (${size})` : "on the machine";
    default:
      return null;
  }
}

/** The chip on ONE row, from where its bytes stand against the machine holding
 *  its folder, naming that machine.
 *
 *  Where the bytes stand is never a row's word while the machine still sends
 *  them: awake, the chat's folder is served from the machine, so a row the drive
 *  has not caught up with opens as the machine has it; asleep, the release has
 *  made the drive current. A row that is `behind` or `unlanded` therefore says
 *  nothing — the folder's own line says when what is listed is the last synced
 *  copy — and only bytes the machine will NOT send are named, because those the
 *  reader has to go and get. A row whose bytes are on the drive, and a state
 *  this build predates, say nothing either. */
export function liveContentChip(content: string | undefined, machine: string): string | null {
  switch (content) {
    case "unsynced":
      return `left on ${machine}, not saved`;
    default:
      return null;
  }
}

/** What a preview says while the drive is bringing a file's bytes from the
 *  machine holding it, from what the machine answered. Every answer but the
 *  two that change what the reader should expect reads as the fetch it is: a
 *  file the machine was still writing, or that it had not got to yet, is on its
 *  way all the same. */
export function fetchingLine(outcome: string | null, machine: string): string {
  switch (outcome) {
    case "offline":
      return `${capitalize(machine)} is offline · showing nothing yet`;
    case "busy":
    case "throttled":
      return `${capitalize(machine)} is busy · trying again`;
    default:
      return `Fetching from ${machine}…`;
  }
}

/** The line over a preview the drive served from its own older copy, because
 *  the machine holding a newer one did not bring it in time. */
export function staleCopyLine(
  asOf: string | null,
  machine: string,
  formatTime: (iso: string) => string = defaultTime,
): string {
  const copy = asOf === null ? "an older copy" : `the copy from ${formatTime(asOf)}`;
  return `Showing ${copy}; ${machine} has a newer one`;
}
