// The convergence oracle: one chat, one ordered server log, many ways to read it
// — and every way must render the same thing.
//
// The server's ordered event log (`chat_messages`, by `seq`) is the single
// source of truth. Everything a tab shows is derived from it: the REST tail page
// a cold open reads, the retained window the socket's snapshot replays, and the
// appends that arrive live afterwards are all VIEWS of that one log. So for
// every chat and every sequence number `n`:
//
//     fold(snapshot(n0) + liveEvents(n0+1..n))  ≡  fold(snapshot(n))
//
// for every `n0 ≤ n` — a tab that opened early and folded live must show what
// a tab that opens fresh at `n` shows, whichever of the REST page and the socket
// snapshot lands first. This module drives the REAL data source
// (`CloudDataSource`: its window, its replay settle, its relay fold) through a
// scripted transport and a scripted document handle, with no socket and no
// fetch, so a test and a dev-only detector can both ask the question.
//
// The rule it checks: the server snapshot plus its ordered events are the only
// source of truth, and a browser that has seen the same events shows the same
// transcript, with no terminal state the server did not send.

import type { ConversationTurn } from "@alkera/chat-model";

import type { DocHandle, DocMessage, OpPayload } from "../../../../api/realtime/docSync";
import type { ChatMessageList, ChatMessageRead } from "../../../../api/cloudChat/transport";

import { CloudDataSource } from "./CloudDataSource";
import type { TurnState } from "./ChatDataSource";
import { conversationAwaitsResponse, pendingAskKind } from "./harnessEventFold";
import { TRANSCRIPT_PAGE_ROWS } from "./transcriptWindow";

/** One row of the server's ordered log — exactly `ChatMessageRead`. */
export type LogRow = ChatMessageRead;

/** What a tab derives from the log at one point: the folded turns plus the two
 *  readings the shell hangs the composer off. */
export interface FoldView {
  turns: ConversationTurn[];
  pendingAsk: ReturnType<typeof pendingAskKind>;
  awaitsResponse: boolean;
  turnState: TurnState | null;
  /** Whether the tab says there are older turns it has not loaded. Not a
   *  reading that is compared: it is what tells a turn the view never loaded
   *  from one it dropped. Absent means the view holds the log from its start. */
  hasOlder?: boolean;
}

/** Which of a cold open's two reads lands first. The page and the socket race
 *  on every real open, and both orders must fold to the same view. */
export type ColdOpenOrder = "rest-first" | "socket-first";

const CHAT_ID = "chat";

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;

/** The rows up to and including `n`, ascending by `seq`. */
export function eventsUpTo(log: readonly LogRow[], n: number): LogRow[] {
  return log.filter((row) => row.seq <= n).sort((a, b) => a.seq - b.seq);
}

/** The publisher's word about the turn as the document would carry it at `n`,
 *  derived from the log itself: the last `session.status_changed` at or below
 *  `n` (`idle` → idle, anything else → working), `null` before the first. The
 *  log is the only thing a fixture holds, and both sides of a comparison are
 *  handed the same word, so the fold — not the word — is what is compared. */
export function turnStateAt(log: readonly LogRow[], n: number): TurnState | null {
  let word: TurnState | null = null;
  for (const row of log) {
    if (row.seq > n || row.kind !== "session.status_changed") continue;
    const event = harnessEvent(row);
    word = event?.status === "idle" ? "idle" : "working";
  }
  return word;
}

/** The harness event a row carries: one level in for a machine's envelope,
 *  flat for a person's prompt. The same unwrapping `CloudDataSource` does. */
function harnessEvent(row: LogRow): Record<string, unknown> | null {
  const payload = row.payload;
  if (!isRecord(payload)) return null;
  if (typeof payload.event_type === "string") return payload;
  return isRecord(payload.payload) ? payload.payload : null;
}

// --- the server's own page shapes, computed from the log -------------------

/** `chat_service.list_messages`: the `limit` rows after `afterSeq`. The log is
 *  never pruned, so `resync_from` is never set. */
export function pageAfter(rows: readonly LogRow[], afterSeq: number, limit: number): ChatMessageList {
  const items = rows.filter((row) => row.seq > afterSeq).slice(0, limit);
  return {
    items,
    next_after_seq: items.length ? items[items.length - 1].seq : afterSeq,
    resync_from: null,
    has_older: false,
    cut: false,
  };
}

/** The server's `chat_page_turn_reach` default: how far below a page the read
 *  reaches for the prompt row its first turn began at, in page limits. */
export const PAGE_TURN_REACH = 4;

/** The server's `chat_page_message_reach` default: how far below a boundary
 *  the read reaches to account for a message the page carries, in page limits.
 *  `null` models a server from before pages were kept outside a message. */
export const PAGE_MESSAGE_REACH = 2;

/** `chat_service.message_id_for_event`: which message a stored row belongs to,
 *  off the row itself — stated flat by an event, on the part by a
 *  `part.created`. For paging EVERY message the transcript names counts,
 *  whoever published it: the machine's answers, the box's echo of the person's
 *  message, the notes the server writes into a turn. Never text, timing or
 *  role. A `prompt` row and the session-level events name none; and a
 *  `prompt.cancelled` row is the one row that states an id it does NOT belong
 *  to — it names the prompt it cancels, which no `message.created` opens. */
