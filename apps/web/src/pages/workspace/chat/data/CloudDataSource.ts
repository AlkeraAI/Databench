// The browser's chat source: the cloud's REST surface plus the doc-sync socket.
//
// The same composition the extension drives against a local daemon runs against
// this, so the shapes it hands back are identical — the difference is entirely
// where they come from:
//
// * the TRANSCRIPT is the chat's document (`doc:chat:<id>`). The `hello`
//   snapshot seeds the retained window, each durable `append` op adds the events
//   the publisher persisted, and the ephemeral lane carries the token chunks
//   that are never persisted. Every one of them goes through the SAME fold the
//   daemon source uses, so a tool card renders identically in both shells;
// * HISTORY older than that window comes from `GET /chats/{id}/messages`, which
//   is also the recovery path: a page that answers with `resync_from` is the
//   in-band "start again from here" marker, so an old cursor is never an error;
// * a SEND is `POST /chats/{id}/messages`. The server persists it, assigns the
//   sequence and relays it to the publisher, so this client does not write to
//   the document at all — the publisher is the document's only writer.
//
// What this source cannot do, it says so: the cloud has no OpenCode harness of
// its own to ask for a model catalogue or a permission mode, so `caps` reports
// `opencodeActive: false` and the surfaces that gate on it stand down rather
// than poll an endpoint that does not exist.

import type {
  BlobPage,
  CostLedgerEntryView,
  CostStateView,
  DecisionView,
  PermissionConversationPart,
} from "@alkera/chat-model";
import { PERMISSION_MODE_VALUES } from "@alkera/chat-model";

import { ApiError } from "../../../../api/errors";
import { retryAfterMs } from "../../../../api/retry";
import {
  CATCH_UP_RETRY,
  CATCH_UP_RETRY_ATTEMPTS,
  CATCH_UP_RETRY_FLOOR_MS,
  ladderDelay,
  RETRY_JITTER_RATIO,
} from "../../../../lib/limits";
import { keys } from "../../../../api/keys";
import { queryClient } from "../../../../api/queryClient";
import { acquireRealtimeClient, getRealtimeClient } from "../../../../api/realtime/client";
import { acquireLiveDraft, type LiveDraft } from "../../../../api/realtime/crdt/liveDraft";
import { openDoc, type DocHandle, type OpPayload } from "../../../../api/realtime/docSync";
import type { AccountScope } from "../../../../lib/accountScope";
import { hueOf } from "@/lib/personHue";

import {
  APPROVAL_PENDING,
  APPROVAL_REACHES,
  type ApprovalVerdict,
  type CancelOutcome,
  type ChatCapabilities,
  type ChatDataSource,
  type TurnState,
} from "./ChatDataSource";
import type { ChatPermissionMode, CreateChatOptions } from "../../../../api/cloudChat/transport";
import * as cloud from "../../../../api/cloudChat/transport";
import { cloudChatFiles, type ChatFilesPort } from "./chatFiles";
import { titleFrom } from "./chatTitle";
import type { ChatMessageRead, ChatSessionRead } from "../../../../api/cloudChat/transport";
import {
  createConversationFoldState,
  foldHarnessEvent,
  foldRelayedAnswer,
  foldRelayedPrompt,
  setFoldingSeq,
  promptsWereTaken,
  replayVerdict,
  settleStaleInterrupts,
  type ConversationFoldState,
  type HarnessEvent,
} from "./harnessEventFold";
import {
  TRANSCRIPT_PAGE_ROWS,
  TRANSCRIPT_WINDOW_MAX_ROWS,
  TranscriptWindow,
  type TranscriptWindowAdapter,
} from "./transcriptWindow";
import type {
  Chat,
  ChatActivity,
  ChatDefaults,
  ChatEvent,
  ChatMessage,
  ChatTurn,
  FileSearchResult,
  ModelInfo,
  ModelOptions,
  PermissionMode,
  SendOptions,
  SlashCommandInfo,
  SlashCommandOutcome,
  Unsubscribe,
} from "./model";

/** The snapshot the chat doc strategy keeps: the retained window of events, by
 *  the ids it de-duplicates them with. */
interface ChatDocState {
  meta?: Record<string, unknown>;
  events?: Record<string, unknown>[];
  ids?: Record<string, number>;
}

/** One transcript entry as it rides the document. `payload` is the harness event
 *  the fold reads; the rest is the envelope the server sequences it by. */
