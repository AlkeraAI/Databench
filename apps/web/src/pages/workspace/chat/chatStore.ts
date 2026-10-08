// Single source of truth for an open chat's transcript + send lifecycle.
//
// The one optimistic-send pattern lives here: `send` pushes the user's message
// and flips `sending` SYNCHRONOUSLY (no awaited RPC first), so the bubble and the
// working indicator appear the instant the user hits Send. The daemon's echoed
// events fold in via the data source and replace the optimistic turn once the
// real one lands (reconciliation below). ChatSurface renders straight from here
// and keeps no turns/busy state of its own.

import { create } from "zustand";
import type { ConversationTurn } from "@alkera/chat-model";
import { awaitsTurnStart, conversationAwaitsResponse } from "./data/harnessEventFold";
import { chatData, chatHost, type ChatDataSource, type SendOptions, isSessionNotOpen } from "./data";
import {
  clearInFlight,
  holdInFlight,
  readInFlight,
  readQueued,
  releaseInFlight,
  writeQueued,
  type QueuedMessage,
} from "./queuedMessages";
import { queryRetryDelay, retryAfterMs } from "@/api/retry";
import { chatAccountScope, type AccountScope } from "@/lib/accountScope";
import { RETRY_BACKOFF_FLOOR_MS, RETRY_JITTER_RATIO, SEND_THROTTLE_CAP_MS } from "@/lib/limits";

export type { QueuedMessage } from "./queuedMessages";

/** Who the queue is written down for: the person and org the shell names, read
 *  at the moment of the write so a queue is never filed under an account the
 *  shell has since moved off. A store driven with no host installed (a fixture
 *  that installs only a source) names nobody, and its queue lives in memory. */
function queueScope(): AccountScope | null {
  try {
    return chatAccountScope(chatHost().account());
  } catch {
    return null;
  }
}

let seq = 0;
let staleSeq = 0;
let queuedSeq = 0;

/** A fresh id the server dedupes one message by. */
const mintClientId = (): string => globalThis.crypto.randomUUID();

/** A prompt the daemon refused with SESSION_NOT_OPEN, held for one-click resend. */
type StaleSend = { text: string; opts?: SendOptions };

/** What asked for a send — which is what decides where its words go back to if
 *  the server refuses it. `doSend` serves all three, and only the first of them
 *  is words the reader is looking at. */
export type SendIntent = "composer" | "queued" | "resend";

interface SendContext {
  intent: SendIntent;
  /** The refusal record a stale resend is re-delivering, carried through so a
   *  retry that meets SESSION_NOT_OPEN re-offers the ORIGINAL rather than
   *  minting a fresh one that has lost its place in the recency order. */
  refusal?: StaleSend;
  /** The queued row this release took out of the queue and where it was
   *  standing, so a refusal can put that same row back in its own place. The
   *  two travel together because a row returned anywhere else is a message the
   *  reader typed first that the agent now reads second. */
  queued?: { message: QueuedMessage; at: number };
  /** Which attempt this is: 0 is the reader's, then each automatic retry. */
  attempt?: number;
  /** The id the server dedupes this message by, minted on the first attempt
   *  and carried through every retry so a retried send is recorded once. */
  clientId?: string;
}

const COMPOSER_SEND: SendContext = { intent: "composer" };

/** Creation order per refusal record, keyed by object identity so the record's
 *  consumer-visible shape stays exactly `{text, opts}`. Two concurrent reopen
 *  windows use it to agree on which refusal the one prompt slot keeps. */
const staleOrder = new WeakMap<object, number>();

function mintStale(text: string, opts?: SendOptions): StaleSend {
  const record = { text, opts };
  staleOrder.set(record, ++staleSeq);
  return record;
}

/** The newer of two refusal records; either side may be null. */
function newerStale(a: StaleSend | null, b: StaleSend | null): StaleSend | null {
  if (!a) return b;
  if (!b) return a;
  return (staleOrder.get(a) ?? 0) >= (staleOrder.get(b) ?? 0) ? a : b;
}

/** The user's just-sent message, shown before the daemon echoes it back. Each
 *  one carries a fresh id: FIFO reconciliation and the resend path both track
 *  placeholders by id, so two sends of the same text stay distinguishable. */
export function optimisticUserTurn(text: string): ConversationTurn {
  const id = `optimistic-${seq++}`;
  return { id, author: "user", status: "done", parts: [{ id: `${id}-t`, kind: "text", text }] };
}

/** Whether the reader's newest message has not been taken up by any machine:
 *  the record's own word (`awaitsTurnStart`), or a bubble this tab put up that
 *  the record has not even echoed yet. */
export function awaitingTurnStart(turns: ConversationTurn[]): boolean {
  const last = turns[turns.length - 1];
  if (last?.author === "user" && last.id.startsWith("optimistic-")) return true;
  return awaitsTurnStart(turns);
}

function countUserTurns(turns: ConversationTurn[]): number {
  return turns.reduce((n, turn) => (turn.author === "user" ? n + 1 : n), 0);
}

interface ChatEntry {
  /** Folded transcript from the daemon (history + streamed events). */
  base: ConversationTurn[];
  /** User turns shown immediately on Send, dropped FIFO as `base` echoes them. */
  optimistic: ConversationTurn[];
  /** User-turn count in `base` at the last reconcile — drives the FIFO drop. */
  baseUserCount: number;
  /** A turn we initiated is awaiting a response — drives the working indicator. */
  sending: boolean;
  /** History is being fetched (the daemon opens the chat ~2s). */
  loading: boolean;
  error: string | null;
  /** The send the daemon refused because it no longer holds this chat's
   *  session (SESSION_NOT_OPEN). Kept so the user's reopen action can deliver
   *  the same message; null once delivered, dismissed, or superseded. The one
   *  slot holds the NEWEST refusal; recency is store-internal bookkeeping
   *  (`staleOrder`), not part of this record's shape. */
  staleSend: StaleSend | null;
  /** A send the server refused outright, in the reader's words - null when
   *  nothing was refused. A refusal is not a stale session and cannot be
   *  retried into one: the rung this reader holds on the chat does not carry a
   *  message, so the optimistic bubble is withdrawn and this says why. */
  sendRefusal: string | null;
  /** Words handed back to the composer after a send the server did not record,
   *  under the stamp the composer adopts a draft by. A refused send that only
   *  withdrew its bubble would leave the reader with an empty field and no copy
   *  of what they had typed. */
  returnedDraft: { text: string; at: number } | null;
  /** The reader pressed Stop on a turn the source could not actually abort, so
   *  the composer was handed back deliberately. Holds until that turn ends (or
   *  a new send takes it): without it the next streamed token re-derives
   *  `sending` and takes the composer away again mid-sentence. */
  released: boolean;
  /** Messages typed while the turn was running, oldest first. Each goes out
   *  through the normal send path when the turn ends — one per turn, so the
   *  order the reader typed them in is the order the agent sees them. */
  queued: QueuedMessage[];
  /** The turn a message this tab SENT is waiting behind, or null when nothing
   *  is waiting.
   *
   *  A chat opened while a turn is already running does not read as busy — the
   *  transcript's word on a turn this tab did not start is not enough to take
   *  the composer away — so a message typed there goes out. The server takes
   *  it and the box parks it until the running turn ends, and without this the
   *  field simply cleared and nothing happened for as long as that took. */
  queuedBehind: string | null;
}