export function messageIdOf(row: LogRow): string | null {
  if (row.kind === "prompt.cancelled") return null;
  const payload = row.payload;
  if (!isRecord(payload)) return null;
  const event =
    typeof payload.event_type === "string" ? payload : isRecord(payload.payload) ? payload.payload : payload;
  if (event.event_type === "prompt.cancelled") return null;
  if (typeof event.message_id === "string" && event.message_id !== "") return event.message_id;
  const part = event.part;
  if (isRecord(part) && typeof part.message_id === "string" && part.message_id !== "") {
    return part.message_id;
  }
  return null;
}

/** `chat_service._unaccounted_messages`: the messages `rows` shows part of
 *  without the row that OPENED them. A message is held whole only when the
 *  page holds that row — a gap in its rows cannot fool it. */
export function unaccountedMessages(rows: readonly LogRow[]): Set<string> {
  const carried = new Set<string>();
  const opened = new Set<string>();
  for (const row of rows) {
    const name = messageIdOf(row);
    if (name === null) continue;
    carried.add(name);
    if (row.kind === "message.created") opened.add(name);
  }
  for (const name of opened) carried.delete(name);
  return carried;
}

// --- `alkera_core.objects.transcript_page`, mirrored -----------------------------
//
// The rule is one pure function on the server (`align_boundary`), driven by the
// `chat_messages` pager and by the daemon's local-log pager alike. It is
// mirrored here shape for shape — the same row, the same reach, the same
// answer — so the model is checked against the rule's own cases and not
// against a reading of the service around it.

/** One transcript row, as the page rule needs to see it. */
export interface PageRow {
  seq: number;
  startsTurn: boolean;
  messageId: string | null;
  opensMessage: boolean;
}

/** How far a page may reach down, in multiples of its own limit. */
export interface PageReach {
  limit: number;
  turn: number;
  message: number;
}

/** Rows the boundary may descend below the page it started as. */
export const pageBudget = (reach: PageReach): number =>
  reach.limit * (Math.max(reach.turn, 1) + Math.max(reach.message, 1));

/** Where the page opens, and whether it is still carrying someone's middle
 *  — a message it cannot open, or a turn it opens in the middle of. The
 *  rule's own word, taken verbatim by every caller. */
export interface Boundary {
  seq: number;
  cut: boolean;
}

/** How the server's pager reads a stored row for the rule: a `prompt` starts
 *  a turn, `message.created` opens a message, membership is `messageIdOf`. */
export function pageRowOf(row: LogRow): PageRow {
  return {
    seq: row.seq,
    startsTurn: row.kind === "prompt",
    messageId: messageIdOf(row),
    opensMessage: row.kind === "message.created",
  };
}

/** `_unaccounted`: messages `rows` shows part of without the row that opened them. */
function unaccountedOf(rows: readonly PageRow[]): Set<string> {
  const carried = new Set<string>();
  const opened = new Set<string>();
  for (const row of rows) {
    if (row.messageId === null) continue;
    carried.add(row.messageId);
    if (row.opensMessage) opened.add(row.messageId);
  }
  for (const name of opened) carried.delete(name);
  return carried;
}

/** `_reach_below`: whether a page opening above `below` carries a message it
 *  cannot account for, and the lowest sequence it would have to drop to for
 *  the ones it can still see (null when it cannot see them at all). */
function reachBelow(
  below: readonly PageRow[],
  page: readonly PageRow[],
): { unproved: boolean; target: number | null } {
  const unproved = unaccountedOf(page);
  const carried = new Set<string>();
  for (const row of page) if (row.messageId !== null) carried.add(row.messageId);
  const lowest = new Map<string, number>();
  for (const row of below) {
    if (row.messageId === null || !carried.has(row.messageId)) continue;
    unproved.add(row.messageId);
    if (!lowest.has(row.messageId)) lowest.set(row.messageId, row.seq);
  }
  const reachable = [...unproved].filter((name) => lowest.has(name)).map((name) => lowest.get(name) as number);
  return { unproved: unproved.size > 0, target: reachable.length ? Math.min(...reachable) : null };
}

/** `align_boundary`: where the page whose `limit` rows begin at `pageLow`
 *  should open.
 *
 *  Two reaches run alternately, because neither settles it alone: down to the
 *  row that STARTS the turn, and down past any message the boundary would open
 *  in the middle of. Lowering onto a turn's first row can land inside an
 *  answer; lowering past that answer lands on the box's echo of the person's
 *  message, which begins one row UNDER the row that started the turn. Every
 *  step strictly lowers the boundary, and the descent stops `budget` rows
 *  under the page.
 *
 *  `known` is every row from `max(pageLow − budget, oldest)` up through the
 *  page, ascending — the whole stretch the descent may look at, and the rule
 *  decides on exactly that: a caller holding the entire log and one that read
 *  only the budget open the page in the same place and agree on `cut`. A
 *  message is held whole only when that stretch holds the row that OPENED it
 *  and the budget allowed a look under the page: `message.created` is
 *  re-announced from inside a turn, so when the descent spent its whole
 *  budget arriving, the opening row in the page proves nothing. `oldest` is
 *  the lowest sequence the transcript still holds, so a page that opens on
 *  the very first row is never reported as carrying a middle. The known
 *  exposure the rule documents — one message's rows further apart than the
 *  budget, with a re-announced opening inside the page — is mirrored as is. */