interface DocEvent {
  event_id?: unknown;
  role?: unknown;
  kind?: unknown;
  payload?: unknown;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;

/** The stance a cloud chat falls back to before anything authoritative has been
 *  read — the floor the server itself applies to a reader who saved no default.
 *
 *  It is a FALLBACK, not a fact: the stance a given chat runs in is decided by
 *  the server from the reader's saved default, so the chat page reads the chat
 *  row and the home composer reads `resolveChatDefaults().permissionMode`. This
 *  is only what either shows in the moment before that answer arrives. */
export const CLOUD_PERMISSION_MODE = "read_only";

/** The stances a browser reader may put a cloud chat in, in the order the
 *  picker should read them.
 *
 *  The editor's five, in the editor's own order. `bypass` is here because a
 *  reader who knows the extension's stances and does not find it in the
 *  browser reads its absence as a product that cannot do it, not as a policy —
 *  and it is the stance a long unattended run needs. The guard that matters is
 *  not the absent option: a cloud chat's writes are fenced to its own folder in
 *  EVERY mode, and its connected data is read-only in every mode, so what
 *  `bypass` hands over is the asking, never the boundary.
 *
 *  `auto` is the ceiling: the stance to raise a chat to when the model needs
 *  something the analyst modes refuse outright — a page no search of its own
 *  returned, say — and the reader would rather have it judged than refused. Its
 *  middle is cleared by the safety judge the box builds at start.
 *
 *  The list is the generated one, which the server's `CloudPermissionMode` is
 *  pinned to, so the surface agrees with the machine rather than being the only
 *  thing holding the line. */
const CLOUD_MODES: readonly string[] = PERMISSION_MODE_VALUES;

type CloudPermissionMode = ChatPermissionMode;

/** Whether a mode string is one the cloud create/switch routes accept. A stance
 *  this source cannot open a chat in is dropped rather than sent: the server
 *  would refuse the whole create over it, losing the chat for a stance the
 *  reader could have corrected afterwards. */
const isCloudPermissionMode = (mode: string): mode is CloudPermissionMode =>
  CLOUD_MODES.includes(mode);


/** The turn state the publisher writes into the document's meta, as the
 *  browser reads it back (`{"turn_state": {"state": "working" | "idle"}}`). */
function turnStateOf(meta: unknown): TurnState | null {
  if (!isRecord(meta)) return null;
  const turn = meta.turn_state;
  if (!isRecord(turn)) return null;
  return turn.state === "working" ? "working" : turn.state === "idle" ? "idle" : null;
}

/** The id to remember a HALTED turn under — a Stop somebody pressed, or a
 *  budget the mirror enforced — or null for anything else the machine says.
 *
 *  A turn that merely finished is not one of these: `idle` is the box saying it
 *  is done, `aborted` is the box saying it was stopped, and the difference is
 *  what a message this browser is holding for the end of the turn hangs on —
 *  the end of a halted turn must not be what sends it.
 *
 *  An id rather than a count, because the same stop is read more than once: a
 *  re-hello replays the retained window it sits in, and a durable page brings
 *  it back again. Recognising it as the stop already seen is what keeps a
 *  second reading from cancelling a message typed after it. */
function stopIdOf(event: HarnessEvent, eventId: string | null): string | null {
  if (event.event_type !== "session.status_changed") return null;
  if (event.status !== "aborted" && event.status !== "cancelled") return null;
  if (eventId !== null && eventId !== "") return eventId;
  const own = event.event_id;
  if (typeof own === "string" && own !== "") return own;
  // A frame with no id of its own is still the machine saying the turn was
  // halted; the turn it names and the moment it names it are what tell two
  // such stops apart.
  return `${String(event.turn_id ?? "")}@${String(event.time ?? "")}`;
}

/** The permission kinds whose approval would let a mutating action run, where
 *  the classifier gave no verdict to read instead. The mirror keeps the same
 *  set (`WRITE_CLASS_KINDS`) and refuses any answer that approves one on a
 *  session that refuses writes; `apps/cli/tests/cloud/fixtures/write_class_asks.json`
 *  is the table both sides are driven over. */
export const WRITE_CLASS_KINDS = new Set<string>(["edit", "shell", "task", "external"]);

/** Whether approving this ask would authorize a WRITE — the same reading the
 *  mirror makes before it settles one (`_authorizes_a_write`).
 *
 *  The classifier's verdict is read FIRST, because the kind alone cannot answer
 *  it: `shell` is the canonical kind of every command the harness asks about,
 *  so a kind-first answer makes `ls` as much a write as `rm` — and withholds
 *  the Allow on an ask the box would have settled. Only a positive `read`
 *  excuses an ask; a verdict that is missing, empty, or a word this build does
 *  not know leaves the kind to answer, which is the fail-closed side. */
function authorizesAWrite(part: PermissionConversationPart): boolean {
  // Widened deliberately: the declared union is what the classifier is MEANT to
  // send, and this value arrives over the wire — an empty string and a word
  // from a newer box are both shapes the type says cannot happen and the
  // network can still deliver. Each falls through to the kind, which is the
  // fail-closed side.
  const effect: string | undefined = part.subject?.effect;
  if (typeof effect === "string" && effect !== "") return effect !== "read";
  return WRITE_CLASS_KINDS.has(part.canonicalKind);
}

/** The harness event inside a transcript entry, or null when the entry carries
 *  none (a shape from a newer publisher: dropped, never thrown on). */
function harnessEventOf(entry: unknown): HarnessEvent | null {
  if (!isRecord(entry)) return null;
  const { payload } = entry as DocEvent;
  if (isRecord(payload)) return payload;
  return null;
}

/** The transcript row's `kind` for a person's message. Spelled once here, as
 *  the server spells it once beside the record model it writes. */
const PROMPT_KIND = "prompt";

/** The relay a route mints when a person switches the chat's permission mode —
 *  the same word the mirror keys on (`MODE_RELAY_KIND`). */
const MODE_RELAY_KIND = "mode";

/** How many pages further down a cold open reads to find the opening of a
 *  machine message its tail page holds only the end of, when the page did not
 *  say so: a server from before pages were kept outside every message anchors
 *  a page on a person's message even when that message sits inside the
 *  machine's. A server with that rule never hands such a page, so against it
 *  this costs no read at all. */
const MID_MESSAGE_PAGE_REACH = 2;

/** How many pages further down it reads when the page DID say so (`cut`) and
 *  still begins inside a message: the server reached as far as its own budget
 *  and the message carries on below. The reader keeps going until the message
 *  is whole; the bound is only so one enormous message cannot pull a
 *  transcript's worth of pages into an open. */
const MID_MESSAGE_CUT_REACH = 8;

/** The relay a Stop rides to the box (`StopRelay`). The note the server wrote
 *  for it is in the transcript, not on the socket. */
const STOP_RELAY_KIND = "stop";

/** A member's answer to an ask, as the server relays it to the box
 *  (`AnswerRelay`): it rides the prompt kind and is told apart by naming the
 *  ask. Only a permission choice settles a card here; a question's answer is
 *  read back off the recorded row. */
function relayedAnswerOf(event: unknown): { requestId: string; optionId?: string } | null {
  if (!isRecord(event) || event.kind !== PROMPT_KIND) return null;
  const requestId = event.interrupt_id;
  if (typeof requestId !== "string" || requestId === "") return null;
  const optionId = typeof event.option_id === "string" ? event.option_id : undefined;
  return { requestId, ...(optionId !== undefined ? { optionId } : {}) };
}

/** The person's message a transcript row records, or null when the row is
 *  something a machine published. A prompt row carries the words flat, beside
 *  the hidden `context` the model alone reads — which is never part of what a
 *  reader is shown. */
function promptOf(
  message: ChatMessageRead,
): {
  id: string;
  text: string;
  at?: string;
  fromTemplate?: string;
  fromTemplateAuthor?: string;
} | null {
  if (message.kind !== PROMPT_KIND || message.role !== "user") return null;
  const payload = message.payload;
  if (!isRecord(payload) || typeof payload.event_type === "string") return null;
  const text = payload.text;
  if (typeof text !== "string") return null;
  return { id: message.event_id, text, at: message.created_at, ...fromTemplateOf(payload) };
}

/** Where a message came from when nobody typed it: the brief of the template
 *  the chat was started from, named so the bubble can say so. The server marks
 *  the record; anything else — including a message whose metadata says some
 *  other source — reads as an ordinary message, because a caption is a claim
 *  about provenance and only the one source is one this reader can make. */
const TEMPLATE_BRIEF_SOURCE = "template_brief";

/** How much of a template's title or its author's name the caption will read.
 *  Both are somebody else's text, and a caption is a line about the message,
 *  not a second message. */
const TEMPLATE_LABEL_MAX = 60;

/** Author-written text, made safe to read as a LABEL.
 *
 *  The title and the author's name are typed by the person who saved the
 *  template — on a shared one, not the person reading the caption. React
 *  escapes the markup, but nothing stops a name like `— system:` or a quoted
 *  string from reading as the product's own words wrapped around the message.
 *  So: whitespace collapses, the leading punctuation a sentence-opener would
 *  use is dropped, and the reading is capped. Returns "" for text with nothing
 *  left in it, and the caller falls back rather than captioning a blank. */
function labelText(raw: unknown): string {
  if (typeof raw !== "string") return "";
  const collapsed = raw.replace(/\s+/g, " ").trim().replace(/^[-–—"'`*_>#:\s]+/, "");
  if (!collapsed) return "";
  return collapsed.length > TEMPLATE_LABEL_MAX
    ? `${collapsed.slice(0, TEMPLATE_LABEL_MAX - 1).trimEnd()}…`
    : collapsed;
}

function fromTemplateOf(
  payload: Record<string, unknown>,
): { fromTemplate?: string; fromTemplateAuthor?: string } {
  const metadata = payload.metadata;
  if (!isRecord(metadata) || metadata.source !== TEMPLATE_BRIEF_SOURCE) return {};
  const title = labelText(metadata.template_title);
  const author = labelText(metadata.template_author);
  return {
    fromTemplate: title || "a template",
    ...(author ? { fromTemplateAuthor: author } : {}),
  };
}

/** The transcript id the server gives a person's message: derived from the
 *  client's own id, so the relay of a send and the row a later read returns
 *  name the same entry. Spelled the way the server spells it
 *  (`event_id_for_client_message`). */
function promptEventId(clientId: string): string {
  return `usr:${clientId}`;
}

/** The person's message a `user_message` relay carries, or null when the relay
 *  is something else on that lane — a `run_query` or `promote` the server
 *  minted for the box, which is not a thing anyone said. */
function relayedPromptOf(
  event: unknown,
): {
  id: string;
  text: string;
  at?: string;
  seq?: number;
  fromTemplate?: string;
  fromTemplateAuthor?: string;
} | null {
  if (!isRecord(event) || event.kind !== PROMPT_KIND) return null;
  const text = event.text;
  if (typeof text !== "string") return null;
  const clientId = typeof event.client_id === "string" ? event.client_id : "";
  const messageId = typeof event.message_id === "string" ? event.message_id : "";
  const id = clientId ? promptEventId(clientId) : messageId;
  if (!id) return null;
  // The stamp the server put on the recorded row, so the relay and the row a
  // later read returns say the same time.
  const at = typeof event.at === "string" ? { at: event.at } : {};
  // The recorded row's sequence rides the relay too: the message's place in
  // the transcript, which is what its echo is matched by.
  const seq = typeof event.seq === "number" ? { seq: event.seq } : {};
  return { id, text, ...at, ...seq, ...fromTemplateOf(event) };
}

/** What the window holds for a cloud chat: a REST row, or an entry off the
 *  chat document. Both carry the transcript sequence when they have one (a
 *  row always; a snapshot entry and a live append since the server stamps
 *  each with the sequence its durable write assigned; an ephemeral token
 *  chunk never) and the event id the two are de-duplicated by. */
type CloudRow =
  | { kind: "row"; seq: number; eventId: string; message: ChatMessageRead }
  | {
      kind: "doc";
      seq: number | null;
      ephemeral?: boolean;
      eventId: string | null;
      role: string | null;
      event: HarnessEvent;
    };

/** The harness event a window row folds — for a REST row, the entry the
 *  machine published one level in (`applyMessages` explains the unwrapping). */
function eventOf(row: CloudRow): HarnessEvent | null {
  if (row.kind === "doc") return row.event;
  const payload = row.message.payload;
  if (isRecord(payload) && typeof payload.event_type === "string") return payload;
  return harnessEventOf(payload) ?? (isRecord(payload) ? payload : null);
}

/** The id prefix of a row that answers no waiting message — a note the server
 *  or the machine wrote about the chat itself (a mode change), not the agent
 *  taking the messages above it up. The server's `ASIDE_NOTE_PREFIX`. */
const ASIDE_NOTE_PREFIX = "aside-";

function isAsideRow(row: CloudRow): boolean {
  return typeof row.eventId === "string" && row.eventId.startsWith(ASIDE_NOTE_PREFIX);
}

const cloudWindowAdapter: TranscriptWindowAdapter<CloudRow> = {
  // The agent runs on the org's workspace machine, not in this browser's
  // process tree, so a crash under the turn is said in those words.
  createState: () => createConversationFoldState({ agentHost: "workspace" }),
  foldRows(state, rows) {
    for (const row of rows) {
      if (row.kind === "row") {
        // A person's message is not something a machine said: the server
        // wrote it the moment it accepted the send, and it is the transcript's
        // record of the words whether or not a box has taken the turn yet.
        const prompt = promptOf(row.message);
        if (prompt !== null) {
          foldRelayedPrompt(state, { ...prompt, seq: row.seq });
          continue;
        }
      }
      const event = eventOf(row);
      // Where this row stands, so an echo it opens is tied to the saying it
      // answers by its place in the transcript and not by its words.
      setFoldingSeq(state, row.seq);
      if (event) foldHarnessEvent(state, event);
      setFoldingSeq(state, null);
      // A row the machine wrote into the transcript after a prompt was recorded
      // is the transcript's word that the prompt was taken — the same reading
      // whether the row lands live off the socket or on a page a reload reads.
      if (row.seq !== null && !isAsideRow(row)) promptsWereTaken(state);
    }
  },
  // A turn begins where a person spoke: the row the server recorded for the
  // message, which is where the server cuts its pages. The box's own
  // `message.created` for that message is not one on a page — it repeats the
  // row, and is re-announced from inside the answer every time the session
  // moves on. The document lane carries no prompt rows, so there the box's
  // announcement is the nearest thing to a turn start the live-edge trim has.
  startsTurn(row) {
    if (row.kind === "row") return promptOf(row.message) !== null;
    if (row.role === "user") return true;
    return row.event.event_type === "message.created" && row.event.role === "user";
  },
  pageMessageOf(row) {
    // `chat_service.message_id_for_event`, row for row: the id a row states,
    // flat or on its part, whoever published it — except a cancelled prompt's
    // row, whose id names the prompt it cancels.
    const kind = row.kind === "row" ? row.message.kind : null;
    const event = eventOf(row);
    if (!event || kind === "prompt.cancelled" || event.event_type === "prompt.cancelled") return null;
    if (typeof event.message_id === "string" && event.message_id !== "") return event.message_id;
    const part = isRecord(event.part) ? event.part : null;
    return part && typeof part.message_id === "string" && part.message_id !== "" ? part.message_id : null;
  },
  opensPageMessage(row) {
    const event = eventOf(row);
    if (!event || event.event_type !== "message.created") return null;
    return typeof event.message_id === "string" && event.message_id !== "" ? event.message_id : null;
  },
  opensMachineMessage(row) {
    const role = row.kind === "row" ? row.message.role : row.role;
    if (role !== "assistant") return null;
    const event = eventOf(row);
    if (!event || event.event_type !== "message.created") return null;
    return typeof event.message_id === "string" && event.message_id !== "" ? event.message_id : null;
  },
  elidedTurnIds(row) {
    const event = eventOf(row);
    if (!event || event.event_type !== "compaction.applied") return [];
    const ids = event.summarised_message_ids;
    return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string") : [];
  },
};

/** REST rows as window rows. */
function windowRows(messages: readonly ChatMessageRead[]): CloudRow[] {
  return messages.map((message) => ({
    kind: "row",
    seq: message.seq,
    eventId: message.event_id,
    message,
  }));
}

function chatOf(read: ChatSessionRead): Chat {
  const pin = read.model;
  return {
    id: read.id,
    title: read.title,
    updatedAt: read.updated_at,
    // The model is pinned when the chat is created, so the composer greys its
    // picker and shows what this chat actually answers on — the same reading
    // the editor makes off the manifest.
    ...(pin
      ? {
          model: {
            id: pin.id,
            efforts: pin.efforts ?? [],
            effort: pin.effort ?? null,
            // The catalog's window, recorded on the pin when the chat was
            // created. It is the denominator the composer's context readout
            // spends against; a catalog that reported none leaves it 0 and the
            // readout states the count alone.
            contextWindow: pin.context_window || undefined,
          },
        }
      : {}),
    permissionMode: read.permission_mode,
    parentSessionId: null,
    lastEventId: null,
  };
}

/** How the source is reached and what it reads through — every one of them
 *  injectable, so a test drives the whole source with no socket and no fetch. */
export interface CloudDataSourceOptions {
  rest?: typeof cloud;
  /** Open one document; defaults to the shared portal socket. */
  openDoc?: (chatId: string) => DocHandle<ChatDocState>;
  /** Hold the shared socket open while a chat is subscribed. */
  acquire?: () => () => void;
  /** Ids for optimistic sends. */
  clientId?: () => string;
  /** Who is signed in and in which org, read when a live draft opens: the
   *  edits a page leaves unsent are stashed under them. A shell that names
   *  nobody keeps no stash. */
  account?: () => AccountScope | null;
  /** How many rows a transcript page is asked for. Default: the portal's. It
   *  is also what a page's size is read against — see `messageCutBelow`. */
  pageRows?: number;
  /** The jitter a refused read's backoff is spread by. Injected so a test can
   *  pin both ends of the band instead of racing a random one. */
  random?: () => number;
}

/** Whether a refused read is worth asking again.
 *
 *  A limiter, a timeout and a server that broke all answer differently on the
 *  next ask, and so does a transport that never got an answer at all — there is
 *  no status to read on that one, so it climbs. Every other 4xx is the server's
 *  SETTLED word about this request: the chat was deleted, the reader's access
 *  went away, the credential expired. Asking again cannot change any of those,
 *  so the ladder is not armed and the source stops claiming the chat is current
 *  at once, rather than spending three minutes re-asking for a chat nobody is
 *  going to be shown. */
function worthAnotherRead(err: unknown): boolean {
  if (!(err instanceof ApiError)) return true;
  if (err.status === 408 || err.status === 429) return true;
  return err.status < 400 || err.status >= 500;
}

/** How long to wait before re-reading, after the `attempt`-th read was refused.
 *
 *  The same shape the portal's own reads use (`queryRetryDelay`): the server's
 *  word wins where it gave one — a limiter that says "in two seconds" knows
 *  something this tab does not — the floor holds under both paths so a
 *  `Retry-After` of `0` cannot spin, and the jitter on top is what keeps every
 *  tab an org-wide limiter refused in one second from coming back in one
 *  second. `random` is injected so a test can pin both ends of the band. */
function catchUpDelayMs(attempt: number, err: unknown, random: () => number): number {
  const asked = retryAfterMs(err);
  const climb = ladderDelay(attempt - 1, CATCH_UP_RETRY);
  const base = asked === null ? climb : Math.max(asked, CATCH_UP_RETRY_FLOOR_MS);
  return Math.round(base * (1 + RETRY_JITTER_RATIO * random()));
}

let clientSeq = 0;
function defaultClientId(): string {
  clientSeq += 1;
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(36).slice(2, 10);
  return `web-${random}-${clientSeq}`;
}

/** What a result page says in the browser, where a result is read only once it
 *  has been saved from the chat. */
export const BLOB_NOT_SAVED =
  "This result is on the machine that produced it. Save it from the chat to open it here.";

export class CloudDataSource implements ChatDataSource {
  /** The cloud drives no OpenCode harness of its own — no slash-command
   *  registry, no effort to set on a running session — but it DOES serve the
   *  two things a reader chooses before a chat starts: the gateway's model
   *  catalogue, through the portal's own route, and the stance the session is
   *  opened in. `fixedPermissionMode` stays the mode a chat STARTS in, so the
   *  home composer's chip states it before there is a chat to ask. */
  readonly caps: ChatCapabilities = {
    opencodeActive: false,
    modelCatalog: true,
    // A reader may move an OPEN cloud chat onto another model or effort: the
    // box runs each turn on the chat's current pin, the server refuses a
    // switch that would lose the chat's reasoning, and the picker shows each
    // model's verdict (`modelOptions`). A box on an older build applies a
    // switch once the chat's agent restarts, and the options say so.
    switchableModel: true,
    permissionModes: CLOUD_MODES,
    fixedPermissionMode: CLOUD_PERMISSION_MODE,
  };

  /** A box runs one turn at a time. The server records a message the moment it
   *  takes it and answers straight away, so a message sent into a chat that is
   *  mid-turn is accepted and then waits on that chat's prompt lane until the
   *  running turn ends. */
  readonly holdsSendsBehindTurn = true;

  /** The server records a message and the chat's box starts its turn when it
   *  takes the chat up — seconds later, or minutes when the box is busy
   *  coming up. */
  readonly startsTurnsRemotely = true;

  /** The chat's files over the Files REST surface: pasted images land in the
   *  chat's folder and the transcript loads them back by content URL. */
  readonly chatFiles: ChatFilesPort = cloudChatFiles();
  private readonly rest: typeof cloud;
  private readonly open: (chatId: string) => DocHandle<ChatDocState>;
  private readonly acquire: () => () => void;
  private readonly newClientId: () => string;
  private readonly account: () => AccountScope | null;
  private readonly random: () => number;

  private seq = 0;
  private chats: Chat[] = [];
  /** The loaded window per chat: the tail it opened on, every page scrolled
   *  into above it, and the live segment the socket appends to. It owns the
   *  fold states and the de-duplication by event id — a REST page and a
   *  snapshot can carry the same entry. */
  private readonly windows = new Map<string, TranscriptWindow<CloudRow>>();
  /** Whether the durable record holds rows above each chat's window, and
   *  whether a page is on its way. Written only by the reads that know. */
  private readonly history = new Map<string, { hasOlder: boolean; loading: boolean }>();
  /** Whether the lowest page read was cut INSIDE A MESSAGE by a server that
   *  had reached for it: the page says `cut` and holds more rows than were
   *  asked for. `cut` alone does not say that. Every server says it of a page
   *  whose first row is not a person's message — a turn longer than its reach
   *  — and a server from before pages were kept outside a message never hands
   *  back more than the limit. One that reached past the page to get outside
   *  a message, and ran out of budget doing it, hands back the limit PLUS that
   *  budget; that surplus is its signature, and the only case where reading on
   *  is this reader's job. So a portal talking to the older server behaves
   *  exactly as it did. */
  private readonly messageCutBelow = new Map<string, boolean>();
  private readonly pageRows: number;

  private noteCut(chatId: string, page: { cut?: boolean; items: readonly unknown[] }): void {
    this.messageCutBelow.set(chatId, (page.cut ?? false) && page.items.length > this.pageRows);
  }
  /** One durable read in flight per chat: the cold open, the socket's first
   *  snapshot and a re-hello all ask for the same page. */
  private readonly catching = new Map<string, Promise<void>>();
  /** A catch-up asked for while one is already running or waiting out a
   *  backoff. ONE is kept, never a queue: the request is "bring this chat up to
   *  date", so two of them collapse into the single read that follows — a
   *  streaming turn that relays a dozen times must not put a dozen reads on a
   *  server that is already refusing. */
  private readonly catchUpPending = new Map<string, { settle: boolean }>();
  /** The armed retry per chat. Its presence is also what makes a trigger that
   *  arrives mid-backoff JOIN the ladder instead of starting a second one. */
  private readonly catchUpTimer = new Map<string, ReturnType<typeof setTimeout>>();
  /** How far this chat has been READ FORWARD from the durable record: every row
   *  up to this sequence has been paged. Only `catchUp` writes it.
   *
   *  Emphatically NOT "the highest sequence we have seen": the response to our
   *  own send names a row far ahead of anything read, and moving the cursor
   *  onto it declares the history below it read when it never was. A chat
   *  started from the composer is created BY a send, so that is the FIRST thing
   *  this source learns about it — and the source then answered with a
   *  transcript holding only the message it had just sent, for its whole life,
   *  because nothing would ever page from below the cursor again. */
  private readonly cursor = new Map<string, number>();
  private readonly subscribers = new Map<string, Set<(event: ChatEvent) => void>>();
  private readonly live = new Map<string, () => void>();
  /** The machine's last word on each chat's turn, from the document's meta. */
  private readonly turns = new Map<string, TurnState>();
  /** The last turn HALT the machine reported per chat, by the id of the event
   *  that carried it. Kept by identity so the same stop, met again on a
   *  re-hello or on a durable page, is recognised as one already seen. */
  private readonly stops = new Map<string, string>();
  /** Each followed chat's permission mode, as the row or the document last said
   *  it. The pill follows it, and `mayAllow` trusts the row's verdict only
   *  while the row was read in this same mode. */
  private readonly modes = new Map<string, string>();
  private readonly modeWatchers = new Map<string, Set<(mode: string) => void>>();

  constructor(opts: CloudDataSourceOptions = {}) {
    this.rest = opts.rest ?? cloud;
    this.open =
      opts.openDoc ?? ((chatId) => openDoc<ChatDocState>(getRealtimeClient(), "chat", chatId));
    this.acquire = opts.acquire ?? acquireRealtimeClient;
    this.newClientId = opts.clientId ?? defaultClientId;
    this.account = opts.account ?? (() => null);
    this.pageRows = opts.pageRows ?? TRANSCRIPT_PAGE_ROWS;
    this.random = opts.random ?? Math.random;
  }

  // --- chats ---------------------------------------------------------------

  async listChats(): Promise<Chat[]> {
    // Every chat, by following the cursor: the rail, the chat home and the
    // breadcrumb lookup all read this list, and a chat missing from it reads to
    // all three as one that was deleted.
    const page = await this.rest.listAllChats();
    this.chats = page.items.map(chatOf);
    return [...this.chats];
  }

  async createChat(initialMessage: string, opts?: SendOptions): Promise<Chat> {
    const chat = await this.startChat(initialMessage, opts);
    if (initialMessage.trim()) await this.sendUserMessage(chat.id, initialMessage, opts);
    return chat;
  }

  async startChat(title: string, opts?: SendOptions): Promise<Chat> {
    // The model rides the CREATE, not the first message: a chat's model is
    // pinned once, and the box reads the pin off the chat row when it opens the
    // session — by the time a message arrives the session already exists.
    // The stance rides the CREATE for the same reason: it is decided when the
    // chat row is written, so a mode the reader picked on the home chip has to
    // be named here or it is not the mode their first turn runs in. Omitting it
    // is how a composer that offers no stance control asks for the reader's
    // saved default — never a way to mean "read-only".
    // Built as the transport's own option type, field by field: a key the
    // transport does not know is then a type error. Spread in as a literal it
    // was not — the stance rode to the transport under a name it did not
    // read, and every chat opened at the read-only floor whatever the chip
    // had promised.
    const body: CreateChatOptions = {};
    if (opts?.model) body.model = opts.model.id;
    if (opts?.effort) body.effort = opts.effort;
    const mode = opts?.mode;
    if (mode && isCloudPermissionMode(mode)) body.permissionMode = mode;
    // The empty composer's send takes the chat warmed ahead for this reader
    // when one stands — its session already open on the box — and the picks
    // above are re-pinned onto it where they differ. With none standing the
    // server creates exactly as before, under the same 201.
    // A chat started in a named workspace is made there; a spare is warmed in
    // no workspace in particular, so it is claimed only when none is named.
    if (opts?.workspaceId) body.workspaceId = opts.workspaceId;
    else body.claimSpare = true;
    const created = await this.rest.createChat(titleFrom(title), body);
    const chat = chatOf(created);
    this.chats = [chat, ...this.chats.filter((c) => c.id !== chat.id)];
    return chat;
  }

  async deleteChat(_chatId: string, _title?: string): Promise<boolean> {
    // Deleting a cloud chat is not a capability this source offers, and a
    // silent success would leave the row on screen with nothing behind it.
    return false;
  }

  async reopenChat(chatId: string): Promise<void> {
    // A cloud chat has no session to re-open; what a failed send needs is the
    // history it may have missed.
    await this.catchUp(chatId);
  }

  async sendUserMessage(chatId: string, content: string, opts?: SendOptions): Promise<ChatMessage> {
    // The ids name nodes the portal already linked to this chat; the server
    // refuses one that is not (`chat.attachment_not_linked`), so the body is
    // never what attaches a file. An empty list is not sent at all.
    const attachments = opts?.attachments?.length ? { attachments: opts.attachments } : {};
    const message = await this.rest.postMessage(chatId, {
      text: content,
      client_id: opts?.clientId ?? this.newClientId(),
      ...attachments,
    });
    this.applyMessages(chatId, [message]);
    return { id: message.id, role: "user", content };
  }

  async getChatTurns(chatId: string): Promise<ChatTurn[]> {
    // Gate on the REST cursor, which only a durable read writes — NOT on the
    // fold, which the socket's snapshot also populates. The snapshot carries
    // only the retained window, so whenever it won the race the history was
    // never paged and the reader opened a chat holding hundreds of events on a
    // handful of them (sometimes on the empty state).
    // Only a COLD open is a replay: a fold the socket has already fed holds
    // live asks the reader can still answer, and retiring those would put the
    // cold-open fix in the way of answering (see `settleReplay`).
    if (!this.cursor.has(chatId)) {
      await this.catchUp(chatId, { settle: !this.windows.has(chatId) });
    }
    return this.windowFor(chatId).snapshot();
  }

  transcriptHistory(chatId: string): { hasOlder: boolean; loading: boolean } {
    const said = this.history.get(chatId) ?? { hasOlder: false, loading: false };
    // A window with no paging cursor cannot ask for the page above whatever the
    // last read said, and a surface told there is more re-arms its sentinel
    // against a request that can only no-op. The cursor is the truth.
    const window = this.windows.get(chatId);
    if (said.hasOlder && window && window.oldestSeq === null) {
      return { hasOlder: false, loading: said.loading };
    }
    return said;
  }

  /** Whether a cold open should read one more page below for the sake of the
   *  machine message its oldest rows begin inside.
   *
   *  Two servers can be on the other end, and a portal does not get to assume
   *  which. One that keeps its pages outside every message begins a page inside
   *  one only when the message outran its reach, and says so (`messageCutBelow`):
   *  the reader keeps going until the message is whole. One from before that rule says
   *  nothing, and gives itself away by the shape alone — a person's message
   *  leading, then the END of the machine message above it — which gets the
   *  short reach it always had. A page that begins outside every message asks
   *  for nothing, which is every page the first kind of server sends within
   *  its budget. */
  private readsOnForAMessage(chatId: string, reach: number): boolean {
    const window = this.windows.get(chatId);
    if (!window) return false;
    if ((this.messageCutBelow.get(chatId) ?? false) && window.beginsMidMessage(true)) {
      return reach < MID_MESSAGE_CUT_REACH;
    }
    return window.opensMidMessage && reach < MID_MESSAGE_PAGE_REACH;
  }

  /** One more page above the window, from the durable record. The page joins
   *  the window at a turn start or re-folds the segment it cut (see
   *  `TranscriptWindow`); the store hears `history_loaded`, which it reconciles
   *  without counting the older turns as echoes of anything it sent. */
  async loadOlderTurns(chatId: string): Promise<boolean> {
    const history = this.transcriptHistory(chatId);
    const window = this.windowFor(chatId);
    const before = window.oldestSeq;
    if (!history.hasOlder || history.loading) return false;
    if (before === null) {
      // Nothing to page from. Saying so once is what stops the surface asking
      // again on every render for a request that could only no-op.
      this.history.set(chatId, { hasOlder: false, loading: false });
      return false;
    }
    this.history.set(chatId, { hasOlder: true, loading: true });
    try {
      const page = await this.rest.listMessages(chatId, { before, limit: this.pageRows });
      const { folded, liveRefolded } = window.prependOlder(windowRows(page.items));
      this.history.set(chatId, { hasOlder: page.has_older ?? false, loading: false });
      this.noteCut(chatId, page);
      // A re-folded live segment lost the settle the cold open applied; the
      // same rule re-applies (and still yields to a machine that is working).
      if (liveRefolded) this.settleReplay(chatId);
      if (folded > 0) this.announce(chatId, false, "history_loaded");
      return folded > 0;
    } catch (err) {
      this.history.set(chatId, { hasOlder: true, loading: false });
      throw err;
    }
  }

  /** The reader is back at the bottom: pages past the budget are let go, and
   *  the durable record serves them again on the next scroll up. */
  releaseOlderTurns(chatId: string): void {
    const window = this.windows.get(chatId);
    if (!window) return;
    const { released, liveRefolded } = window.releaseHead(TRANSCRIPT_WINDOW_MAX_ROWS);
    if (released === 0) return;
    this.history.set(chatId, { hasOlder: true, loading: false });
    // Only a CUT live segment lost the settle the cold open applied to it — a
    // head-segment drop never touched the live fold. The same rule re-applies,
    // and still yields to a machine that is working.
    if (liveRefolded) this.settleReplay(chatId);
    this.announce(chatId, false, "history_loaded");
  }

  /** What the machine says about this chat's turn, as of the last document
   *  frame. Read by the stall watchdog on its own timer, so a turn the box is
   *  still working on is never declared dead for being quiet. */
  turnState(chatId: string): TurnState | null {
    // The document's word, as the box or the server last wrote it. The server
    // ends a turn no machine is left to finish (an ending, a refusal, a stamp
    // gone silent) by writing `idle` here itself, so nothing on this side
    // decides a turn is over from what the compute plane says.
    //
    // Before the document has said anything — a cold open whose socket has not
    // answered yet — the durable record speaks for it: the session status the
    // box last wrote to the transcript. A reopened chat whose box is mid-turn
    // is then read as working from its first paint, never as a settled chat.
    const said = this.turns.get(chatId) ?? (this.windows.get(chatId)?.liveState.sessionWorking ? "working" : null);
    return said;
  }

  /** The turn HALT this chat last reported, by the id of the event that carried
   *  it, or null while the machine has reported none.
   *
   *  A chat the cloud serves has as many readers as it has people, and only one
   *  of them pressed Stop. The others learn it here and nowhere else: their tab
   *  sees the turn go from working to idle, which is what an ordinary end looks
   *  like too — and an ordinary end is what releases the words a composer is
   *  holding. The halt has to be told apart from the end, or a Stop in one tab
   *  is what sends the message another tab was holding behind it. The value
   *  changes once per stop, so a reader acts on it once. */
  stoppedTurn(chatId: string): string | null {
    return this.stops.get(chatId) ?? null;
  }

  subscribeChat(chatId: string, onEvent: (event: ChatEvent) => void): Unsubscribe {
    const set = this.subscribers.get(chatId) ?? new Set<(event: ChatEvent) => void>();
    set.add(onEvent);
    this.subscribers.set(chatId, set);
    if (set.size === 1) this.openLive(chatId);
    return () => {
      set.delete(onEvent);
      if (set.size > 0) return;
      this.subscribers.delete(chatId);
      this.live.get(chatId)?.();
      this.live.delete(chatId);
    };
  }

  // --- the chat list's derived state ---------------------------------------

  getChatActivity(): Record<string, ChatActivity> {
    // The cloud list carries its own freshness on every row, so this session
    // folds no separate activity for it.
    return {};
  }

  getSubagentChats(): Chat[] {
    return [];
  }

  getSubagentLabels(): Record<string, string> {
    return {};
  }

  // --- interrupts ----------------------------------------------------------
  //
  // An ask is raised by the harness on the machine and is waiting THERE, so it
  // is answered by relaying the answer to the publisher — `POST
  // /chats/{id}/answer`, which records nothing and puts the answer on the
  // chat's channel. The machine checks that the ask is still outstanding, that
  // the option is one the ask offered, and that no answer approves a write on
  // its read-only session; the resolution enters the transcript when the
  // harness settles it, exactly as it does for the editor.

  async resolvePermission(chatId: string, requestId: string, optionId: string): Promise<void> {
    await this.rest.answerInterrupt(chatId, {
      interrupt_id: requestId,
      option_id: optionId,
      reject: false,
    });
  }

  async answerQuestion(
    chatId: string,
    requestId: string,
    answers: string[][],
    note?: string | null,
  ): Promise<void> {
    await this.rest.answerInterrupt(chatId, {
      interrupt_id: requestId,
      answers,
      reject: false,
      ...(note ? { note } : {}),
    });
  }

  async rejectQuestion(chatId: string, requestId: string, reason: string | null): Promise<void> {
    await this.rest.answerInterrupt(chatId, { interrupt_id: requestId, reject: true, reason });
  }

  /** Stop is a relay like everything else the portal sends: the server records
   *  who pressed it and puts it on the chat's channel, and the box running the
   *  turn cancels it. There is nothing to wait for and nothing to poll, so the
   *  outcome is decided by whether the post landed.
   *
   *  A post that did not land is reported as a turn still running, never as a
   *  stop: a reader told "stopped" by a Stop that never reached the server
   *  would sit watching an answer nobody ended.
   *
   *  A post the SERVER answered is a different failure and is raised, not folded
   *  into that sentence. The stop route is gated on send, so a reader watching a
   *  colleague's live turn gets a 403 — and "the stop did not reach the
   *  workspace" is then simply false: it reached it and was refused. Only a
   *  failure with no answer behind it (the browser offline, the request never
   *  sent) keeps the sentence, which is the one case it describes. */
  async cancelTurn(chatId: string): Promise<CancelOutcome> {
    try {
      await this.rest.stopTurn(chatId);
      return { stopped: true };
    } catch (err) {
      if (err instanceof ApiError) throw err;
      return {
        stopped: false,
        reason: "The stop did not reach the machine. The turn is still running.",
      };
    }
  }

  /** Whether an Allow on this ask would be acted on, in the server's words.
   *
   *  A read the box always runs; a write is decided by the chat's stance, and
   *  that verdict is the server's, carried on the chat row
   *  (`approval_refusal`). This source keeps no stance table of its own. */
  mayAllow(chatId: string, part: PermissionConversationPart): ApprovalVerdict {
    return authorizesAWrite(part) ? this.writeVerdict(chatId) : APPROVAL_REACHES;
  }

  /** The row's verdict on a write in this chat, or {@link APPROVAL_PENDING}
   *  while there is none to trust: before the row is read, while it was read
   *  in a stance the chat has since left (a fresh read is on its way), or from
   *  a server that does not say. Never the read-only floor's words over a chat
   *  in another stance, and never an Allow nobody vouched for. */
  private writeVerdict(chatId: string): ApprovalVerdict {
    const row = queryClient.getQueryData<ChatSessionRead>(keys.chats.one(chatId));
    const live = this.modes.get(chatId);
    if (row === undefined || row.approval_refusal === undefined) return APPROVAL_PENDING;
    if (live !== undefined && row.permission_mode !== live) return APPROVAL_PENDING;
    const refusal = row.approval_refusal;
    return refusal === null || refusal === "" ? APPROVAL_REACHES : { allowed: false, refusal };
  }

  // --- engine options ------------------------------------------------------

  /** The models this reader may start a chat on.
   *
   *  The portal's route proxies the gateway's own catalog, so the browser's
   *  picker offers exactly what the CLI's does — the same entitlement, the same
   *  BYOK filter, the same fixture families dropped. A catalog that cannot be
   *  read comes back EMPTY rather than throwing, which is the state the
   *  composer already polls out of. */
  async listModels(): Promise<ModelInfo[]> {
    const page = await this.rest.chatModels();
    return (page.items ?? []).map((model) => ({
      id: model.id,
      displayName: model.display_name,
      wire: model.wire,
      efforts: model.efforts ?? [],
      defaultEffort: model.default_effort ?? null,
    }));
  }

  /** The saved Default Chat Model + Effort a new chat seeds from.
   *
   *  Resolved on the SERVER, against the same catalog, by the same rules the
   *  CLI and the editor obey — including the one that matters: a gateway outage
   *  returns the saved values untouched instead of resetting them to whatever
   *  happens to be first. Doing it here would have put that rule in a third
   *  place. */
  async resolveChatDefaults(): Promise<ChatDefaults> {
    const defaults = await this.rest.chatDefaults();
    return {
      model: defaults.model ?? null,
      effort: defaults.effort ?? null,
      permissionMode: defaults.permission_mode ?? null,
    };
  }

  /** Change an open chat's effort: the same pin and route as a model switch,
   *  on the model the chat is already on (never refused on reasoning grounds).
   *  The box runs the next turn at it. */
  async setEffort(chatId: string, effort: string): Promise<void> {
    const modelId = this.chats.find((c) => c.id === chatId)?.model?.id;
    if (!modelId) throw new Error("This chat has no model to set an effort on.");
    await this.setModel(chatId, modelId, effort, modelId);
  }

  /** Move this chat onto `model`.
   *
   *  The chat ROW is the model's home, the same way it is the permission mode's:
   *  the box reads it when it opens the session, so a reload and a resume both
   *  come back on what the reader picked instead of relabelling their transcript
   *  with the workspace default. The route also relays the switch, so a box
   *  already running the chat answers the NEXT turn on the new model.
   *
   *  It answers with the chat AS STORED, so the pin cached here is what the
   *  server accepted — a model this workspace cannot run is refused there with a
   *  422 and nothing local moves. */
  async setModel(
    chatId: string,
    model: string,
    effort?: string | null,
    expectedModelId?: string | null,
  ): Promise<void> {
    const chat = await this.rest.setChatModel(chatId, model, effort, expectedModelId);
    this.chats = this.chats.map((c) => (c.id === chatId ? chatOf(chat) : c));
  }

  /** The open chat's picker, as the server decides it: every model with its
   *  verdict. The server is the one authority on the rule, so nothing here
   *  re-derives which switch would lose reasoning. */
  async modelOptions(chatId: string): Promise<ModelOptions> {
    const read = await this.rest.chatModelOptions(chatId);
    return {
      currentModelId: read.current.model_id ?? null,
      currentEffort: read.current.effort ?? null,
      canSwitch: read.can_switch,
      applies: read.applies ?? "next_turn",
      billedToOwner: read.billed_to_owner ?? false,
      options: (read.options ?? []).map((option) => ({
        model: {
          id: option.model.id,
          displayName: option.model.display_name,
          wire: option.model.wire,
          efforts: option.model.efforts ?? [],
          defaultEffort: option.model.default_effort ?? null,
        },
        state: option.state,
        reasonCode: option.reason_code ?? null,
        message: option.message ?? null,
        groupMessage: option.group_message ?? null,
        escapeNewChatModel: option.escape_new_chat_model ?? null,
      })),
    };
  }

  /** The mode this chat's session is opened in.
   *
   *  The chat row is the authority — the box reads the same field when it opens
   *  the session — so this is a read of the row, not of a local guess. Through
   *  the cache the page reads the row from, under the row's own key: the chat
   *  is ONE request however many things on the page stand on it, and a refusal
   *  is answered once instead of once per asker. */
  async getPermissionMode(chatId: string): Promise<string | null> {
    const chat = await queryClient.ensureQueryData({
      queryKey: keys.chats.one(chatId),
      queryFn: () => this.rest.getChat(chatId),
    });
    this.noteMode(chatId, chat.permission_mode);
    return chat.permission_mode ?? null;
  }

  /** Put this chat's session into `mode`.
   *
   *  The route persists it on the chat and relays it to the machine, and it
   *  answers with the chat AS STORED — so what lands here is what the server
   *  accepted, never the caller's optimistic guess. A mode the product does not
   *  offer is refused there with a 422 rather than quietly downgraded. */
  async setPermissionMode(chatId: string, mode: PermissionMode | string): Promise<void> {
    const chat = await this.rest.setPermissionMode(chatId, mode as ChatPermissionMode);
    // The row as stored, into the slot every reader of the row reads — the
    // route answered with it, so nothing on the page has to ask again to see
    // the mode it was just put in.
    queryClient.setQueryData(keys.chats.one(chatId), chat);
    this.noteMode(chatId, chat.permission_mode);
  }

  /** Follow this chat's mode as the machine republishes it.
   *
   *  The publisher puts the live mode on the chat document's meta, the same
   *  lane `turn_state` rides, so a mode a plan approval flipped on the box
   *  reaches the pill without a poll. A switch made HERE notifies immediately —
   *  the server has already accepted it by then, so the pill never shows a mode
   *  the chat is not in. */
  subscribePermissionMode(chatId: string, onMode: (mode: string) => void): Unsubscribe {
    const set = this.modeWatchers.get(chatId) ?? new Set<(mode: string) => void>();
    set.add(onMode);
    this.modeWatchers.set(chatId, set);
    return () => {
      set.delete(onMode);
      if (set.size === 0) this.modeWatchers.delete(chatId);
    };
  }

  /** Record the chat's mode and tell whoever is watching it.
   *
   *  What arrives is a string off the chat row or the document's meta — the
   *  server's word, not this client's — so a build older than the server, a
   *  publisher writing a stance nobody here has heard of, and a malformed row
   *  all reach this line. A word that is not one of the stances this client
   *  knows is recorded as the FLOOR rather than kept: the record is what
   *  decides whether an approval is offered, and a stance nobody can act on is
   *  not a reason to go on offering one — least of all after a chat that was in
   *  `bypass` is moved to a word this build cannot read.
   *
   *  The floor is what the watchers hear too. A pill naming a stance the client
   *  cannot explain, over a chat it is about to treat as read-only, is the
   *  disagreement this whole seam exists to prevent. */
  private noteMode(chatId: string, mode: string | undefined | null): void {
    if (typeof mode !== "string" || mode === "") return;
    const known = isCloudPermissionMode(mode) ? mode : CLOUD_PERMISSION_MODE;
    if (this.modes.get(chatId) === known) return;
    this.modes.set(chatId, known);
    // The row's verdict was for the stance it was read in: a chat that moved
    // reads its row again, and the card waits for the server's new word.
    const row = queryClient.getQueryData<ChatSessionRead>(keys.chats.one(chatId));
    if (row !== undefined && row.permission_mode !== known) {
      void queryClient.invalidateQueries({ queryKey: keys.chats.one(chatId) });
    }
    this.modeWatchers.get(chatId)?.forEach((listener) => listener(known));
  }

  async listCommands(): Promise<SlashCommandInfo[]> {
    return [];
  }

  async runCommand(): Promise<SlashCommandOutcome> {
    return {
      kind: "cli_only",
      command: null,
      payload: {},
      // Says what is true here rather than naming a surface this reader does
      // not have: a browser reader has no editor to be sent to.
      message: "Slash commands run where the agent runs. Ask for it in plain language instead.",
    };
  }

  // --- the activity dock ---------------------------------------------------

  async getCostState(): Promise<CostStateView> {
    return { spent: {}, caps: {}, orgManaged: false, unknownKeys: [] };
  }

  async setCostLimits(): Promise<CostStateView> {
    return this.getCostState();
  }

  async listDecisions(): Promise<{ decisions: DecisionView[]; hasMore: boolean }> {
    return { decisions: [], hasMore: false };
  }

  async listCostLedger(): Promise<{ entries: CostLedgerEntryView[]; hasMore: boolean }> {
    return { entries: [], hasMore: false };
  }

  async getUsage(): Promise<{ credits: Record<string, unknown>; usage: Record<string, unknown> }> {
    return { credits: {}, usage: {} };
  }

  // --- references ----------------------------------------------------------

  async fetchBlob(): Promise<BlobPage> {
    // A blob lives on the machine that produced it; in the browser a result is
    // reached by promoting it, which is what the result card offers.
    // The sentence is shown as the page's own copy, so it is written for the
    // reader: capitalised, and naming where the result is and how to open it.
    throw new Error(BLOB_NOT_SAVED);
  }

  async searchFiles(): Promise<FileSearchResult[]> {
    return [];
  }

  // --- header counts -------------------------------------------------------

  async listContext(): Promise<{ total: number }> {
    return { total: 0 };
  }

  async lineageRoots(): Promise<{ total_nodes?: number }> {
    return {};
  }

  // --- the shared composer draft -------------------------------------------

  /** The chat's draft on the live lane. The socket is held for as long as the
   *  draft is, so the live document never outlives the connection it rides. */
  openLiveDraft(chatId: string): { draft: LiveDraft; release: () => void } {
    const releaseSocket = acquireRealtimeClient();
    const held = acquireLiveDraft(chatId, { socket: getRealtimeClient(), hueOf, account: this.account() });
    return {
      draft: held.draft,
      release: () => {
        held.release();
        releaseSocket();
      },
    };
  }

  // --- internals -----------------------------------------------------------

  private windowFor(chatId: string): TranscriptWindow<CloudRow> {
    const existing = this.windows.get(chatId);
    if (existing) return existing;
    const fresh = new TranscriptWindow(cloudWindowAdapter);
    this.windows.set(chatId, fresh);
    return fresh;
  }

  /** The live segment's fold: what the socket and the reader's own sends
   *  write into. Never held across an await — a page that cuts into the live
   *  segment replaces it. */
  private stateFor(chatId: string): ConversationFoldState {
    return this.windowFor(chatId).liveState;
  }

  /** Record what a document frame said about the turn, and answer whether that
   *  MOVED it. A frame that carries no turn state leaves the last word
   *  standing: the meta is merged server-side, so an unrelated key must not
   *  read as "the turn ended". The box repeats the same word on its heartbeat,
   *  which moves nothing. */
  private noteTurnState(chatId: string, meta: unknown): boolean {
    const state = turnStateOf(meta);
    if (state === null) return false;
    const before = this.turns.get(chatId);
    this.turns.set(chatId, state);
    return before !== state;
  }

  /** Record the newest turn HALT a batch of rows carries, and answer whether it
   *  is one this chat had not already reported. A stop met a second time — the
   *  re-hello that replays the window it sits in, the page that reads it back
   *  off the record — is the same stop and moves nothing. */
  private noteStops(chatId: string, rows: readonly CloudRow[]): boolean {
    let id: string | null = null;
    for (const row of rows) {
      const event = eventOf(row);
      if (event === null) continue;
      id = stopIdOf(event, row.eventId) ?? id;
    }
    if (id === null || this.stops.get(chatId) === id) return false;
    this.stops.set(chatId, id);
    return true;
  }

  /** The permission mode a document frame reported, if it reported one.
   *
   *  The publisher stamps the LIVE mode of the session it is running — which is
   *  the only way a mode the box itself flipped (a plan approval accepted on
   *  the machine) reaches the pill. A frame that says nothing about the mode
   *  leaves the last word standing: the meta is merged server-side, so an
   *  unrelated key must not read as a mode change. */
  private noteMetaMode(chatId: string, meta: unknown): void {
    if (!isRecord(meta)) return;
    const mode = meta.permission_mode;
    if (typeof mode === "string") this.noteMode(chatId, mode);
  }

  /** Read from the durable record: on a cold open the NEWEST page — the tail
   *  the chat opens on, with everything above it left for `loadOlderTurns` —
   *  and from then on forward from wherever this chat's cursor stands. One read
   *  runs per chat at a time; a second caller shares it.
   *
   *  A forward page that answers `resync_from` means our cursor fell out of
   *  the retained window: the window is discarded and rebuilt from the
   *  sequence the server offers, which is the reset path taken in-band rather
   *  than as an error. `resync_from` names a message that EXISTS — "start again
   *  from HERE", the oldest one still held — while `afterSeq` is exclusive, so
   *  the read resumes one below it. */
  private catchUp(chatId: string, opts: { settle?: boolean } = {}): Promise<void> {
    const running = this.catching.get(chatId);
    if (running) return running;
    const read = this.readForward(chatId, opts).finally(() => this.catching.delete(chatId));
    this.catching.set(chatId, read);
    return read;
  }

  /** Ask for a catch-up that is ALLOWED to fail — the two triggers that are
   *  nobody's awaited call: a relay saying the server just recorded a row this
   *  tab can only get by reading, and a socket snapshot, whose retained window
   *  may have compacted away an append made while the socket was down.
   *
   *  Neither caller has anywhere to put a rejection, and a forward read refused
   *  by the rate limiter would leave the tape short until the next relay or
   *  reconnect, which inside a long streaming turn may never come. So the
   *  failure is owned here: retried on a bounded ladder, and when the ladder
   *  runs out, said out loud. */
  private wantCatchUp(chatId: string, opts: { settle?: boolean } = {}): void {
    const settle = opts.settle ?? true;
    if (this.catching.has(chatId) || this.catchUpTimer.has(chatId)) {
      const pending = this.catchUpPending.get(chatId);
      // A caller that says NOT to settle wins over one that says nothing:
      // settling retires what a replay left open, and a live relay — an ask the
      // machine is still holding — may never be the reason that happens.
      this.catchUpPending.set(chatId, { settle: (pending?.settle ?? true) && settle });
      return;
    }
    void this.driveCatchUp(chatId, settle, 1);
  }

  private async driveCatchUp(chatId: string, settle: boolean, attempt: number): Promise<void> {
    try {
      await this.catchUp(chatId, { settle });
    } catch (err) {
      if (attempt >= CATCH_UP_RETRY_ATTEMPTS || !worthAnotherRead(err)) {
        this.catchUpPending.delete(chatId);
        this.stopClaimingCurrent(chatId);
        return;
      }
      const timer = setTimeout(() => {
        this.catchUpTimer.delete(chatId);
        const pending = this.catchUpPending.get(chatId);
        this.catchUpPending.delete(chatId);
        void this.driveCatchUp(chatId, pending ? pending.settle && settle : settle, attempt + 1);
      }, catchUpDelayMs(attempt, err, this.random));
      this.catchUpTimer.set(chatId, timer);
      return;
    }
    // A trigger that arrived while this read was in flight asked about rows it
    // may have been too late to cover; the ladder starts again from the top
    // because the server just answered.
    const pending = this.catchUpPending.get(chatId);
    if (pending === undefined) return;
    this.catchUpPending.delete(chatId);
    void this.driveCatchUp(chatId, pending.settle, 1);
  }

  /** The source stops standing behind what it has read of this chat.
   *
   *  The ladder ran out, or the server's answer was a settled one: the window
   *  holds whatever whole pages got through and the cursor names exactly how
   *  far that was. Both go, TOGETHER, exactly as the in-band `resync_from`
   *  reset drops them together — and that pairing is the whole point. Dropping
   *  the cursor alone makes the next trigger read the durable TAIL, whose floor
   *  can sit far above the rows the window already holds; the rows in between
   *  are then never read, and the fresh cursor says the chat is current, so
   *  nothing will ever read them. That is a hole in the MIDDLE of the tape,
   *  which is worse than the short one this whole change removes: a short tape
   *  closes on the next forward read, a hole never does.
   *
   *  So the chat goes back to being unread. The next trigger is a cold open —
   *  the tail, with everything above it reachable through `loadOlderTurns`,
   *  which is exactly what a reader who reloaded would see. The replay
   *  announcement is the same word a reconnect gives the surfaces, so the tape
   *  is re-derived rather than left quietly wrong. Nothing is invented: no
   *  turn, no status and no terminal state is written by this path. */
  private stopClaimingCurrent(chatId: string): void {
    this.windows.get(chatId)?.reset();
    this.cursor.delete(chatId);
    this.history.delete(chatId);
    this.messageCutBelow.delete(chatId);
    this.announce(chatId, true);
  }

  private async readForward(chatId: string, opts: { settle?: boolean }): Promise<void> {
    const progress = { folded: 0 };
    try {
      await this.readPages(chatId, progress);
    } catch (err) {
      // A read cut short still folded whole pages, and the cursor now names the
      // last of them. Announcing them is the difference between a tab one page
      // behind and a tab showing nothing of the pages it did read. The settle
      // is NOT run: retiring a replay's open parts off a read that never
      // reached the end would close an ask the record has not finished
      // describing — exactly the client-invented terminal state the chat state
      // model forbids.
      if (progress.folded > 0) this.announce(chatId, true);
      throw err;
    }
    // On a re-hello the machine is still there and its asks are still live;
    // only the cold open is reading a transcript nobody is behind any more.
    if (opts.settle ?? true) this.settleReplay(chatId);
    if (progress.folded > 0) this.announce(chatId, true);
  }

  private async readPages(chatId: string, progress: { folded: number }): Promise<void> {
    let after = this.cursor.get(chatId) ?? 0;
    if (!this.cursor.has(chatId)) {
      const tail = await this.rest.listMessages(chatId, { tail: true, limit: this.pageRows });
      const resyncFrom = tail.resync_from ?? null;
      // A marker naming the very first message is not a reset (the forward
      // read's own rule, with the cursor at zero).
      if (resyncFrom !== null && resyncFrom > 1) {
        // The same in-band reset the forward read honours: start again from
        // the oldest message the server still holds, read forward from there.
        this.windows.get(chatId)?.reset();
        this.history.delete(chatId);
        after = Math.max(resyncFrom - 1, 0);
        this.cursor.set(chatId, after);
      } else {
        const window = this.windowFor(chatId);
        // The socket's snapshot may have folded the newest entries already; the
        // rows below its reach are the page above, and join as one.
        const lo = window.oldestSeq ?? Infinity;
        const rows = windowRows(tail.items);
        // A stop the record already holds is where this chat STANDS, not news
        // that just arrived: read it here so the first look a reader takes at
        // the chat starts from it, and a later reading of the same stop cannot
        // cancel what they typed after opening.
        this.noteStops(chatId, rows);
        const below = window.prependOlder(rows.filter((row) => row.kind === "row" && row.seq < lo));
        progress.folded += below.folded;
        // The rows the snapshot's reach covers fold in the SERVER's order: a
        // prompt rides the relay lane and is never in the window, so the page
        // is what puts it back between the rows already folded.
        const within = window.mergeLive(rows.filter((row) => row.kind === "row" && row.seq >= lo));
        progress.folded += within.folded;
        // A page that cut into the segment the snapshot opened re-folded it,
        // and the settle that snapshot received went with the old state; the
        // same rule re-applies (and still yields to a machine that is working).
        if (below.liveRefolded || within.liveRefolded) this.settleReplay(chatId);
        this.history.set(chatId, { hasOlder: tail.has_older ?? false, loading: false });
        after = tail.next_after_seq;
        // The server anchors the tail on a person's message, and a message
        // sent while the machine was still writing (a Stop, then the next
        // words before the stopped turn's last rows landed) sits INSIDE the
        // message above: the page then carries that message's end and none of
        // what it said. The page below completes it; read it now rather than
        // show an answer's last line as the whole answer until someone
        // scrolls. Bounded: a message longer than a few pages stays cut, as a
        // turn the server says is cut does.
        this.noteCut(chatId, tail);
        // The tail is folded, so the cursor stands behind it BEFORE anything
        // else is asked for. A read refused on the first forward page then
        // resumes at the forward page rather than re-reading the tail and
        // re-reaching for the message it cut.
        this.cursor.set(chatId, after);
        for (let reach = 0; this.readsOnForAMessage(chatId, reach); reach += 1) {
          if (!(await this.loadOlderTurns(chatId))) break;
        }
      }
    }
    for (;;) {
      const page = await this.rest.listMessages(chatId, { afterSeq: after });
      // The server sends the marker only when there is one; absent and null both
      // mean the cursor is still inside the retained window.
      const resyncFrom = page.resync_from ?? null;
      if (resyncFrom !== null && resyncFrom > after + 1) {
        const resume = Math.max(resyncFrom - 1, 0);
        this.windows.get(chatId)?.reset();
        this.history.delete(chatId);
        this.cursor.set(chatId, resume);
        after = resume;
        progress.folded = 0;
        continue;
      }
      progress.folded += this.applyMessages(chatId, page.items, { quiet: true });
      const done = page.items.length === 0 || page.next_after_seq <= after;
      if (!done) after = page.next_after_seq;
      // EVERY page that folded is recorded on the cursor before the next one is
      // asked for — the chat has now been read this far, which is what the
      // cursor means, and it is recorded even when the read returned nothing or
      // an empty chat would re-page REST on every look. Writing it only after
      // the last page is what made a refusal halfway so expensive: the pages
      // above were already in the window while the cursor still said the read
      // had not started, so the resume — when one finally came — re-read them
      // all. The fold drops a row whose event id it has already seen, so
      // re-reading a page is only a wasted request; not writing the cursor is a
      // window whose contents nothing accounts for.
      this.cursor.set(chatId, after);
      if (done) break;
    }
  }

  /** Retire what a REPLAY left dangling, exactly as the daemon source does over
   *  an open-chat replay (`settleStaleInterrupts`).
   *
   *  A tool call still `running`, or a `permission.request` nobody announced
   *  as a person's, is what the transcript held when the publisher stopped
   *  writing — a mirror killed mid-turn, a policy that answered before the box
   *  said so. Nothing in the machine is waiting on those any more, and the
   *  surface puts a pending permission card WHERE THE COMPOSER GOES: left
   *  alone, a dead ask is a chat the reader can neither answer nor type in. So
   *  the replay's leftovers are settled (the tool reads as failed, the ask as
   *  resolved) the moment the replay finishes.
   *
   *  The one thing a replay never retires is an ask the box announced as a
   *  person's (`prompting`) or a question, left unresolved: that is durable
   *  state of the chat, and the box that opens the chat next re-offers the
   *  same ask under the same id and resolves the answer — so a reload mid-ask
   *  keeps its card, and the answer goes out exactly as a live one does.
   *
   *  The document's word is believed, and only it: a turn may legitimately run
   *  for hours or days — reading a file, waiting on a warehouse, thinking — and
   *  the age of the stamp says nothing about whether it is still going. A
   *  five-minute floor here meant a three-hour turn read as CRASHED to anyone
   *  who reloaded after minute five. What retires it is `turn_state: idle`,
   *  which the box writes when it ends a turn and the server writes when no
   *  box is left to (an ending, a refusal, a stamp gone silent). */
  private settleReplay(chatId: string): void {
    // …only on the server's word (`replayVerdict`). A second reader joining
    // mid-answer — or the first one reloading — gets a snapshot that is a
    // replay by shape but live by content, and the very same frame carries
    // `turn_state: working`: nothing is retired. A durable read that lands
    // BEFORE that word (the REST page raced the socket) knows nothing, and
    // retires nothing either — settling on "no word yet" was what marked a
    // running write failed on every reload mid-turn. Only `idle` closes what
    // the transcript left open — and never a clock: a box that
    // DIED holding `working` is ended by the server, which says `idle`.
    const verdict = replayVerdict(this.turns.get(chatId) ?? null);
    if (verdict === "keep") return;
    const window = this.windows.get(chatId);
    // An ask the machine announced as a person's to answer is kept pending
    // whatever the stamp says: it is durable state of the chat, and the box
    // that opens the chat next re-offers the same ask and resolves the answer.
    // Retiring it on a cold open was what orphaned an ask raised mid-reload.
    if (window) settleStaleInterrupts(window.liveState, { keepAnswerableAsks: true });
  }

  /** Fold a page of REST messages into the live segment; returns how many were
   *  new. `quiet` leaves the announcement to the caller, so a replay says "the
   *  conversation moved" once, after it has been reconciled — never with a
   *  dead ask still on screen.
   *
   *  A row's `payload` is the transcript ENTRY the machine published —
   *  `{event_id, role, kind, payload}` — so the harness event is one level in,
   *  exactly as it is on the document (`applyDocEvents` unwraps it there).
   *  Folded flat, a reopened chat shows nothing the machine said. A payload
   *  that already names its `event_type` IS the event (the reader's own prompt
   *  row, and any entry written flat). The window's adapter reads both. */
  private applyMessages(
    chatId: string,
    messages: ChatMessageRead[],
    opts: { replay?: boolean; quiet?: boolean } = {},
  ): number {
    const rows = windowRows(messages);
    const stopped = this.noteStops(chatId, rows);
    const folded = this.windowFor(chatId).appendLive(rows);
    if ((folded > 0 || stopped) && !opts.quiet) this.announce(chatId, opts.replay ?? false);
    return folded;
  }

  /** Fold every transcript entry we have not folded yet. Ephemeral entries (the
   *  token chunks) carry no id to de-duplicate by and are always folded. An
   *  entry that names a sequence below the window is one an older page will
   *  bring, in order — the window drops it here. */
  private applyDocEvents(
    chatId: string,
    entries: unknown[],
    opts: { ephemeral: boolean; replay?: boolean },
  ): void {
    const rows: CloudRow[] = [];
    for (const entry of entries) {
      const event = harnessEventOf(entry) ?? (isRecord(entry) ? entry : null);
      if (event === null) continue;
      const id = !opts.ephemeral && isRecord(entry) ? entry.event_id : undefined;
      const seq = isRecord(entry) && typeof entry.seq === "number" ? entry.seq : null;
      const role = isRecord(entry) && typeof entry.role === "string" ? entry.role : null;
      rows.push({
        kind: "doc",
        seq: opts.ephemeral ? null : seq,
        // A token frame is not in the durable record and never will be: the
        // part the publisher writes when the text settles is. Saying so is
        // what lets the window let one go — a row it could not drop, sitting
        // above the rows a turn settles into, holds the whole window open.
        ephemeral: opts.ephemeral,
        eventId: typeof id === "string" && id !== "" ? id : null,
        role,
        event,
      });
    }
    // Read BEFORE the fold, so a stop is noticed whether or not the frame that
    // carries it also brings a row nobody had seen.
    const stopped = this.noteStops(chatId, rows);
    const folded = this.windowFor(chatId).appendLive(rows);
    // The snapshot is a replay of the retained window — the same reconciliation
    // the daemon runs after a reconnect. Live ops are not: an ask that arrives
    // on an open socket is outstanding on the machine and is the one the reader
    // answers.
    if (opts.replay) this.settleReplay(chatId);
    if (folded > 0 || stopped) this.announce(chatId, opts.replay ?? false);
  }

  /** Apply what a `user_message` relay carries: a person's message, or a
   *  permission mode a person switched the chat into.
   *
   *  A message relay names the entry the server recorded, so the sender's own
   *  tab — which folds the send's response under the same id — and a later
   *  durable read both find the turn already standing; only a message this
   *  reader has not seen moves the conversation. A mode relay is minted after
   *  the route recorded the switch on the chat, so it is the stored mode, not
   *  a guess — and it is the only live word another reader's pill (and the
   *  Allow it may offer on an ask) gets about a switch made elsewhere. */
  private applyRelays(chatId: string, events: unknown[]): void {
    const state = this.stateFor(chatId);
    let folded = 0;
    // The server writes rows of its own that no box publishes onto the
    // document — the note a Stop leaves, the record of a member's answer with
    // the member's name — and says so on this lane only by the relay it sends
    // the box. A tab that heard the relay reads forward to fold those rows in
    // their order; nothing here invents them.
    let recorded = false;
    for (const event of events) {
      if (isRecord(event) && event.kind === MODE_RELAY_KIND) {
        if (typeof event.mode === "string") this.noteMode(chatId, event.mode);
        continue;
      }
      if (isRecord(event) && event.kind === STOP_RELAY_KIND) {
        recorded = true;
        continue;
      }
      const answer = relayedAnswerOf(event);
      if (answer !== null) {
        foldRelayedAnswer(state, answer);
        recorded = true;
        folded += 1;
        continue;
      }
      const prompt = relayedPromptOf(event);
      if (prompt === null) continue;
      const known = state.messageToTurn.size;
      foldRelayedPrompt(state, prompt);
      if (state.messageToTurn.size > known) folded += 1;
    }
    if (folded > 0) this.announce(chatId, false);
    if (recorded) this.wantCatchUp(chatId, { settle: false });
  }

  /** Tell every subscriber the conversation moved. The event carries no content:
   *  the reader re-reads the folded turns, exactly as it does in the editor. */
  private announce(
    chatId: string,
    replay: boolean,
    kind: "graph_changed" | "history_loaded" = "graph_changed",
  ): void {
    this.seq += 1;
    const event: ChatEvent = {
      id: `evt-${this.seq}`,
      chatId,
      kind,
      content: kind === "history_loaded" ? "older turns loaded" : "conversation updated",
      replay,
    };
    this.subscribers.get(chatId)?.forEach((listener) => listener(event));
  }

  private openLive(chatId: string): void {
    const release = this.acquire();
    const handle = this.open(chatId);
    // Only the FIRST snapshot is a replay. Every later one is a re-hello on a
    // living machine — a pong timeout, the session deadline, a backend restart
    // — and the machine is still holding the ask the reader was about to
    // answer and still writing into the turn on screen. Settling those retires
    // an ask nobody can raise again and flashes a live turn as cancelled.
    let opened = false;
    const off = handle.onMessage((message) => {
      if (message.kind === "snapshot") {
        const turnMoved = this.noteTurnState(chatId, message.state?.meta);
        this.noteMetaMode(chatId, message.state?.meta);
        const events = Array.isArray(message.state?.events) ? message.state.events : [];
        this.applyDocEvents(chatId, events, { ephemeral: false, replay: !opened });
        opened = true;
        // A reopened socket carries the machine's CURRENT word about the turn,
        // and that word is the only thing allowed to replace the one a reader
        // kept through the gap — a gap in the news is not news. A turn that
        // ENDED while the socket was down moves it, and the frame that says so
        // usually folds nothing: every entry in the retained window is already
        // on screen. So without announcing it here the end of that turn reached
        // the reader only on whatever unrelated event happened along next, and
        // the composer stayed working in the meantime. A frame that says
        // NOTHING about the turn moves nothing, and the retained word stands.
        if (turnMoved) this.announce(chatId, true);
        // A snapshot is what a reconnect delivers, and it carries only the
        // RETAINED window. An append made while the socket was down and since
        // compacted out of that window is simply absent from it, with nothing
        // to say so — a hole in the transcript that only a page reload closed.
        // REST is the durable record, so it is re-read here; the entries the
        // two share are folded once, by event id.
        this.wantCatchUp(chatId, { settle: !opened });
        return;
      }
      if (message.kind === "op") {
        const payload: OpPayload = message.payload;
        // The publisher announces the turn itself on the meta lane — the one
        // frame that says a quiet box is still working rather than gone.
        if (payload.intent === "set_meta") {
          const turnMoved = this.noteTurnState(chatId, payload.meta);
          this.noteMetaMode(chatId, payload.meta);
          // A turn starting and a turn ending are the two moments the composer
          // hangs off, and neither is a transcript event — the publisher
          // announces them here and nowhere else. Without this the end of a
          // turn reached the reader only on whatever event happened to fold
          // next, which after a compaction is the next turn. The stamp the box
          // repeats on its heartbeat says what the last one said and announces
          // nothing.
          if (!turnMoved) return;
          // A turn the server ended (its machine stopped, its box refused the
          // chat, its stamp went silent) leaves whatever the box was streaming
          // open: nothing else will ever close it. `idle` means nothing in the
          // turn is still running, so the fold closes it here, keeping only an
          // ask a person can still answer.
          if (this.turns.get(chatId) === "idle") this.settleReplay(chatId);
          this.announce(chatId, false);
          return;
        }
        // A person spoke — in this tab or another. The server relays every
        // accepted send on this lane the moment it records it, before any box
        // has taken the turn; folded here, a second reader sees the message
        // as it is sent rather than on their next durable read.
        if (payload.intent === "user_message") {
          this.applyRelays(chatId, payload.events ?? []);
          return;
        }
        if (payload.intent !== "append" && payload.intent !== "chunk") return;
        this.applyDocEvents(chatId, payload.events ?? [], { ephemeral: message.ephemeral });
      }
    });
    this.live.set(chatId, () => {
      off();
      handle.dispose();
      release();
      // The turn is NOT forgotten with the socket. What the publisher last said
      // about it is a fact about the MACHINE, not about this connection: a box
      // three seconds into a write is still writing while the tab is closed,
      // and it is still writing when the owner comes back and a new ticket
      // opens a new socket. That re-open is a cold open by shape, so its
      // snapshot is folded as a replay — and a replay with no standing
      // `working` to stop it settles the pending write as FAILED and stamps
      // the running turn `cancelled`, minutes before the write actually
      // returns. The snapshot need not re-state the turn either: the document's
      // meta is merged server-side and the publisher re-stamps it on its own
      // heartbeat, so the frame that opens the socket can carry other keys and
      // nothing about the turn. Dropping the word here is what left nothing to
      // re-establish it from. It is retired by the box's own `idle`, or by the
      // machine's absence — never by a socket closing.
      // An armed retry belongs to a chat somebody was reading. Nobody is now,
      // and the read it would run answers to nothing — so the ladder goes with
      // the socket. Re-opening the chat re-arms it from the first rung.
      const retry = this.catchUpTimer.get(chatId);
      if (retry !== undefined) clearTimeout(retry);
      this.catchUpTimer.delete(chatId);
      this.catchUpPending.delete(chatId);
      this.modes.delete(chatId);
    });
  }
}