/** A chat's composer choices. The daemon owns the durable copy for an existing
 *  chat (mode in the manifest, model/effort on the pin); this webview-lifetime
 *  copy keeps the pill and pickers steady across navigation, and it is the ONLY
 *  home a not-yet-created chat's choices have. */
export interface ComposerPrefs {
  mode?: string;
  model?: string;
  effort?: string;
}

/** The prefs key for a chat that does not exist yet. */
export const DRAFT_CHAT_KEY = "draft";

/** The chats this webview has a turn in flight for. The chat lists read it to
 *  light a row before the first daemon event lands. */
export function sendingChatIds(byId: Record<string, { sending: boolean }>): ReadonlySet<string> {
  const ids = new Set<string>();
  for (const [chatId, entry] of Object.entries(byId)) {
    if (entry.sending) ids.add(chatId);
  }
  return ids;
}

interface ChatStore {
  byId: Record<string, ChatEntry>;
  /** Turns a chat must already be showing the moment it is opened, keyed by the
   *  chat they belong to.
   *
   *  A chat started from a composer is created on one route and read on
   *  another: the reader presses Send on the chats screen, watches their
   *  message go up, and is then moved onto `/chat/<id>`, which mounts a fresh
   *  surface whose transcript is whatever the server has published so far —
   *  nothing, until the box that runs the chat wakes up and publishes the
   *  message back. Handing the SAME turn over here carries it
   *  across: `open` adopts it as the chat's optimistic turn, and the usual FIFO
   *  reconcile retires it when the echo lands, so it is neither dropped nor
   *  drawn twice.
   *
   *  The slot is spent by the ECHO, not by the open. React runs an effect's
   *  subscribe–unsubscribe–subscribe in development, and a slot spent by the
   *  first open left the second — the one that lasts — opening onto an empty
   *  transcript with no working line, which is the blank the reader saw after
   *  the hop. Every open until the echo adopts the same turn; once the box has
   *  published it back, a re-open gets the folded transcript and nothing else. */
  pending: Record<string, ConversationTurn[]>;
  /** Hand a just-opened chat the turns it should mount holding. */
  holdPendingTurns: (chatId: string, turns: ConversationTurn[]) => void;
  /** Composer choices per chat id (plus the draft key), surviving unmounts. */
  composerPrefs: Record<string, ComposerPrefs>;
  setComposerPref: (chatKey: string, patch: ComposerPrefs) => void;
  /** A created chat inherits the draft's choices; the draft resets. */
  claimDraftPrefs: (chatId: string) => void;
  /** Load history + stream daemon updates into the store. Returns an unsubscribe. */
  open: (chatId: string) => () => void;
  /** Read the transcript again after a read that failed. */
  reload: (chatId: string) => void;
  /** THE optimistic send: push the user turn + `sending` now, then fire the RPC. */
  send: (chatId: string, text: string, opts?: SendOptions) => void;
  /** Hold a message the reader typed mid-turn. It goes out through `send` the
   *  moment the turn ends — never from here, so the one send path stays one. */
  queueMessage: (chatId: string, text: string, opts?: SendOptions) => void;
  /** Rewrite a held message before it goes. */
  editQueued: (chatId: string, id: string, text: string) => void;
  /** Take a held message back. Nothing is sent for it. */
  removeQueued: (chatId: string, id: string) => void;
  /** The reader's word on a message that will not send itself — one restored
   *  from a previous page session, or one their Stop cancelled: send it. It
   *  goes now if the chat owes nothing; if a turn is running it joins the
   *  queue that goes on that turn's end. Either way it stops waiting for a
   *  click. */
  sendQueued: (chatId: string, id: string) => void;
  cancel: (chatId: string) => void;
  /** The user's answer to the stale-session prompt: re-open the chat from
   *  disk, then deliver the message the daemon refused. */
  reopenAndResend: (chatId: string) => void;
  /** Dismiss the stale-session prompt without re-opening. */
  dismissStaleSend: (chatId: string) => void;
}

const EMPTY: ChatEntry = {
  base: [],
  optimistic: [],
  baseUserCount: 0,
  sending: false,
  loading: false,
  error: null,
  staleSend: null,
  sendRefusal: null,
  returnedDraft: null,
  released: false,
  queued: [],
  queuedBehind: null,
};

/** The last word each chat's machine gave about its turn, so a gap in the
 *  news is not mistaken for news. */
const lastTurnWord = new Map<string, "working" | "idle">();

/** Whether the machine says it is still writing this turn.
 *
 *  The folded transcript cannot answer that on its own. A compaction summary
 *  lands as the tail of the live turn and reads as a settled exchange, and an
 *  auto compaction then re-sends the very same prompt — so for the length of
 *  the compaction, and again in the beat between the summary and the re-send,
 *  the transcript owes nothing while the box is very much working. The box
 *  says so on its own lane and re-stamps it on its heartbeat, and that word is
 *  what keeps the composer from handing itself back in the middle of a turn.
 *  A source that publishes no turn state (the editor's daemon) leaves the
 *  transcript as the only authority, exactly as before.
 *
 *  A read that answers NOTHING is not the box saying the turn ended — it is
 *  nobody having said anything. The chat's sockets drop and reopen (a pong
 *  timeout, a deploy, a laptop changing networks) and for a beat the source has
 *  no live word: the document was re-helloed and the snapshot that re-states
 *  `turn_state` has not landed. Taking that silence for `idle` handed the
 *  composer back while a tool was still running on the box, and the reader's
 *  next prompt went into the turn that was still going. So the last word the
 *  machine gave stands through the gap, and only the machine replaces it. */
function machineWorking(chatId: string): boolean {
  const said = chatData().turnState?.(chatId) ?? null;
  if (said !== null) {
    lastTurnWord.set(chatId, said);
    return said === "working";
  }
  return lastTurnWord.get(chatId) === "working";
}

/** A source that can say the chat's turn was HALTED, and which halt it was.
 *
 *  Asked structurally rather than through `ChatDataSource`, for the same reason
 *  the shared draft is optional there: only a source whose chat has more than
 *  one reader has anything to report. The editor's daemon serves one person at
 *  one keyboard, and the hand that pressed Stop is the hand this store is
 *  already hearing from. */
interface StopReporting {
  stoppedTurn?(chatId: string): string | null;
}

/** The halt this chat's machine last reported, by the id of the event that
 *  carried it, or null when it has reported none. */
function stopReported(chatId: string): string | null {
  return (chatData() as ChatDataSource & StopReporting).stoppedTurn?.(chatId) ?? null;
}

/** The halt each open chat was last known to stand on. A chat with NO entry is
 *  one nobody has looked at yet: the first look records where the record
 *  stands, because a Stop pressed before the reader arrived is the chat's
 *  history, not something that just happened to them. */
const lastStopSeen = new Map<string, string | null>();

/** The turn a message sent RIGHT NOW would have to wait behind, or null when
 *  it would start straight away.
 *
 *  Only ever answered for a source that really holds a mid-turn send behind
 *  the running turn: the editor's daemon supersedes one instead, and telling
 *  its reader their message was queued would be a straight lie.
 *
 *  Read off the transcript alone, never off the machine's `working` word: a
 *  box that says it is working over a settled transcript (a compaction) has no
 *  turn on screen to anchor the wait to, and a claim that cannot say what it
 *  is waiting for cannot say when the wait is over either. */