export function alignBoundary(
  known: readonly PageRow[],
  opts: { pageLow: number; oldest?: number; reach: PageReach },
): Boundary {
  const { pageLow, reach } = opts;
  const oldest = opts.oldest ?? 1;
  const turnReach = reach.limit * Math.max(reach.turn, 1);
  const messageReach = reach.limit * Math.max(reach.message, 1);
  const floor = Math.max(pageLow - pageBudget(reach), oldest);
  let boundary = pageLow;
  let cut = false;
  for (;;) {
    const at = boundary;
    let page = known.filter((row) => row.seq >= at);
    if (page.length === 0) return { seq: boundary, cut: cut && boundary > oldest };
    let moved = false;
    if (!page[0].startsTurn) {
      const turnFloor = Math.max(boundary - turnReach, floor);
      const starts = known.filter((row) => row.startsTurn && row.seq >= turnFloor && row.seq < at).map((row) => row.seq);
      if (starts.length > 0) {
        boundary = Math.max(...starts);
        moved = true;
        const lowered = boundary;
        page = known.filter((row) => row.seq >= lowered);
      }
    }
    // Only a row that names a message can be the end of one.
    if (page.some((row) => row.messageId !== null)) {
      const top = boundary;
      const { unproved, target } = reachBelow(
        known.filter((row) => row.seq >= floor && row.seq < top),
        page,
      );
      // A descent that ends within a message's reach of its floor has looked
      // at almost nothing under the page, and an opening row inside the page
      // proves nothing there: it is the boundary's distance from the FLOOR
      // that says whether anything could have been seen.
      if (floor > oldest && boundary - floor < messageReach) {
        cut = true;
      } else if (target === null) {
        cut = cut || (unproved && floor > oldest);
      } else {
        boundary = target;
        moved = true;
      }
    }
    if (!moved) {
      const from = boundary;
      const opensATurn = known.find((row) => row.seq >= from)?.startsTurn ?? false;
      return { seq: boundary, cut: (cut || !opensATurn) && boundary > oldest };
    }
  }
}

/** `chat_service.list_messages_before`: the `limit` rows nearest `before`
 *  (exclusive; the newest page when null), ascending, then lowered until its
 *  first row opens a turn AND belongs to no message that began below it (see
 *  `alignPageBoundary`). `cut` says the page opens mid-turn, or carries a
 *  message whose opening it could not reach: the page below carries the rest.
 *
 *  `messageReach: null` is a server from before that rule, which a portal may
 *  still be talking to: the page is extended down to the nearest prompt row
 *  within `limit × reach` rows and nothing more — a prompt a person sent while
 *  the machine was still writing anchors the page INSIDE that message — and
 *  `cut` is composed here as that server composed it. */
export function pageBefore(
  rows: readonly LogRow[],
  before: number | null,
  limit: number,
  reach: number = PAGE_TURN_REACH,
  messageReach: number | null = PAGE_MESSAGE_REACH,
): ChatMessageList {
  const below = before === null ? rows : rows.filter((row) => row.seq < before);
  let page = below.slice(Math.max(below.length - limit, 0));
  if (page.length === 0) {
    return { items: [], next_after_seq: 0, prev_before: null, has_older: false, cut: false };
  }
  let low = page[0].seq;
  if (messageReach === null) {
    if (page[0].kind !== "prompt") {
      const floor = low - limit * Math.max(reach, 1);
      let anchor: number | null = null;
      for (const row of rows) {
        if (row.kind === "prompt" && row.seq < low && row.seq >= floor) anchor = row.seq;
      }
      if (anchor !== null) {
        const from = anchor;
        page = [...rows.filter((row) => row.seq >= from && row.seq < low), ...page];
        low = from;
      }
    }
  } else {
    // The stretch the rule decides on, and no more: the budget under the
    // page, down to the transcript's first row — as the server reads it.
    const oldest = rows.length ? rows[0].seq : 1;
    const answer = alignBoundary(below.map(pageRowOf), {
      pageLow: low,
      oldest,
      reach: { limit, turn: reach, message: messageReach },
    });
    const opensAt = answer.seq;
    page = below.filter((row) => row.seq >= opensAt);
    low = answer.seq;
    // The rule's `cut` is the page's, verbatim: it already says whether the
    // page opens mid-turn or carries a message it cannot open.
    return {
      items: page,
      next_after_seq: page[page.length - 1].seq,
      prev_before: low,
      has_older: low > oldest,
      cut: answer.cut,
    };
  }
  const oldest = rows.length ? rows[0].seq : 0;
  return {
    items: page,
    next_after_seq: page[page.length - 1].seq,
    prev_before: low,
    has_older: low > oldest,
    cut: page[0].kind !== "prompt" && low > oldest,
  };
}

/** The transcript row as the document window carries it: the same envelope a
 *  REST row's `payload` is, stamped with the sequence its durable write got. */