function turnAMessageWouldWaitBehind(entry: ChatEntry): string | null {
  if (chatData().holdsSendsBehindTurn !== true) return null;
  const turns = [...entry.base, ...entry.optimistic];
  if (!conversationAwaitsResponse(turns)) return null;
  // A message nothing has started yet is not a running turn. The new message
  // waits for the workspace exactly as that one does, and the working line
  // already says so.
  if (awaitingTurnStart(turns)) return null;
  return turns[turns.length - 1]?.id ?? null;
}

/** Whether `turnId` is STILL the turn a waiting message is behind.
 *
 *  Read off the transcript up to and including that turn, never off its tail:
 *  the server writes the waiting message into the transcript the moment it
 *  takes it, so the tape always ends on an unanswered prompt and its tail says
 *  nothing about whether the turn in front has finished. A turn that is gone
 *  from the transcript entirely is not one anything can still be waiting on. */
function stillTheRunningTurn(turns: ConversationTurn[], turnId: string): boolean {
  const at = turns.findIndex((turn) => turn.id === turnId);
  if (at < 0) return false;
  return conversationAwaitsResponse(turns.slice(0, at + 1));
}

/** Whether the server refused the send itself.
 *
 *  A 403 is the one failure the reader must be told about by name: the message
 *  was never accepted, so a bubble sitting in the transcript is a lie about
 *  what the colleague on the other side can see. Every other failure is a turn
 *  that may yet land, and those keep their optimistic bubble. */
export function isSendForbidden(err: unknown): boolean {
  return (err as { status?: unknown } | null)?.status === 403;
}

/** What a reader is told when the send is refused.
 *
 *  Deliberately NOT the transport's own sentence. A refused send carries the
 *  policy's message when there is one and "the request to /api/v1/chats/<id>
 *  /messages failed (403)" when there is not, and neither tells a person what
 *  happened or what to do about it. The rung is the reason, so the rung is what
 *  it says. */
export const SEND_FORBIDDEN =
  "You can read this chat, but you're not allowed to send messages in it. Ask the owner for edit access.";

/** Whether the server refused the send because the chat's workspace stopped
 *  answering (`409 machine_unreachable`). The message was not recorded: the
 *  bubble goes, the server's own sentence is what the reader is told, and the
 *  words go back to the field for a send once the workspace is back. */
export function isSendMachineUnreachable(err: unknown): boolean {
  const refused = err as { status?: unknown; code?: unknown } | null;
  return refused?.status === 409 && refused.code === "machine_unreachable";
}

/** Whether the server refused the send because this reader is sending too fast.
 *
 *  A 429 is not a 403: the limiter is asking for a pause, not saying no. The
 *  message was never recorded, so the bubble is a lie the same way a refused
 *  one is — but the words are still deliverable, so they come back to the field
 *  and the send is re-offered once on the wait the server itself named. */
export function isSendThrottled(err: unknown): boolean {
  return (err as { status?: unknown } | null)?.status === 429;
}

/** Whether the send failed for a reason that passes: the server answered 5xx
 *  (a database stall answers 503 with a `Retry-After`), or the request never
 *  got an answer at all. Nothing about the message was refused, so it is
 *  retried on its own, and the words are never dropped. */
export function isSendUnavailable(err: unknown): boolean {
  const status = (err as { status?: unknown } | null)?.status;
  if (typeof status === "number") return status >= 500;
  return err instanceof TypeError;
}

/** How many times a send the server could not take is retried on its own
 *  before the reader is asked. A throttled send is retried once: the limiter
 *  asked for a pause, and more retries would spend the budget it is out of. */
export const SEND_UNAVAILABLE_RETRIES = 3;

/** The wait before automatic retry `attempt` (zero-based) of a send the server
 *  could not take: its own `Retry-After` when it named one, else a climbing
 *  backoff, jittered either way. */
export function sendUnavailableWaitMs(
  err: unknown,
  attempt: number,
  random: () => number = Math.random,
): number {
  return Math.min(queryRetryDelay(attempt, err, random), SEND_THROTTLE_CAP_MS);
}

/** The one line a send the server could not take leaves behind while another
 *  attempt is armed, or null once it is not: the held row then says "Not sent"
 *  and carries its own Retry, and a second line would only repeat it. */
export function sendUnavailableNotice(retryingIn: number | null): string | null {
  if (retryingIn === null) return null;
  return `Couldn't send. Retrying in ${retryingIn} ${retryingIn === 1 ? "second" : "seconds"}.`;
}

/** The queue rows a page that went away left behind: what it had queued, and
 *  every send it still had on the wire, as a row that was not sent. A send
 *  already held as a row is not added twice. */
export function restoredQueue(chatId: string): QueuedMessage[] {
  const scope = queueScope();
  const queued = readQueued(scope, chatId);
  const known = new Set(queued.map((held) => held.clientId).filter(Boolean));
  const lost = readInFlight(scope, chatId)
    .filter((send) => !known.has(send.clientId))
    .map(
      (send): QueuedMessage => ({
        id: `unsent-${send.clientId}`,
        text: send.text,
        opts: send.opts,
        clientId: send.clientId,
        unsent: true,
        restored: true,
      }),
    );
  return [...queued, ...lost];
}

/** How long a throttled send waits before it is offered again: the server's own
 *  `Retry-After` when it named one, and the ordinary backoff floor when it did
 *  not. Floored as well as capped — a header of `0` from a proxy that spells it
 *  badly would otherwise re-send the very burst that was just refused.
 *
 *  Jittered on top, for the reason the reads are: the scenario this exists for
 *  is several tabs of ONE reader, all refused by the same limiter in the same
 *  second and all handed the same `Retry-After`. Without the spread the one
 *  automatic retry is most likely to be refused again in precisely the case it
 *  was written for. Added and never subtracted — the server's wait is a floor.
 *  `random` is injected so a test can pin both ends of the spread. */
export function sendThrottleWaitMs(err: unknown, random: () => number = Math.random): number {
  const asked = retryAfterMs(err);
  const base = Math.min(
    Math.max(asked ?? RETRY_BACKOFF_FLOOR_MS, RETRY_BACKOFF_FLOOR_MS),
    SEND_THROTTLE_CAP_MS,
  );
  return Math.round(base * (1 + RETRY_JITTER_RATIO * random()));
}

/** The one line a throttled send leaves behind. `retryingIn` is the wait in
 *  whole seconds while another attempt is armed, and null once the send is the
 *  reader's to make again. Spelled the way the failed-load plate spells the
 *  same wait, so a reader meets one phrasing for it across the product. */
export function sendThrottledNotice(retryingIn: number | null): string {
  if (retryingIn === null) return "Sending too quickly. Try again in a moment.";
  return `Sending too quickly. Retrying in ${retryingIn} ${retryingIn === 1 ? "second" : "seconds"}.`;
}

/** What a reader is told when the transcript itself cannot be read. A chat that
 *  is not there is answered by the page's own dead end, which replaces this
 *  surface, so what is left for this line is the chat still on screen with
 *  messages that did not arrive. */
export const TRANSCRIPT_UNREADABLE = "This chat's messages could not be loaded.";

export const useChatStore = create<ChatStore>((set, get) => {
  const composerActions = {
    composerPrefs: {} as Record<string, ComposerPrefs>,
    setComposerPref: (chatKey: string, patch: ComposerPrefs): void =>
      set((state) => ({
        composerPrefs: {
          ...state.composerPrefs,
          [chatKey]: { ...state.composerPrefs[chatKey], ...patch },
        },
      })),
    claimDraftPrefs: (chatId: string): void =>
      set((state) => {
        const draft = state.composerPrefs[DRAFT_CHAT_KEY];
        if (!draft) return state;
        const rest = { ...state.composerPrefs };
        delete rest[DRAFT_CHAT_KEY];
        return {
          composerPrefs: { ...rest, [chatId]: { ...draft, ...state.composerPrefs[chatId] } },
        };
      }),
  };

  // Merge a freshly-folded `base` with the entry's pending optimistic turns.
  // The daemon echoes each sent message as a real user turn, so the number of
  // NEW user turns in `base` is how many optimistic placeholders to retire —
  // dropped FIFO by COUNT (not by text), so repeating a message or sending two
  // identical ones in a row can't drop the wrong (or a not-yet-echoed) bubble.
  // `live` = this update came from a STREAMED daemon event, not the initial
  // history load. A non-live reconcile may KEEP-or-CLEAR the indicator but
  // never resurrect it: on a cold reopen (extension reload) `prev.sending` is
  // false, so a chat whose last turn never finished stays idle — but
  // navigating away from a STREAMING chat and back (prev.sending true, the
  // singleton store remembers) re-derives from the transcript instead of
  // wiping a turn that is still in flight.
  // Refresh fetches are SEQUENCED per chat: several can be in flight at once
  // (a send's failure refresh racing a reopen's replay refresh), and applying
  // a stale fold after a fresh one snaps `baseUserCount` backwards, and the next
  // echo then retires no placeholder and the resent bubble strands duplicated.
  // A fetch that resolves after a newer one has applied is dropped.
  const fetchSeq = new Map<string, number>();
  const appliedSeq = new Map<string, number>();
  const refresh = (chatId: string, live: boolean, opts: { history?: boolean } = {}): Promise<void> => {
    const seq = (fetchSeq.get(chatId) ?? 0) + 1;
    fetchSeq.set(chatId, seq);
    return chatData().getChatTurns(chatId).then((base) => {
      if ((appliedSeq.get(chatId) ?? 0) > seq) return;
      appliedSeq.set(chatId, seq);
      reconcile(chatId, base, live, opts);
    });
  };

  // A refresh nobody is waiting on — the open, the subscription's frames, the
  // one after a send settles. A transcript the server refuses is an ANSWER, the
  // commonest being a chat that is not there, which the page already draws its
  // dead end for; but the rejection had no reader here, so it travelled out and
  // reached `window.onerror` as an uncaught `ApiError`, and any crash reporter
  // watching that event logged an expected 404 as a client error. It is
  // recorded on the chat instead, and the read stops claiming to be in flight.
  // The resend path keeps the rejection — it has a reprompt to run on it.
  const refreshDetached = (chatId: string, live: boolean, opts: { history?: boolean } = {}): void => {
    void refresh(chatId, live, opts).catch(() => {
      set((state) => {
        const prev = state.byId[chatId] ?? EMPTY;
        return {
          byId: {
            ...state.byId,
            [chatId]: { ...prev, loading: false, error: TRANSCRIPT_UNREADABLE },
          },
        };
      });
    });
  };

  // How many sends this chat has issued. Only the newest one's answer may say
  // what the composer is waiting on — an older answer arriving late is about a
  // message that is no longer the reader's last word.
  const sendOrder = new Map<string, number>();

  // THE optimistic send behind the public `send`. `refusal` is set only on the
  // resend path. A resend refused AGAIN re-offers its ORIGINAL record, whose
  // recency predates any prompt raised during the reopen window, so the one
  // slot keeps the newest refusal instead of letting the retry evict it. A
  // fresh send mints a new record, which is the newest by construction.
  // The automatic retry each chat has waiting, if any. Held per chat rather
  // than per send: a chat carries one waiting send at a time, and arming a
  // second discards the first rather than letting two run at once.
  //
  // THE RULE for cancelling it: only the reader's own word on this composer
  // takes it back — their press on Send, which is what handing their words
  // back invites, and their press on Stop. A send of OTHER words — the queue
  // releasing a row, a stale resend — leaves it armed, because the refused
  // words are still in the field with nothing else holding them and the reader
  // was told they were going.
  const retryTimers = new Map<string, ReturnType<typeof setTimeout>>();

  const disarmRetry = (chatId: string): void => {
    const armed = retryTimers.get(chatId);
    if (armed === undefined) return;
    clearTimeout(armed);
    retryTimers.delete(chatId);
  };

  const armRetry = (chatId: string, timer: ReturnType<typeof setTimeout>): void => {
    disarmRetry(chatId);
    retryTimers.set(chatId, timer);
  };

  /** Where a refused send's words go back to, which is decided by what asked
   *  for the send — not by the fact that it was refused.
   *
   *  Only the composer's own send may touch the composer. The queue release and
   *  the stale resend carry text the reader is not looking at and may not even
   *  remember typing; putting it in `returnedDraft` paints it over whatever
   *  they are typing right now, and the composer adopts it by stamp, so there
   *  is no undo. The queue takes its row back instead — marked so it goes only
   *  when the reader says so, exactly as a restored one does — and the stale
   *  resend keeps its refusal record, which is what the reopen prompt delivers. */
  const refusedWords = (
    prev: ChatEntry,
    text: string,
    opts: SendOptions | undefined,
    ctx: SendContext,
  ): Partial<ChatEntry> => {
    if (ctx.intent === "composer") {
      return {
        // Forced forward so two refusals in the same millisecond each reach the
        // composer, which adopts a draft by its stamp.
        returnedDraft: { text, at: Math.max(Date.now(), (prev.returnedDraft?.at ?? 0) + 1) },
      };
    }
    if (ctx.intent === "queued") {
      const held = ctx.queued ?? { message: { id: `queued-${++queuedSeq}`, text, opts }, at: 0 };
      const back = { ...held.message, restored: true, stopped: false };
      // The row was taken out of the queue before the send went; put that same
      // row back, under its own id and in the place it was standing in. The
      // end of the queue is the wrong place twice over: the reader typed it
      // before everything now above it, and a held row only blocks the ones
      // BEHIND it — so a row that waits for a click while standing last lets
      // every later message past it and the agent reads them out of order.
      // The row is never already there: a release takes it out before it
      // sends, and nothing but the reader puts one back.
      const at = Math.min(held.at, prev.queued.length);
      const queued = [...prev.queued.slice(0, at), back, ...prev.queued.slice(at)];
      return { queued };
    }
    return { staleSend: newerStale(prev.staleSend, ctx.refusal ?? mintStale(text, opts)) };
  };

  // A send the limiter refused, withdrawn and re-offered.
  //
  // THE RULE, and the only one this function has: a refused send is held in ONE
  // place — the composer's own send keeps it as an armed retry and writes no
  // queued row, and every other caller hands its words back to the place its
  // reader acts on them (the queue's own row, the reopen prompt's record) and
  // arms nothing.
  //
  // Both at once is a message in two places. A row marked `restored` means
  // "held until the reader says so", and a timer that sends it anyway puts the
  // same words in the transcript AND in the queue, under a live Send key, with
  // a copy in this browser that outlives a reload: one click bills it twice.
  //
  // The message was never recorded, so the bubble goes and the words come back
  // to the field. One automatic retry — on the server's own `Retry-After` when
  // it named one — covers the ordinary case (several tabs of one reader hitting
  // Send inside the same second, which is what spends the budget). A SECOND
  // refusal is not re-armed: at that point the limiter is not asking for a beat
  // but for a rest, and a client that keeps re-sending on its own is spending
  // the very budget it was told it is out of. The line says which of the two it
  // is, and Send is live either way.
  const sendThrottled = (
    chatId: string,
    text: string,
    opts: SendOptions | undefined,
    bubbleId: string,
    err: unknown,
    ctx: SendContext,
  ): void => {
    // Only the composer's own send is re-offered by a timer. Its words went
    // back to a field and nothing else holds them; the other two callers hand
    // theirs to a row or a prompt that waits for the reader, and the reader's
    // click on that IS the retry.
    const attempt = ctx.attempt ?? 0;
    const again = ctx.intent === "composer" && attempt < 1;
    const waitMs = sendThrottleWaitMs(err);
    const notice = sendThrottledNotice;
    set((state) => {
      const prev = state.byId[chatId] ?? EMPTY;
      const entry: ChatEntry = {
        ...prev,
        optimistic: prev.optimistic.filter((turn) => turn.id !== bubbleId),
        sending: false,
        sendRefusal: notice(again ? Math.ceil(waitMs / 1_000) : null),
        ...refusedWords(prev, text, opts, ctx),
      };
      return { byId: { ...state.byId, [chatId]: entry } };
    });
    // Only the queue writes itself down, and only once the row is back in it.
    if (ctx.intent === "queued") writeQueued(queueScope(), chatId, get().byId[chatId]?.queued ?? []);
    if (!again) return;
    // The timer clears its own entry as it fires: a superseded one was cleared
    // before it could run, so whatever is under this key when the callback
    // lands is this timer and nobody else's.
    armRetry(
      chatId,
      setTimeout(() => {
        retryTimers.delete(chatId);
        doSend(chatId, text, opts, { ...ctx, attempt: attempt + 1 });
      }, waitMs),
    );
  };

  // The automatic retry each unsent row has waiting, by row id. Kept apart from
  // the composer's throttle retry: the words of an unsent row live in the row,
  // so its timer is the row's, and the reader's press on another row or on
  // Send must not take it away.
  const outboxTimers = new Map<string, ReturnType<typeof setTimeout>>();

  const disarmOutbox = (rowId: string): void => {
    const armed = outboxTimers.get(rowId);
    if (armed === undefined) return;
    clearTimeout(armed);
    outboxTimers.delete(rowId);
  };

  // Take one row out of the queue, written down before it goes, and send it
  // under the identity it was first sent with.
  const releaseRow = (chatId: string, rowId: string, attempt: number): void => {
    disarmOutbox(rowId);
    const outgoing: { message: QueuedMessage | null; at: number } = { message: null, at: 0 };
    set((state) => {
      const prev = state.byId[chatId] ?? EMPTY;
      const at = prev.queued.findIndex((message) => message.id === rowId);
      if (at < 0) return state;
      outgoing.message = prev.queued[at] ?? null;
      outgoing.at = at;
      const queued = prev.queued.filter((message) => message.id !== rowId);
      return { byId: { ...state.byId, [chatId]: { ...prev, queued } } };
    });
    const going = outgoing.message;
    if (!going) return;
    writeQueued(queueScope(), chatId, get().byId[chatId]?.queued ?? []);
    doSend(chatId, going.text, going.opts, {
      intent: "queued",
      queued: { message: going, at: outgoing.at },
      attempt,
      ...(going.clientId ? { clientId: going.clientId } : {}),
    });
  };

  // A send the server could not take (a 5xx, or no answer at all).
  //
  // THE RULE: the words are held in ONE place, and that place is written down.
  // The bubble goes, and the message becomes a queue row marked `unsent` at the
  // place it was sent from, so a reload or a closed tab keeps it and the order
  // the reader sent things in is the order they go in. The row goes again on a
  // climbing backoff (the server's own `Retry-After` when it named one), under
  // the client id of its first attempt, so a send whose answer was lost is
  // recorded once. When the retries are spent the row stays "Not sent" with a
  // Retry of its own; nothing goes again until the reader presses it.
  const sendUnavailable = (
    chatId: string,
    text: string,
    opts: SendOptions | undefined,
    bubbleId: string,
    err: unknown,
    ctx: SendContext,
  ): void => {
    const attempt = ctx.attempt ?? 0;
    const again = attempt < SEND_UNAVAILABLE_RETRIES;
    const waitMs = sendUnavailableWaitMs(err, attempt);
    const held: QueuedMessage = ctx.queued?.message ?? {
      id: `unsent-${ctx.clientId ?? `${Date.now()}-${queuedSeq++}`}`,
      text,
      opts,
    };
    const row: QueuedMessage = {
      ...held,
      unsent: true,
      restored: false,
      stopped: false,
      ...(ctx.clientId ? { clientId: ctx.clientId } : {}),
    };
    set((state) => {
      const prev = state.byId[chatId] ?? EMPTY;
      const others = prev.queued.filter((message) => message.id !== row.id);
      const at = Math.min(ctx.queued?.at ?? 0, others.length);
      const entry: ChatEntry = {
        ...prev,
        optimistic: prev.optimistic.filter((turn) => turn.id !== bubbleId),
        sending: false,
        sendRefusal: sendUnavailableNotice(again ? Math.ceil(waitMs / 1_000) : null),
        queued: [...others.slice(0, at), row, ...others.slice(at)],
      };
      return { byId: { ...state.byId, [chatId]: entry } };
    });
    // The row is written down before the wire record is let go, so there is
    // no moment in which neither holds the words.
    writeQueued(queueScope(), chatId, get().byId[chatId]?.queued ?? []);
    if (ctx.clientId) releaseInFlight(queueScope(), chatId, ctx.clientId);
    if (!again) return;
    disarmOutbox(row.id);
    outboxTimers.set(
      row.id,
      setTimeout(() => {
        outboxTimers.delete(row.id);
        releaseRow(chatId, row.id, attempt + 1);
      }, waitMs),
    );
  };

  const doSend = (
    chatId: string,
    text: string,
    opts?: SendOptions,
    given: SendContext = COMPOSER_SEND,
  ): void => {
    // One server-side identity per message, minted on its first attempt and
    // carried through every retry.
    const ctx: SendContext = { ...given, clientId: given.clientId ?? mintClientId() };
    const clientId = ctx.clientId;
    // Synchronous optimistic update — the user sees their message + the working
    // indicator immediately, before any host/daemon round-trip.
    // Held by id so a refusal can withdraw THIS bubble, not whichever one
    // happens to be last: two sends can be in flight at once.
    const bubble = optimisticUserTurn(text);
    // What is running BEFORE this message joins the transcript. Read here
    // rather than when the answer comes back: by then the server has already
    // written the message into the tape, and the tape can no longer say what
    // was running when it arrived.
    const waitsBehind = turnAMessageWouldWaitBehind(get().byId[chatId] ?? EMPTY);
    const order = (sendOrder.get(chatId) ?? 0) + 1;
    sendOrder.set(chatId, order);
    set((state) => {
      const prev = state.byId[chatId] ?? EMPTY;
      const entry: ChatEntry = {
        ...prev,
        optimistic: [...prev.optimistic, bubble],
        sending: true,
        error: null,
        staleSend: null,
        sendRefusal: null,
        released: false,
        // This send has not been taken yet, so nothing is known to be waiting;
        // whatever the last one was waiting behind is no longer what the line
        // under the field is about.
        queuedBehind: null,
      };
      return { byId: { ...state.byId, [chatId]: entry } };
    });
    // Fire and forget. The daemon streams the echoed user message + reply back
    // through subscribeChat → reconcile. A rejection records a polished error
    // turn in the fold on the next reconcile, so this handler only clears sending.
    // A stale-session rejection additionally keeps the refused message so the
    // reopen prompt can deliver it.
    // Written down before the request goes: a page that dies with the request
    // open leaves the words for the next one to offer back. A stale resend is
    // already held by its reopen prompt.
    const durable = ctx.intent !== "resend" && clientId !== undefined;
    if (durable) holdInFlight(queueScope(), chatId, { clientId, text, ...(opts ? { opts } : {}) });
    void chatData().sendUserMessage(chatId, text, { ...opts, clientId }).then(
      () => {
        if (durable) releaseInFlight(queueScope(), chatId, clientId);
        // The words became a message, so the copy of them that a refusal handed
        // back to the field goes. Left standing, the reader saw their sentence
        // in the transcript AND still in the composer, which reads as "it did
        // not go" — and they send it again. Handed over as a stamped EMPTY
        // draft rather than dropped, because the composer adopts a draft by its
        // stamp and would otherwise keep showing the one it already took.
        // Only the draft this very send was handed back: a newer refusal's
        // words are a different sentence and are not this one's to clear.
        set((state) => {
          const prev = state.byId[chatId] ?? EMPTY;
          if (prev.returnedDraft === null || prev.returnedDraft.text !== text) return state;
          const cleared = { text: "", at: Math.max(Date.now(), prev.returnedDraft.at + 1) };
          return { byId: { ...state.byId, [chatId]: { ...prev, returnedDraft: cleared } } };
        });
        // Taken, and taken over a turn that was already running: the message is
        // recorded and will not be answered until that turn ends. Said from
        // here — the moment the server accepted it — because until then there
        // was nothing to wait, and a refused send waits for nothing.
        if (waitsBehind === null) return;
        // A newer send issued inside this round trip owns the line now, and a
        // slow answer to the older one must not put its wait back under the
        // field. Tracked by issue order rather than by the bubble, which the
        // echo can retire before the send's own answer arrives.
        if (sendOrder.get(chatId) !== order) return;
        set((state) => {
          const prev = state.byId[chatId] ?? EMPTY;
          return { byId: { ...state.byId, [chatId]: { ...prev, queuedBehind: waitsBehind } } };
        });
      },
      (err: unknown) => {
        if (isSendUnavailable(err)) {
          sendUnavailable(chatId, text, opts, bubble.id, err, ctx);
          return;
        }
        // Every other answer hands the words to the place it names (the field,
        // the queue's row, the reopen prompt) or says they were refused.
        if (durable) releaseInFlight(queueScope(), chatId, clientId);
        if (isSendThrottled(err)) {
          sendThrottled(chatId, text, opts, bubble.id, err, ctx);
          return;
        }
        if (isSendMachineUnreachable(err)) {
          set((state) => {
            const prev = state.byId[chatId] ?? EMPTY;
            const entry: ChatEntry = {
              ...prev,
              optimistic: prev.optimistic.filter((turn) => turn.id !== bubble.id),
              sending: false,
              sendRefusal: err instanceof Error && err.message ? err.message : prev.sendRefusal,
              ...refusedWords(prev, text, opts, ctx),
            };
            return { byId: { ...state.byId, [chatId]: entry } };
          });
          if (ctx.intent === "queued") writeQueued(queueScope(), chatId, get().byId[chatId]?.queued ?? []);
          return;
        }
        const forbidden = isSendForbidden(err);
        set((state) => {
          const prev = state.byId[chatId] ?? EMPTY;
          const refused = isSessionNotOpen(err) ? (ctx.refusal ?? mintStale(text, opts)) : null;
          const staleSend = refused ? newerStale(prev.staleSend, refused) : prev.staleSend;
          // A refused send never happened, so its bubble is withdrawn rather
          // than left standing as a message the other readers cannot see.
          const optimistic = forbidden
            ? prev.optimistic.filter((turn) => turn.id !== bubble.id)
            : prev.optimistic;
          const entry: ChatEntry = {
            ...prev,
            optimistic,
            sending: false,
            staleSend,
            sendRefusal: forbidden ? SEND_FORBIDDEN : prev.sendRefusal,
          };
          return { byId: { ...state.byId, [chatId]: entry } };
        });
        refreshDetached(chatId, true);
      },
    );
  };

  // The applied reopened fold has downshifted the count, so the leftover
  // placeholders clear (the refused text is about to be resent as its own
  // bubble) and the resend fires, restoring any NEWER refusal the resend's
  // own bookkeeping would otherwise clear.
  const resendAfterApply = (chatId: string, stale: StaleSend, leftovers: Set<string>): void => {
    set((state) => {
      const prev = state.byId[chatId] ?? EMPTY;
      const optimistic = prev.optimistic.filter((t) => !leftovers.has(t.id));
      return { byId: { ...state.byId, [chatId]: { ...prev, optimistic } } };
    });
    const pending = get().byId[chatId]?.staleSend;
    doSend(chatId, stale.text, stale.opts, { intent: "resend", refusal: stale });
    if (!pending) return;
    set((state) => {
      const prev = state.byId[chatId] ?? EMPTY;
      return {
        byId: {
          ...state.byId,
          [chatId]: { ...prev, staleSend: newerStale(prev.staleSend, pending) },
        },
      };
    });
  };

  // A refresh that REJECTS proves nothing about the count, so instead of a
  // blind resend the prompt comes back. When two refused messages both
  // reopened and both refreshes rejected, the one slot keeps the NEWEST
  // refusal deterministically, not whichever rejection resolved last.
  const repromptOnFailure = (chatId: string, stale: StaleSend): void => {
    set((state) => {
      const prev = state.byId[chatId] ?? EMPTY;
      return {
        byId: {
          ...state.byId,
          [chatId]: { ...prev, staleSend: newerStale(prev.staleSend, stale) },
        },
      };
    });
  };

  // Every change to what a chat is holding goes through here, so the copy the
  // browser keeps can never disagree with the copy on screen.
  const reviseQueue = (
    chatId: string,
    next: (queued: QueuedMessage[]) => QueuedMessage[],
  ): void => {
    set((state) => {
      const prev = state.byId[chatId] ?? EMPTY;
      return { byId: { ...state.byId, [chatId]: { ...prev, queued: next(prev.queued) } } };
    });
    writeQueued(queueScope(), chatId, get().byId[chatId]?.queued ?? []);
  };

  const reconcile = (
    chatId: string,
    base: ConversationTurn[],
    live: boolean,
    opts: { history?: boolean } = {},
  ): void => {
    // The message this reconcile releases from the queue, if the turn it was
    // waiting behind has just ended. Taken OUT of the entry inside the same
    // synchronous update that names it, so a second reconcile racing this one
    // finds an empty queue and the message cannot go twice.
    const outgoing: { message: QueuedMessage | null } = { message: null };
    // Whether this reconcile cancelled what the composer was holding, so the
    // browser's copy of the queue is rewritten to match.
    const halted = { queue: false };
    set((state) => {
      const prev = state.byId[chatId] ?? EMPTY;
      const baseUserCount = countUserTurns(base);
      // A page of OLDER turns joining above the transcript (or leaving it) is
      // not an echo of anything this tab sent: its user turns were said long
      // before the placeholder, so none of them retires one.
      const echoed = opts.history ? 0 : Math.max(0, baseUserCount - prev.baseUserCount);
      const optimistic = echoed > 0 ? prev.optimistic.slice(echoed) : prev.optimistic;
      const turns = [...base, ...optimistic];
      // The machine's word opens the turn as well as holding it. A reader who
      // reopens a chat mid-turn — or watches one somebody else started — is
      // looking at a turn the server says is running, and owes it the working
      // line and a Stop, not a Send that invites the same words again. A box
      // that DIED holding `working` cannot take the composer this way: the
      // source answers `idle` for a machine that is gone, and Stop is live
      // whatever the machine's state.
      // Asked on EVERY reconcile, not only when the transcript has gone quiet:
      // this is where the machine's word is heard, and a word never heard is a
      // word that cannot stand through the socket gap that follows it.
      const working = machineWorking(chatId);
      const awaits = conversationAwaitsResponse(turns) || working;
      // A message on the RECORD that no machine has started is owed a turn on
      // a source whose machine starts it later: a reader arriving now (a
      // reload, a second tab) is in the same wait as the tab that sent it, so
      // it reads as in flight rather than as a settled chat with a composer
      // inviting the same words again.
      const owed = chatData().startsTurnsRemotely === true && awaitsTurnStart(base);
      // A deliberate release outlives the tokens that keep arriving after it —
      // the machine may not be stoppable, but the composer stays the reader's.
      // The turn ending is what ends the release.
      const released = prev.released && awaits;
      // The turn is over: nothing is streaming and the machine says it is
      // idle. That — not the composer going quiet — is what a queued message
      // was waiting for, so a reader who pressed Stop on an unstoppable turn
      // does not get their held message sent into the turn still running.
      //
      // A message RESTORED from a previous page session is the exception: it
      // waits for the reader's click however idle the chat is (see
      // `restored`). So is one the reader STOPPED: the press that ended the
      // turn is not the turn ending well, and a hold the server never saw must
      // not be sent by the very act of halting the chat (see `stopped`).
      // Either blocks the ones behind it rather than letting them past,
      // because the order the reader typed them in is the order the agent has
      // to see them in.
      //
      // `cancel` marks the hold stopped when the press was made HERE. A chat
      // the cloud serves has as many readers as it has people, though, and a
      // Stop pressed in somebody else's tab reaches this one as the turn going
      // from working to idle — which is exactly what an ordinary end looks
      // like, and an ordinary end is what releases a hold. So the machine's own
      // word that the turn was halted is read here, and the hold is cancelled
      // by whoever pressed the key.
      const stop = stopReported(chatId);
      const stoppedElsewhere = lastStopSeen.has(chatId) && stop !== lastStopSeen.get(chatId);
      lastStopSeen.set(chatId, stop);
      let queued = stoppedElsewhere
        ? prev.queued.map((held) => ({ ...held, stopped: true }))
        : prev.queued;
      halted.queue = stoppedElsewhere && queued.length > 0;
      if (
        queued.length > 0 &&
        !awaits &&
        !queued[0].restored &&
        !queued[0].stopped &&
        !queued[0].unsent
      ) {
        outgoing.message = queued[0];
        queued = queued.slice(1);
      }
      // A message the server took while a turn was running stops waiting when
      // that turn does — a real end, read off the transcript, not a clock and
      // not the composer going quiet. From here on the chat is working on the
      // reader's own message and the ordinary running-turn line says so.
      const queuedBehind =
        prev.queuedBehind !== null && stillTheRunningTurn(turns, prev.queuedBehind)
          ? prev.queuedBehind
          : null;
      // The echo retires the hand-off with the placeholder it stands for: a
      // later open of this chat must not adopt a turn the fold already holds.
      let pending = state.pending;
      if (echoed > 0 && state.pending[chatId] !== undefined) {
        pending = { ...state.pending };
        delete pending[chatId];
      }
      return {
        pending,
        byId: {
          ...state.byId,
          [chatId]: {
            ...prev,
            base,
            optimistic,
            baseUserCount,
            loading: false,
            // The transcript was read, so a failure an earlier read recorded
            // is no longer what this chat is showing.
            error: null,
            sending: released ? false : live || prev.sending || owed || working ? awaits : false,
            released,
            queued,
            queuedBehind,
          },
        },
      };
    });
    const going = outgoing.message;
    if (!going) {
      // A hold the stop cancelled must read the same way after a reload: it is
      // kept, and it does not go on its own.
      if (halted.queue) writeQueued(queueScope(), chatId, get().byId[chatId]?.queued ?? []);
      return;
    }
    // Written down BEFORE the send so a reload that lands between the two
    // cannot restore a message already on its way.
    writeQueued(queueScope(), chatId, get().byId[chatId]?.queued ?? []);
    // The automatic release always takes the head, so that is where a refusal
    // puts it back.
    doSend(chatId, going.text, going.opts, {
      intent: "queued",
      queued: { message: going, at: 0 },
    });
  };

  return {
    ...composerActions,
    byId: {},
    pending: {},

    holdPendingTurns(chatId, turns) {
      // REPLACES the slot rather than adding to it. One surface creates one
      // chat at a time and the reader is being moved onto the newest one, so a
      // hand-off nobody ever opened (a create whose navigation was abandoned)
      // cannot accumulate and surprise a later visit with a stale bubble.
      set({ pending: { [chatId]: turns } });
    },

    open(chatId) {
      // Whatever Stop the record already holds is where this chat stands for a
      // reader arriving now; the first reconcile records it, and only a halt
      // after that is one that happened to them.
      lastStopSeen.delete(chatId);
      // The word a previous open heard is that open's: a chat arriving now
      // hears the machine afresh, or nothing.
      lastTurnWord.delete(chatId);
      const cold = get().byId[chatId]?.queued === undefined;
      // Re-open from the folded `base` as the source of truth — drop any stale
      // optimistic left over from a prior view of this chat (the store is a
      // singleton, so entries persist), so a never-echoed send can't strand a
      // phantom bubble. `base` and `sending` are kept: `base` for a flash-free
      // reload, `sending` so a turn that was streaming when the user navigated
      // away doesn't read as ended on the way back — `reconcile` re-derives
      // both from the folded transcript (it can clear a stale true, and a
      // never-echoed send clears because its base ends on a finished turn).
      set((state) => {
        const base = state.byId[chatId]?.base ?? [];
        const sending = state.byId[chatId]?.sending ?? false;
        // What this chat is holding for the reader. Kept across a navigation
        // like `base`, and read back from the browser on a cold open: the
        // reader typed it, was told it would go, and a reload is the commonest
        // thing to happen while they wait.
        const queued = state.byId[chatId]?.queued ?? restoredQueue(chatId);
        // A message this tab sent is still waiting behind the same turn after a
        // navigation, exactly as `sending` is still true. The first reconcile
        // re-derives it from the transcript and clears a stale one.
        const queuedBehind = state.byId[chatId]?.queuedBehind ?? null;
        // A chat this tab just created opens holding the message that created
        // it (see `pending`). The hand-off stays held until the echo retires
        // it, so a second open in the same breath adopts the same turn.
        const held = state.pending[chatId] ?? [];
        return {
          byId: {
            ...state.byId,
            [chatId]: {
              ...EMPTY,
              base,
              baseUserCount: countUserTurns(base),
              optimistic: held,
              // The handed-over turn is a turn in flight: it was posted a
              // moment ago and nothing has answered it, so the chat opens
              // working rather than idle under an unanswered message.
              sending: sending || held.length > 0,
              loading: true,
              queued,
              queuedBehind,
            },
          },
        };
      });
      // A cold open took over whatever a previous page left on the wire as
      // rows of its own, so the wire record is spent and the rows are what
      // is written down from here on.
      if (cold) {
        writeQueued(queueScope(), chatId, get().byId[chatId]?.queued ?? []);
        clearInFlight(queueScope(), chatId);
      }
      refreshDetached(chatId, false);
      return chatData().subscribeChat(chatId, (event) => {
        // The open-chat REPLAY is the initial history load, NOT live activity —
        // reconcile it as non-live so a chat whose last turn never received its
        // (late) completion event doesn't light the working indicator on reopen.
        // Older pages joining the window are history too, and are reconciled
        // without echo accounting (see `reconcile`).
        const history = event.kind === "history_loaded";
        refreshDetached(chatId, !event.replay && !history, { history });
      });
    },

    reload(chatId) {
      set((state) => {
        const prev = state.byId[chatId] ?? EMPTY;
        return { byId: { ...state.byId, [chatId]: { ...prev, loading: true, error: null } } };
      });
      refreshDetached(chatId, false);
    },

    send(chatId, text, opts) {
      // The press is the reader's word on what this composer sends next, so it
      // supersedes an automatic retry whichever way their words go: out now,
      // or into the queue behind a held message. Without this the press and
      // the timer both land and the same message is recorded twice — asked of
      // the model twice, billed twice, and in the chat twice for everyone who
      // reads it — or, on the queued branch, the older words go out unattended
      // while the reader's own sit waiting behind them.
      disarmRetry(chatId);
      // A message the composer is still holding for the reader's word — one
      // their Stop cancelled, one restored from a previous page — goes before
      // anything typed after it, or the reader's order is not the transcript's.
      const held = get().byId[chatId]?.queued ?? [];
      if (held.length > 0 && (held[0].stopped || held[0].restored || held[0].unsent)) {
        get().queueMessage(chatId, text, opts);
        return;
      }
      doSend(chatId, text, opts);
    },

    queueMessage(chatId, text, opts) {
      // Stamped as well as counted: the counter restarts at zero on a reload,
      // and a queue read back from the browser still holds the ids minted
      // before it — two messages answering to the same id would let an edit or
      // a removal land on the wrong one.
      const message: QueuedMessage = { id: `queued-${Date.now()}-${queuedSeq++}`, text, opts };
      reviseQueue(chatId, (queued) => [...queued, message]);
    },

    editQueued(chatId, id, text) {
      reviseQueue(chatId, (queued) =>
        queued.map((held) => (held.id === id ? { ...held, text } : held)),
      );
    },

    removeQueued(chatId, id) {
      disarmOutbox(id);
      reviseQueue(chatId, (queued) => queued.filter((held) => held.id !== id));
    },

    sendQueued(chatId, id) {
      // The reader's Retry is the retry: the timer that would have sent the
      // same row goes, or the row is sent twice.
      disarmOutbox(id);
      // Same two-step as the automatic flush: taken out of the entry inside
      // the synchronous update that names it, written down before it goes.
      const outgoing: { message: QueuedMessage | null; at: number } = { message: null, at: 0 };
      set((state) => {
        const prev = state.byId[chatId] ?? EMPTY;
        const at = prev.queued.findIndex((message) => message.id === id);
        const held = at < 0 ? undefined : prev.queued[at];
        if (!held) return state;
        const owes =
          conversationAwaitsResponse([...prev.base, ...prev.optimistic]) ||
          machineWorking(chatId);
        // A turn is still running, so this cannot go yet — but the reader has
        // said they want it, and that is exactly what a message waiting on
        // nobody's click is: one that goes by itself when the turn ends.
        const queued = owes
          ? prev.queued.map((message) =>
              message.id === id
                ? { ...message, restored: false, stopped: false, unsent: false }
                : message,
            )
          : prev.queued.filter((message) => message.id !== id);
        if (!owes) {
          outgoing.message = held;
          // Where it was standing, which is where a refusal must put it back:
          // this one can be released from anywhere in the queue.
          outgoing.at = at;
        }
        return { byId: { ...state.byId, [chatId]: { ...prev, queued } } };
      });
      writeQueued(queueScope(), chatId, get().byId[chatId]?.queued ?? []);
      const going = outgoing.message;
      if (going) {
        doSend(chatId, going.text, going.opts, {
          intent: "queued",
          queued: { message: going, at: outgoing.at },
          // A row that already went once goes again as the same message.
          ...(going.clientId ? { clientId: going.clientId } : {}),
        });
      }
    },

    cancel(chatId) {
      // A retry waiting on a timer is exactly the kind of send-that-goes-by-
      // itself this press exists to stop.
      disarmRetry(chatId);
      set((state) => {
        const prev = state.byId[chatId] ?? EMPTY;
        // Everything this browser is holding for the end of that turn is
        // cancelled with it. The words stay — they are the reader's, and
        // nothing else is keeping them — but they stop being something that
        // goes on its own, so the turn's end delivers nothing the press just
        // asked to stop.
        // A row the server could not take keeps its own word, "Not sent", and
        // its Retry; only its timer goes.
        for (const held of prev.queued) if (held.unsent) disarmOutbox(held.id);
        const queued = prev.queued.map((held) => (held.unsent ? held : { ...held, stopped: true }));
        return {
          byId: { ...state.byId, [chatId]: { ...prev, sending: false, released: true, queued } },
        };
      });
      writeQueued(queueScope(), chatId, get().byId[chatId]?.queued ?? []);
    },

    reopenAndResend(chatId) {
      const stale = get().byId[chatId]?.staleSend;
      get().dismissStaleSend(chatId);
      if (!stale) return;
      // Only the placeholders that exist at CLICK time are leftovers of the
      // refused send. One enqueued by a newer send during the reopen refresh
      // must survive the clear, and a newer staleSend must survive the resend.
      const leftovers = new Set((get().byId[chatId]?.optimistic ?? []).map((t) => t.id));
      // The resend waits for the reopened fold to APPLY: the replay drops the
      // failure-fold user turn, and a resend enqueued against the stale count
      // would never retire its placeholder when the echo lands. The message is
      // never silently dropped, and never blindly duplicated.
      void chatData()
        .reopenChat(chatId)
        .catch(() => undefined)
        .then(() => refresh(chatId, false))
        .then(
          () => resendAfterApply(chatId, stale, leftovers),
          () => repromptOnFailure(chatId, stale),
        );
    },

    dismissStaleSend(chatId) {
      set((state) => {
        const prev = state.byId[chatId] ?? EMPTY;
        return { byId: { ...state.byId, [chatId]: { ...prev, staleSend: null } } };
      });
    },
  };
});