function docEntry(row: LogRow): Record<string, unknown> {
  const payload = isRecord(row.payload) ? row.payload : {};
  return { ...payload, event_id: row.event_id, role: row.role, kind: row.kind, seq: row.seq };
}

/** The `user_message` relay the server puts on the socket the moment it
 *  records a person's message, their answer to an ask, or their Stop. */
function relayOf(row: LogRow): Record<string, unknown> | null {
  const payload = row.payload;
  if (row.kind === "prompt" && isRecord(payload) && typeof payload.text === "string") {
    return {
      kind: "prompt",
      text: payload.text,
      message_id: row.event_id,
      at: row.created_at,
      seq: row.seq,
      ...(typeof payload.client_id === "string" ? { client_id: payload.client_id } : {}),
    };
  }
  if (isRecordedAnswer(row)) {
    const event = eventOf(row);
    const optionId = event?.option_id;
    return {
      kind: "prompt",
      interrupt_id: event?.request_id ?? "",
      user_id: "member",
      ...(typeof optionId === "string" ? { option_id: optionId } : {}),
    };
  }
  if (stopNoteRowOf(row) === "created") return { kind: "stop", user_id: "member" };
  return null;
}

/** The Stop relay for a note whose FIRST row the tab already held when it
 *  opened: the server writes the note's three rows together and relays after,
 *  so a tab sampled open between them still hears the relay — on the note's
 *  last row, which is when the write is whole. */
function lateStopRelayOf(row: LogRow, pushed: readonly LogRow[]): Record<string, unknown> | null {
  if (stopNoteRowOf(row) !== "done") return null;
  const note = (row.event_id ?? "").replace(/-done$/, "");
  if (pushed.some((other) => other.event_id === `${note}-created`)) return null;
  return { kind: "stop", user_id: "member" };
}

/** The harness event a row carries. */
function eventOf(row: LogRow): Record<string, unknown> | null {
  const payload = row.payload;
  if (!isRecord(payload)) return null;
  if (typeof payload.event_type === "string") return payload;
  return isRecord(payload.payload) ? payload.payload : null;
}

/** The server's record of a member's answer (`answer-<request>`): written to
 *  the transcript, never published onto the document by any box. */
function isRecordedAnswer(row: LogRow): boolean {
  return (
    (row.kind === "permission.resolved" || row.kind === "question.answered" || row.kind === "question.rejected") &&
    (row.event_id ?? "").startsWith("answer-")
  );
}

/** Which of the three rows of a Stop note this is, or null: the server writes
 *  them to the transcript and relays the Stop; no box publishes them. */
function stopNoteRowOf(row: LogRow): "created" | "text" | "done" | null {
  const match = /^stop-[0-9a-f]+-(created|text|done)$/.exec(row.event_id ?? "");
  return match ? (match[1] as "created" | "text" | "done") : null;
}

/** The token frame the box streams into a text or thought it has just opened.
 *
 *  The durable record holds a part's `part.started` (no words) and, once the
 *  box settles it, its `part.created` (all of them); the words in between
 *  reach a live tab only as these frames, which are never written down. A tab
 *  that watched therefore holds words for every open part that the log does
 *  not — and for a part the box never settled (a Stop landed first) it holds
 *  words the record never will. The model streams one frame into every part
 *  the log opens, so that difference is on the live side of the comparison as
 *  it is on the wire. */
function tokenFrameOf(row: LogRow): Record<string, unknown> | null {
  if (row.kind !== "part.started") return null;
  const event = eventOf(row);
  const type = event?.part_type;
  if (type !== "text" && type !== "reasoning") return null;
  const initial = isRecord(event?.initial) ? event.initial : null;
  if (initial?.synthetic === true) return null;
  // The box's step markers ride the text part type; only the part itself
  // says it is words or a thought, and only those stream.
  if (initial && initial.type !== "text" && initial.type !== "reasoning") return null;
  // A `part.started` that already carries words, or an end time, is the box
  // re-announcing a part it has settled: nothing streams after it.
  if (typeof initial?.text === "string" && initial.text !== "") return null;
  if (isRecord(initial?.time) && initial.time.end != null) return null;
  const partId = event?.part_id ?? initial?.id;
  const messageId = event?.message_id ?? initial?.messageID;
  if (typeof partId !== "string" || typeof messageId !== "string") return null;
  return {
    event_type: type === "text" ? "agent.message_chunk" : "agent.thought_chunk",
    message_id: messageId,
    part_id: partId,
    text: "[streamed]",
  };
}

/** Whether a row reaches a live tab as a document op at all. The server's own
 *  rows do not: the tab hears the relay and reads them back. */
function publishedOnDocument(row: LogRow): boolean {
  return !isRecordedAnswer(row) && stopNoteRowOf(row) === null;
}

// --- a scripted document handle ------------------------------------------------

interface ScriptedDoc {
  open(): DocHandle<never>;
  snapshot(state: Record<string, unknown>): void;
  op(payload: OpPayload, ephemeral?: boolean): void;
}

function scriptedDoc(): ScriptedDoc {
  const listeners = new Set<(m: DocMessage<unknown>) => void>();
  let seq = 0;
  return {
    open: (): DocHandle<never> =>
      ({
        onMessage: (listener: (m: DocMessage<unknown>) => void) => {
          listeners.add(listener);
          return () => listeners.delete(listener);
        },
        onPhase: () => () => undefined,
        getPhase: () => ({
          phase: "live",
          epoch: 1,
          seq,
          peerId: "p:oracle",
          canWrite: false,
          pending: 0,
          error: null,
        }),
        sendOp: () => Promise.reject(new Error("a reader never writes to a chat document")),
        dispose: () => listeners.clear(),
      }) as unknown as DocHandle<never>,
    snapshot(state) {
      seq += 1;
      listeners.forEach((l) => l({ kind: "snapshot", state, epoch: 1, seq }));
    },
    op(payload, ephemeral = false) {
      seq += 1;
      listeners.forEach((l) =>
        l({ kind: "op", payload, peerId: "pub:box", epoch: 1, seq, ephemeral }),
      );
    },
  };
}

/** Let every promise the source chained on the reads settle. */
const settle = async (): Promise<void> => {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
};

// --- one opened chat --------------------------------------------------------------

export interface OpenedChat {
  readonly source: CloudDataSource;
  /** Fold rows the socket appends after the open, in order. Prompts ride the
   *  relay lane as they do on the wire; everything else is a durable append. */
  push(rows: readonly LogRow[]): Promise<void>;
  /** The publisher's word on the meta lane. */
  setTurnState(word: TurnState): Promise<void>;
  view(): Promise<FoldView>;
  close(): void;
  /** Every REST read the tab has made, in order (`tail`, `before:<seq>`,
   *  `after:<seq>`). */
  readonly reads: readonly string[];
}

export interface OpenOptions {
  /** The order the cold open's two reads land in. */
  order?: ColdOpenOrder;
  /** The turn word the document carries at the open. Default: derived from the log. */
  turnState?: TurnState | null;
  /** The lowest sequence the socket's retained window still holds. Default: the
   *  whole log — a window nothing has compacted. */
  windowFrom?: number;
  /** REST page size. Default: the portal's. */
  pageRows?: number;
  /** The server's message reach, in page limits; `null` for a server from
   *  before a page was kept outside every message. Default: the current one. */
  serverMessageReach?: number | null;
}

/** Open the chat cold at `n`: a fresh `CloudDataSource`, the REST tail page and
 *  the socket snapshot both built from the log's rows `≤ n`, landing in `order`. */
export async function openAt(
  log: readonly LogRow[],
  n: number,
  opts: OpenOptions = {},
): Promise<OpenedChat> {
  // What the durable record holds: everything up to `n` at the open, and
  // every row pushed after it the moment it is pushed — the server writes a
  // row before it relays anything about it, so a tab reading forward on a
  // relay finds the row.
  let bound = n;
  const messageReach = opts.serverMessageReach === undefined ? PAGE_MESSAGE_REACH : opts.serverMessageReach;
  /** Every REST read the tab made, in order: what a test counts fetches by. */
  const reads: string[] = [];
  const rows = eventsUpTo(log, n);
  const order = opts.order ?? "rest-first";
  const pageRows = opts.pageRows ?? TRANSCRIPT_PAGE_ROWS;
  const word = opts.turnState === undefined ? turnStateAt(log, n) : opts.turnState;
  const windowFrom = opts.windowFrom ?? 0;
  const doc = scriptedDoc();
  // The REST reads answer on the microtask queue, so "socket first" is the
  // snapshot delivered before the tail page's promise resolves.
  let gate: Promise<void> = Promise.resolve();
  const rest = {
    listMessages: async (
      _chatId: string,
      query: { afterSeq?: number; limit?: number; before?: number; tail?: boolean } = {},
    ): Promise<ChatMessageList> => {
      await gate;
      // `pageRows` is the server's page ceiling: a caller asking for more is
      // cut to it, which is how the reference reads the whole log in one page
      // while the portal's own `limit: 200` still shapes every other open.
      const limit = Math.min(query.limit ?? pageRows, pageRows);
      const held = bound === n ? rows : eventsUpTo(log, bound);
      reads.push(query.tail ? "tail" : query.before !== undefined ? `before:${query.before}` : `after:${query.afterSeq ?? 0}`);
      if (query.tail) return pageBefore(held, null, limit, PAGE_TURN_REACH, messageReach);
      if (query.before !== undefined) {
        return pageBefore(held, query.before, limit, PAGE_TURN_REACH, messageReach);
      }
      return pageAfter(held, query.afterSeq ?? 0, limit);
    },
  };
  const source = new CloudDataSource({
    rest: rest as never,
    openDoc: () => doc.open(),
    acquire: () => () => undefined,
    clientId: () => "oracle",
    // The portal asks for the page size the server serves; a page that comes
    // back LARGER than that is how the portal knows the server reached for a
    // message. The model keeps the two the same number, as production does.
    pageRows,
  });
  const unsubscribe = source.subscribeChat(CHAT_ID, () => undefined);
  const window = rows
    .filter((row) => row.seq >= windowFrom && row.kind !== "prompt")
    .map(docEntry);
  const meta: Record<string, unknown> = { session_id: CHAT_ID };
  if (word) meta.turn_state = { state: word, at: "2026-01-01T00:00:00Z" };
  const snapshot = (): void => doc.snapshot({ meta, events: window, ids: {} });
  if (order === "socket-first") {
    let release: () => void = () => undefined;
    gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const turns = source.getChatTurns(CHAT_ID);
    snapshot();
    release();
    await turns;
  } else {
    await source.getChatTurns(CHAT_ID);
    snapshot();
  }
  await settle();
  return {
    source,
    async push(more) {
      for (const row of more) {
        bound = Math.max(bound, row.seq);
        // The server writes a Stop's three rows, then relays the Stop: by the
        // time a tab hears it, the whole note is on record.
        if (stopNoteRowOf(row) === "created") {
          const note = (row.event_id ?? "").replace(/-created$/, "");
          const last = log.find((r) => r.event_id === `${note}-done`);
          if (last) bound = Math.max(bound, last.seq);
        }
        const relay = relayOf(row) ?? lateStopRelayOf(row, more);
        if (relay) doc.op({ op_id: `relay:${row.seq}`, intent: "user_message", events: [relay] });
        else if (publishedOnDocument(row)) {
          doc.op({ op_id: `append:${row.seq}`, intent: "append", events: [docEntry(row)] });
          const token = tokenFrameOf(row);
          if (token) doc.op({ op_id: `token:${row.seq}`, intent: "chunk", events: [token] }, true);
        }
        // A relay makes the tab read forward; let that read land before the
        // next row, as it does on the wire.
        if (relay) await settle();
      }
      await settle();
    },
    async setTurnState(state) {
      doc.op({
        op_id: `meta:${state}`,
        intent: "set_meta",
        meta: { turn_state: { state, at: "2026-01-01T00:00:01Z" } },
      });
      await settle();
    },
    async view() {
      const turns = await source.getChatTurns(CHAT_ID);
      return {
        turns,
        pendingAsk: pendingAskKind(turns),
        awaitsResponse: conversationAwaitsResponse(turns),
        turnState: source.turnState(CHAT_ID),
        hasOlder: source.transcriptHistory(CHAT_ID).hasOlder,
      };
    },
    close: unsubscribe,
    reads,
  };
}

/** The reference: a tab that read the WHOLE log up to `n` in one page, with a
 *  window nothing compacted. What every other way of reading must agree with. */
export async function referenceView(log: readonly LogRow[], n: number): Promise<FoldView> {
  const opened = await openAt(log, n, { pageRows: Math.max(n, 1) + 1 });
  try {
    return await opened.view();
  } finally {
    opened.close();
  }
}

/** A fresh open at `n`, as the portal does it. */
export async function snapshotView(
  log: readonly LogRow[],
  n: number,
  opts: OpenOptions = {},
): Promise<FoldView> {
  const opened = await openAt(log, n, opts);
  try {
    return await opened.view();
  } finally {
    opened.close();
  }
}

/** A tab that opened at `n0` and folded rows `n0+1..n` live off the socket. */
export async function liveView(
  log: readonly LogRow[],
  n0: number,
  n: number,
  opts: OpenOptions = {},
): Promise<FoldView> {
  const opened = await openAt(log, n0, opts);
  try {
    await opened.push(log.filter((row) => row.seq > n0 && row.seq <= n));
    const word = turnStateAt(log, n);
    if (word) await opened.setTurnState(word);
    const view = await opened.view();
    return { ...view, turns: durableTurns(view.turns) };
  } finally {
    opened.close();
  }
}

// --- the comparison -----------------------------------------------------------------

/** A fold with what the durable log cannot hold taken out.
 *
 *  A text or thought still open is fed by token frames, which are never
 *  written down: the log holds its `part.started` with no words, and a fold of
 *  the log shows nothing for it until the part settles. Compared against a
 *  durable fold, a part the durable side cannot have is not a disagreement —
 *  while a turn streams the live side is simply ahead. A settled part is the
 *  log's to hold, and is compared. The browser detector and the corpus both
 *  read the live side through this. */
export function durableTurns(turns: ConversationTurn[]): ConversationTurn[] {
  return turns.map((turn) => {
    const parts = turn.parts.filter(
      (part) => !((part.kind === "text" || part.kind === "thinking") && part.streaming === true),
    );
    return parts.length === turn.parts.length ? turn : { ...turn, parts };
  });
}

/** One place two views disagree, named down to the turn, part and field. */
export interface Divergence {
  path: string;
  live: unknown;
  snapshot: unknown;
}

const IGNORED_FIELDS = new Set<string>([]);

/** A flag a settle wrote as `false` and a live fold never wrote at all render
 *  the same; nothing else is folded together. */
const sameFlag = (a: unknown, b: unknown): boolean =>
  (a === undefined && b === false) || (a === false && b === undefined);

function diffValue(path: string, a: unknown, b: unknown, out: Divergence[]): void {
  if (a === b || sameFlag(a, b)) return;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) {
      out.push({ path: `${path}.length`, live: a.length, snapshot: b.length });
    }
    const n = Math.min(a.length, b.length);
    for (let i = 0; i < n; i += 1) diffValue(`${path}[${i}]`, a[i], b[i], out);
    return;
  }
  if (isRecord(a) && isRecord(b)) {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const key of keys) {
      if (IGNORED_FIELDS.has(key)) continue;
      if (a[key] === undefined && b[key] === undefined) continue;
      diffValue(`${path}.${key}`, a[key], b[key], out);
    }
    return;
  }
  out.push({ path, live: a, snapshot: b });
}

function turnPath(turn: ConversationTurn, index: number): string {
  return `turn[${index}:${turn.id}]`;
}

/** Where `live` and `snapshot` disagree. A view read through a cold open holds
 *  only the log's tail, so the comparison walks the turns the SHORTER view
 *  holds, matched by turn id, and then the two composer readings. */
export function diffViews(live: FoldView, snapshot: FoldView): Divergence[] {
  const out: Divergence[] = [];
  const [shorter, longer, shorterName, longerName] =
    live.turns.length <= snapshot.turns.length
      ? [live, snapshot, "live", "snapshot"]
      : [snapshot, live, "snapshot", "live"];
  const byId = new Map(longer.turns.map((turn) => [turn.id, turn] as const));
  shorter.turns.forEach((turn, index) => {
    const other = byId.get(turn.id);
    if (!other) {
      out.push({
        path: `${turnPath(turn, index)} missing from ${longerName}`,
        live: shorterName === "live" ? turn.author : undefined,
        snapshot: shorterName === "snapshot" ? turn.author : undefined,
      });
      return;
    }
    const pairs: Divergence[] = [];
    const [a, b] = shorterName === "live" ? [turn, other] : [other, turn];
    diffValue(turnPath(turn, index), a, b, pairs);
    out.push(...pairs);
  });
  // The other direction: a turn only the LONGER view holds — the note a Stop
  // leaves, missing from the tab that watched it happen. It is always
  // reported, with one exception the shorter view has to claim for itself: a
  // tab opened on a page holds the log's tail and SAYS there is more above it
  // (`hasOlder`), and what lies above the first turn the two views share is
  // then history it never loaded, not a turn it dropped. A view that claims
  // nothing, or shares no turn with the other, is excused nothing — so a Stop
  // note first in the log, one between any two turns, and a shorter view of
  // one turn or none are all seen.
  const held = new Set(shorter.turns.map((turn) => turn.id));
  // Where the shorter view STARTS, in the longer one's order: its own first
  // turn, or the first of its turns the longer view holds at all. Not the
  // lowest place any shared turn has — the two views can place a turn
  // differently (a message said mid-turn stands where it was said in a tab
  // that heard it, and where the box echoed it on a page), and one such turn
  // would drag the floor up over history the page never loaded.
  const place = new Map(longer.turns.map((turn, index) => [turn.id, index] as const));
  const top = shorter.turns.find((turn) => place.has(turn.id));
  const floor = shorter.hasOlder === true && top ? (place.get(top.id) as number) : -1;
  longer.turns.forEach((turn, index) => {
    if (index < floor || held.has(turn.id)) return;
    out.push({
      path: `${turnPath(turn, index)} missing from ${shorterName}`,
      live: longerName === "live" ? turn.author : undefined,
      snapshot: longerName === "snapshot" ? turn.author : undefined,
    });
  });
  diffValue("pendingAsk", live.pendingAsk, snapshot.pendingAsk, out);
  diffValue("awaitsResponse", live.awaitsResponse, snapshot.awaitsResponse, out);
  diffValue("turnState", live.turnState, snapshot.turnState, out);
  return out;
}

/** The divergences as one readable block. */
export function describeDivergences(divergences: readonly Divergence[]): string {
  return divergences
    .map((d) => `${d.path}: live=${JSON.stringify(d.live)} snapshot=${JSON.stringify(d.snapshot)}`)
    .join("\n");
}

/** What one check of the invariant produced: the divergences (empty when the
 *  two converge) beside the two views they were read off, so a caller can name
 *  a divergence by the parts each side holds. */
export interface ConvergenceResult {
  divergences: Divergence[];
  live: FoldView;
  snapshot: FoldView;
}

/** The invariant, as a function: `snapshotAt(n0)` folded forward with
 *  `eventsUpTo(n)` must equal `snapshotAt(n)`. Returns the result rather than
 *  throwing, so a caller can pin a known set as well as assert none. */
export async function assertConverges(
  snapshotAt: (n: number) => Promise<OpenedChat>,
  eventsUpTo: (n: number) => readonly LogRow[],
  n0: number,
  n: number,
): Promise<ConvergenceResult> {
  const opened = await snapshotAt(n0);
  let live: FoldView;
  try {
    await opened.push(eventsUpTo(n).filter((row) => row.seq > n0));
    const word = turnStateAt(eventsUpTo(n), n);
    if (word) await opened.setTurnState(word);
    const view = await opened.view();
    // The live side is compared on what the log can hold (`durableTurns`).
    live = { ...view, turns: durableTurns(view.turns) };
  } finally {
    opened.close();
  }
  const fresh = await snapshotAt(n);
  let snapshot: FoldView;
  try {
    snapshot = await fresh.view();
  } finally {
    fresh.close();
  }
  return { divergences: diffViews(live, snapshot), live, snapshot };
}

/** `assertConverges` bound to one log and one cold-open shape. */
export function convergenceOf(log: readonly LogRow[], opts: OpenOptions = {}) {
  return (n0: number, n: number): Promise<ConvergenceResult> =>
    assertConverges(
      (at) => openAt(log, at, opts),
      (at) => eventsUpTo(log, at),
      n0,
      n,
    );
}

// --- a chat nobody has opened yet ---------------------------------------------------
//
// `openAt` opens the chat itself, which is what an oracle comparing two folds
// wants. A test that drives the CHAT STORE through the real source cannot use
// it: the store's own subscription has to be the one that opens the socket, and
// the frames have to arrive where the test puts them — including a second
// `hello`, which is a socket coming back.

/** A `CloudDataSource` on a scripted transport and document, with nobody
 *  subscribed. The caller opens the chat and decides when each frame lands. */
export interface ScriptedChat {
  readonly source: CloudDataSource;
  readonly chatId: string;
  /** The log the REST surface serves, oldest first. Grows with `push` and with
   *  every message the source posts. */
  readonly rows: LogRow[];
  /** The text of every message the source has posted, oldest first. */
  readonly sent: string[];
  /** Deliver a `hello`: the retained window and the meta the document carries.
   *  The first is the cold open; a later one is the socket coming back.
   *  `turnState: null` leaves the key OUT, which is what a frame that says
   *  nothing about the turn looks like — the publisher's meta is merged
   *  server-side and a frame need not re-state it. */
  hello(opts?: { turnState?: TurnState | null; windowFrom?: number }): Promise<void>;
  /** Record rows and put them on the socket, as the publisher does: a prompt on
   *  the relay lane, everything else as a durable append. */
  push(rows: readonly LogRow[]): Promise<void>;
  /** The publisher's word on the meta lane. */
  setTurnState(word: TurnState): Promise<void>;
  /** Let the reads the source chained settle, without moving anything. */
  settle(): Promise<void>;
}

export function scriptedChat(chatId: string = CHAT_ID): ScriptedChat {
  const rows: LogRow[] = [];
  const sent: string[] = [];
  const doc = scriptedDoc();
  let posted = 0;
  const rest = {
    listMessages: async (
      _chatId: string,
      query: { afterSeq?: number; limit?: number; before?: number; tail?: boolean } = {},
    ): Promise<ChatMessageList> => {
      const limit = Math.min(query.limit ?? TRANSCRIPT_PAGE_ROWS, TRANSCRIPT_PAGE_ROWS);
      if (query.tail) return pageBefore(rows, null, limit);
      if (query.before !== undefined) return pageBefore(rows, query.before, limit);
      return pageAfter(rows, query.afterSeq ?? 0, limit);
    },
    postMessage: async (
      _chatId: string,
      body: { text: string; client_id: string },
    ): Promise<ChatMessageRead> => {
      sent.push(body.text);
      posted += 1;
      const row: LogRow = {
        id: `msg-${posted}`,
        chat_id: chatId,
        seq: (rows[rows.length - 1]?.seq ?? 0) + 1,
        role: "user",
        kind: "prompt",
        event_id: `usr:${body.client_id}`,
        payload: { kind: "prompt", text: body.text, client_id: body.client_id },
        created_at: "2026-01-01T00:00:00Z",
      };
      rows.push(row);
      return row;
    },
  };
  const source = new CloudDataSource({
    rest: rest as never,
    openDoc: () => doc.open(),
    acquire: () => () => undefined,
    clientId: () => `oracle-${posted + 1}`,
  });
  return {
    source,
    chatId,
    rows,
    sent,
    async hello(opts = {}) {
      const from = opts.windowFrom ?? 0;
      const meta: Record<string, unknown> = { session_id: chatId };
      const word = opts.turnState ?? null;
      if (word) meta.turn_state = { state: word, at: "2026-01-01T00:00:00Z" };
      doc.snapshot({
        meta,
        events: rows.filter((row) => row.seq >= from && row.kind !== "prompt").map(docEntry),
        ids: {},
      });
      await settle();
    },
    async push(more) {
      for (const row of more) {
        rows.push(row);
        const relay = relayOf(row);
        if (relay) doc.op({ op_id: `relay:${row.seq}`, intent: "user_message", events: [relay] });
        else doc.op({ op_id: `append:${row.seq}`, intent: "append", events: [docEntry(row)] });
      }
      await settle();
    },
    async setTurnState(word) {
      doc.op({
        op_id: `meta:${word}:${Date.now()}`,
        intent: "set_meta",
        meta: { turn_state: { state: word, at: "2026-01-01T00:00:01Z" } },
      });
      await settle();
    },
    settle,
  };
}

/** The sequence numbers where a person's turn starts — the rows the server
 *  recorded their messages at, where a cold open's page may begin. The box's
 *  own `message.created` for the message is not one: it repeats the row, and
 *  is re-announced from inside the answer every time the session moves on.
 *  The natural sample of `n0`. */
export function turnStarts(log: readonly LogRow[]): number[] {
  const starts: number[] = [];
  for (const row of log) {
    if (row.kind === "prompt") starts.push(row.seq);
  }
  return starts;
}
