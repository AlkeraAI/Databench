// The loaded window of a long transcript: the pages a reader has scrolled
// into, each folded on its own, joined at turn starts.
//
// The fold is forward-only — one event onto one state — so a page of OLDER
// events cannot fold onto a state that already holds newer ones. This module
// keeps each page as a segment with its own fold state and joins segments
// instead: a page that ends exactly where the next turn begins becomes a new
// head; a page that cut a turn in half is re-folded together with the segment
// it was cut from, so a tool call on one page and its result on the next read
// as one card. The newest segment is the live one: the tail the chat opened on
// and everything the socket appends afterwards.
//
// Pure: rows in, turns out. The two shells supply an adapter that says how
// their rows fold, which rows start a turn, and which name compacted turns.

import type { ConversationTurn } from "@alkera/chat-model";

import {
  markAllTurnsDirty,
  settleStaleInterrupts,
  takeDirtyTurnIds,
  type ConversationFoldState,
} from "./harnessEventFold";

/** One REST page: the server default, and one round trip. */
export const TRANSCRIPT_PAGE_ROWS = 200;
/** One daemon page, in raw events: a transcript row is two or three of them. */
export const TRANSCRIPT_PAGE_EVENTS = 400;
/** The loaded-row budget; head segments beyond it are released once the
 *  reader is back at the bottom. */
export const TRANSCRIPT_WINDOW_MAX_ROWS = 4000;
/** How close to the top the reader must be for the next page to be asked for. */
export const TRANSCRIPT_TOP_THRESHOLD_PX = 600;

/** What every row a window holds must say about itself. `seq` is the durable
 *  key (a transcript sequence, a log ordinal); null for a live frame that has
 *  none yet, which is by construction newer than anything loaded. `eventId`
 *  de-duplicates a row that arrives twice (a page and a snapshot); null is
 *  never de-duplicated. */
export interface WindowRow {
  readonly seq: number | null;
  readonly eventId: string | null;
  /** A frame that is not in the durable record and never will be: a streamed
   *  token, superseded by the part the publisher writes once the text settles.
   *  It carries no sequence AND names nothing, so it does not stop a cut — a
   *  page below can never bring it back, and never has to. */
  readonly ephemeral?: boolean;
}

export interface TranscriptWindowAdapter<R extends WindowRow> {
  createState(): ConversationFoldState;
  /** Fold a contiguous ascending batch. A batch, not a row, because a source's
   *  fold may read the batch as context (the cloud's "was this prompt taken"). */
  foldRows(state: ConversationFoldState, rows: readonly R[]): void;
  /** A person's message: where a turn — and so a segment — may begin. */
  startsTurn(row: R): boolean;
  /** The message a row belongs to FOR PAGING, as the server counts it: every
   *  message the transcript names, whoever published it — the machine's
   *  answers, the box's echo of the person's message, the notes the server
   *  writes into a turn — by the id the row states and nothing else, except a
   *  cancelled prompt's row (its id names the prompt it cancels). Null for a
   *  row that names none. The window's page-shape checks use THIS notion, so
   *  the window and the server agree on what "inside a message" means; how a
   *  row FOLDS is the fold's business and may be narrower (an echo is the
   *  person's turn, not a message of its own). A source that cannot say leaves
   *  every turn start a legal cut, as before. */
  pageMessageOf?(row: R): string | null;
  /** The message a row OPENS for paging (its `message.created`, any role). */
  opensPageMessage?(row: R): string | null;
  /** The MACHINE's message a row opens, or null. Not membership: it is the
   *  evidence that this source writes openings at all, and where a scan for
   *  the tail of an earlier message can stop — the machine writes one message
   *  at a time, so once it has opened a new one nothing older follows. */
  opensMachineMessage?(row: R): string | null;
  /** Turn ids a compaction in this row elided, so a card below can dim turns
   *  that live in a segment above. */
  elidedTurnIds(row: R): readonly string[];
}

export interface TranscriptSegment<R extends WindowRow> {
  /** Lowest known sequence; `Infinity` for a live segment nothing has landed in. */
  lo: number;
  rows: R[];
  state: ConversationFoldState;
}

export class TranscriptWindow<R extends WindowRow> {
  private segments: TranscriptSegment<R>[] = [];
  private readonly seen = new Set<string>();
  private readonly elided = new Set<string>();
  /** The copy handed out for each folded turn, keyed by the fold's own object.
   *  Rebuilt from the loaded turns on every snapshot, so a released page's
   *  entries go with it. */
  private shared = new Map<ConversationTurn, ConversationTurn>();

  constructor(private readonly adapter: TranscriptWindowAdapter<R>) {}

  /** The live segment's fold state, created empty on first use. Callers hold
   *  no reference across calls: a merge replaces it. */
  get liveState(): ConversationFoldState {
    return this.live().state;
  }

  get segmentCount(): number {
    return this.segments.length;
  }

  /** The paging cursor: read one page BELOW this, and everything the window
   *  has let go of comes back. Null while the window holds no sequence at all,
   *  which is a window that cannot page — see `releaseHead`. */
  get oldestSeq(): number | null {
    const head = this.segments[0];
    return head && Number.isFinite(head.lo) ? head.lo : null;
  }

  /** The highest sequence loaded, or null while nothing with one has. A source
   *  whose live lane is an ordered log stamps its own live rows from here, so
   *  a row that arrives on the socket is as addressable as one that arrived in
   *  a page — which is what lets the budget reach it. */
  get newestSeq(): number | null {
    let high: number | null = null;
    for (const segment of this.segments) {
      for (const row of segment.rows) {
        if (row.seq !== null && (high === null || row.seq > high)) high = row.seq;
      }
    }
    return high;
  }

  get rowCount(): number {
    return this.segments.reduce((total, segment) => total + segment.rows.length, 0);
  }

  hasSeen(eventId: string): boolean {
    return this.seen.has(eventId);
  }

  /** Every loaded turn, oldest first. The arrays are the fold's own; callers
   *  that hand them out clone them. */
  turns(): ConversationTurn[] {
    if (this.segments.length === 1) return this.segments[0].state.turns;
    return this.segments.flatMap((segment) => segment.state.turns);
  }

  /** Every loaded turn as a copy the caller may hold and read from, oldest
   *  first.
   *
   *  A turn the fold has not changed since the last snapshot is the VERY
   *  object the last snapshot carried, so a consumer that keys on identity
   *  re-renders only what moved. That is what makes a streamed token cheap: a
   *  publisher flushing twenty frames a second into a window of thousands of
   *  turns pays for the one turn the token landed in, not a deep copy of the
   *  whole window per frame.
   *
   *  A fold path that cannot name the turns it touched leaves the state saying
   *  "all of them", so the conservative answer is a full re-copy — correct
   *  however the fold grows. */
  snapshot(): ConversationTurn[] {
    const out: ConversationTurn[] = [];
    const next = new Map<ConversationTurn, ConversationTurn>();
    for (const segment of this.segments) {
      const dirty = takeDirtyTurnIds(segment.state);
      for (const turn of segment.state.turns) {
        const held = this.shared.get(turn);
        const copy =
          held !== undefined && dirty !== null && !dirty.has(turn.id)
            ? held
            : structuredClone(turn);
        next.set(turn, copy);
        out.push(copy);
      }
    }
    this.shared = next;
    return out;
  }

  /** Every loaded segment's fold state, oldest first — for a caller that has to
   *  reach a card wherever it sits rather than only in the live tail: a spawn
   *  card whose child is still streaming can be pages above the live edge, and
   *  its activity has to land on the card that is actually loaded. */
  states(): ConversationFoldState[] {
    return this.segments.map((segment) => segment.state);
  }

  /** Fold rows into the live segment. Drops a row already folded and a row
   *  whose sequence sits below the live segment — that one belongs to a page
   *  the reader has not asked for, and would fold out of order here. Returns
   *  how many were folded. */
  appendLive(rows: readonly R[]): number {
    const live = this.live();
    // A segment holding nothing sequenced has no floor to be below: its `lo`
    // is `Infinity`, and reading that as a floor would drop every stamped row
    // that followed. A live segment opens that way whenever the first thing
    // to arrive is a streamed token.
    const floor = Number.isFinite(live.lo) ? live.lo : -Infinity;
    const fresh: R[] = [];
    for (const row of rows) {
      if (row.seq !== null && row.seq < floor && live.rows.length > 0) continue;
      if (!this.claim(row)) continue;
      fresh.push(row);
    }
    if (fresh.length === 0) return 0;
    this.adapter.foldRows(live.state, fresh);
    live.rows.push(...fresh);
    for (const row of fresh) {
      if (row.seq !== null && row.seq < live.lo) live.lo = row.seq;
    }
    this.noteElided(fresh);
    return fresh.length;
  }

  /** Fold rows into the live segment IN SEQUENCE ORDER, not arrival order: a
   *  row stamped below something the segment already holds is put back where
   *  the server put it and the segment is re-folded, so what a reader sees is
   *  the log's order however the two reads of a cold open landed. A person's
   *  prompt rides the relay lane and never sits in the socket window, so the
   *  REST tail that lands after the snapshot carries prompts that belong
   *  between rows already folded; appended after them, the person's message
   *  read below the answer to it and the ask on the answer was no longer on
   *  the last turn. Returns how many rows were new and whether the segment was
   *  re-folded (its settled state is then the caller's to re-apply). */
  mergeLive(rows: readonly R[]): { folded: number; liveRefolded: boolean } {
    const live = this.live();
    const fresh = rows.filter((row) => this.claim(row));
    if (fresh.length === 0) return { folded: 0, liveRefolded: false };
    const high = highest(live.rows);
    const outOfOrder = fresh.some((row) => row.seq !== null && row.seq < high);
    if (!outOfOrder) {
      this.adapter.foldRows(live.state, fresh);
      live.rows.push(...fresh);
      live.lo = lowest(fresh, live.lo);
      this.noteElided(fresh);
      return { folded: fresh.length, liveRefolded: false };
    }
    // Stamped rows in sequence order; an unstamped (ephemeral) row keeps its
    // place after the stamped row it arrived behind.
    const merged = [...live.rows, ...fresh];
    const order = new Map<R, number>();
    let last = -Infinity;
    merged.forEach((row) => {
      if (row.seq !== null) last = row.seq;
      order.set(row, last);
    });
    merged.sort((a, b) => (order.get(a) as number) - (order.get(b) as number));
    const state = this.adapter.createState();
    this.adapter.foldRows(state, merged);
    live.state = state;
    live.rows = merged;
    live.lo = lowest(fresh, live.lo);
    this.noteElided(fresh);
    this.dim(live);
    return { folded: fresh.length, liveRefolded: true };
  }

  /** A page below the window, ascending. The page becomes a new head when the
   *  cut between it and the current head falls on a turn start; otherwise the
   *  two are re-folded as one. Returns how many rows were new, and whether the
   *  live segment was among what was re-folded (its settled state is then the
   *  caller's to re-apply). */
  prependOlder(rows: readonly R[]): { folded: number; liveRefolded: boolean } {
    const fresh = rows.filter((row) => this.claim(row));
    if (fresh.length === 0) return { folded: 0, liveRefolded: false };
    const head = this.segments[0];
    if (!head || head.rows.length === 0) {
      // Nothing loaded yet: this page IS the tail the window opens on.
      const live = this.live();
      this.adapter.foldRows(live.state, fresh);
      live.rows.push(...fresh);
      live.lo = lowest(fresh, live.lo);
      this.noteElided(fresh);
      return { folded: fresh.length, liveRefolded: false };
    }
    // A turn start is a clean join only if the head holds its machine message
    // from the opening. A person can speak while the machine is still writing
    // (a Stop, and the next message sent before the stopped turn's last rows
    // land), and the server anchors a page on that message: the head then
    // opens on a person's words and goes on with the TAIL of the message
    // above. Kept as its own segment, that message would fold twice — its
    // opening and what it said in one segment, its end in the other.
    const cutOnTurn =
      this.adapter.startsTurn(head.rows[0]) &&
      !this.adapter.startsTurn(fresh[fresh.length - 1]) &&
      this.midMessageOf(head.rows) === null;
    const isLive = head === this.segments[this.segments.length - 1];
    if (cutOnTurn) {
      const state = this.adapter.createState();
      this.adapter.foldRows(state, fresh);
      settleStaleInterrupts(state);
      const segment: TranscriptSegment<R> = { lo: lowest(fresh, Infinity), rows: [...fresh], state };
      this.segments.unshift(segment);
      this.noteElided(fresh);
      this.dim(segment);
      return { folded: fresh.length, liveRefolded: false };
    }
    const merged = [...fresh, ...head.rows];
    const state = this.adapter.createState();
    this.adapter.foldRows(state, merged);
    if (!isLive) settleStaleInterrupts(state);
    const segment: TranscriptSegment<R> = { lo: lowest(fresh, head.lo), rows: merged, state };
    this.segments[0] = segment;
    this.noteElided(fresh);
    this.dim(segment);
    return { folded: fresh.length, liveRefolded: isLive };
  }

  /** Let go of the rows above `maxRows`, oldest first. Returns how many went,
   *  and whether the LIVE segment was among what was re-folded — its settled
   *  state is then the caller's to re-apply, exactly as after `prependOlder`.
   *
   *  Whole head segments go first. Once they are gone the LIVE segment is cut
   *  too, because a chat nobody has scrolled back in has only ever had one —
   *  and a budget that could not reach it was no budget at all for the tab left
   *  open across a multi-day turn.
   *
   *  Only the caller knows the reader is at the live edge, which is the whole
   *  licence for this: rows go from the top of what is on screen, and the
   *  durable record serves them again on the next scroll up. */
  releaseHead(maxRows: number): { released: number; liveRefolded: boolean } {
    let released = 0;
    while (this.segments.length > 1 && this.rowCount > maxRows) {
      const [gone] = this.segments.splice(0, 1);
      for (const row of gone.rows) if (row.eventId) this.seen.delete(row.eventId);
      released += gone.rows.length;
    }
    if (this.rowCount <= maxRows) return { released, liveRefolded: false };
    const cut = this.trimLive(maxRows);
    return { released: released + cut, liveRefolded: cut > 0 };
  }

  /** Cut the live segment's own head down to the budget, re-folding what is
   *  kept. The cut prefers a turn start, so the kept rows fold from the top of
   *  a turn and the page that comes back joins above as its own segment.
   *
   *  What bounds the cut is the PAGING CURSOR, not any one row's durability: a
   *  page is asked for as "everything below sequence X", so the window may only
   *  let go of rows it can still name that way. Rows arrive in durable order,
   *  so the stamped ones are a prefix; cutting inside it leaves stamped rows to
   *  take the cursor from, and cutting exactly at its end takes the cursor one
   *  past the last stamped row, which names every row that went and nothing
   *  that stayed. Past that frontier a dropped row could not be asked for
   *  again, so it stays: being over budget is the right answer when the
   *  alternative is losing transcript.
   *
   *  A source whose DURABLE live lane carries no sequences therefore only
   *  sheds what it read from the record. Stamping that lane is what lifts it:
   *  the cloud's server puts the sequence its durable write assigned on the
   *  append it rebroadcasts, and the daemon stamps from `newestSeq` because
   *  its live lane is an ordered log. A streamed token is the other case —
   *  never in the record at all, and so never a boundary (see `ephemeral`).
   *
   *  The ceiling this leaves: the cut lands only on a TURN START, so ONE turn
   *  larger than the budget never sheds at all. That is deliberate. A segment
   *  re-folded from inside a turn has no `message.created` to open on, so the
   *  parts it keeps attach to a synthetic turn of their own: the reader would
   *  see an answer with no question above it, and the page that brings the
   *  turn's opening back would fold the same message under a second turn id.
   *  Being over budget inside one turn is the lesser fault; a turn that ends
   *  releases the whole of it at the next cut. */
  private trimLive(maxRows: number): number {
    const live = this.segments[this.segments.length - 1];
    if (!live) return 0;
    const over = this.rowCount - maxRows;
    // The first DURABLE row the window could not name a cursor past. Nothing
    // at or above it may be dropped. A streamed token is transparent here: it
    // is not in the durable record, so letting it go loses nothing a page
    // could return — and treating one as a boundary is what made the budget a
    // no-op on a chat anybody was actually watching, since a turn puts a token
    // frame above the rows it settles into.
    let frontier = live.rows.length;
    for (let i = 0; i < live.rows.length; i += 1) {
      if (live.rows[i].seq === null && !live.rows[i].ephemeral) {
        frontier = i;
        break;
      }
    }
    const limit = Math.min(frontier, live.rows.length - 1);
    let cut = 0;
    // The machine messages that have rows above the candidate. A turn start is
    // a clean cut only if what the machine writes next is a NEW message: a
    // person can speak while the machine is still writing — a Stop, and the
    // next message sent before the stopped turn's last rows land — and a cut
    // there keeps that message's tail and drops its opening and everything it
    // had said, which is the re-fold from inside a turn the note above rules
    // out. Such a start is passed over for the next one.
    const open = new Set<string>();
    for (let i = 1; i <= limit; i += 1) {
      const above = this.namedBy(live.rows[i - 1]);
      if (above !== null) open.add(above);
      if (i < over || !this.adapter.startsTurn(live.rows[i])) continue;
      if (this.continuesAbove(live.rows, i, open)) continue;
      cut = i;
      break;
    }
    // No turn start is both far enough down and inside the frontier. Where the
    // frontier is a real boundary — stamped rows, then a live lane with no
    // sequences — the whole stamped prefix is still a legal cut: a cursor one
    // past its last row names every row that went and nothing that stayed, and
    // the kept rows re-fold from a clean state whether or not they happen to
    // open on a turn. With nothing unstamped there is no such boundary, and a
    // cut that does not land on a turn start is not worth making.
    if (cut === 0 && frontier >= 1 && frontier < live.rows.length) cut = frontier;
    if (cut === 0) return 0;
    const gone = live.rows.slice(0, cut);
    for (const row of gone) if (row.eventId) this.seen.delete(row.eventId);
    const kept = live.rows.slice(cut);
    const keptLow = lowest(kept, Infinity);
    const goneHigh = highest(gone);
    // The lowest sequence still held names the page below it; with none held,
    // one past the highest that went names everything that went. A cut whose
    // two sides are both sequenceless — only streamed tokens moved — leaves
    // the cursor where it was, which reads as "cannot page" rather than as a
    // number that would answer with the wrong rows.
    const cursor = Number.isFinite(keptLow)
      ? keptLow
      : Number.isFinite(goneHigh)
        ? goneHigh + 1
        : live.lo;
    const state = this.adapter.createState();
    this.adapter.foldRows(state, kept);
    const segment: TranscriptSegment<R> = { lo: cursor, rows: kept, state };
    this.segments[this.segments.length - 1] = segment;
    // The re-fold starts from a clean state, so a turn a compaction below
    // summarised has to be dimmed again on the way out.
    this.dim(segment);
    return cut;
  }

  /** The machine message `rows` holds only from somewhere inside it — the
   *  first one they name, when nothing in them opened it — or null when the
   *  first machine message they name is one they open (or they name none). */
  private midMessageOf(rows: readonly R[], told = false): string | null {
    // Absence alone proves nothing: a source may never write openings at all.
    // The rows have to show that machine messages here DO open — and then a
    // message named without its opening is one that opened above. Unless the
    // server has said as much (`told`): a page it cut inside one long message
    // is all that message's parts, and shows no opening of anything.
    if (!told && !rows.some((row) => (this.adapter.opensMachineMessage?.(row) ?? null) !== null)) {
      return null;
    }
    const opened = new Set<string>();
    for (const row of rows) {
      const opens = this.adapter.opensPageMessage?.(row) ?? null;
      if (opens !== null) opened.add(opens);
      const message = this.namedBy(row);
      if (message !== null && !opened.has(message)) return message;
      // The machine has opened a new message: nothing older follows it.
      if ((this.adapter.opensMachineMessage?.(row) ?? null) !== null) return null;
    }
    return null;
  }

  /** Whether the oldest rows held begin inside a machine message whose opening
   *  is above them. The page below would complete it; until then the fold
   *  shows that message's end with none of what it said. A window that opens
   *  on rows no person's message leads (a page the server says is cut) is not
   *  this: that turn is known to be read from its middle. */
  get opensMidMessage(): boolean {
    const head = this.segments[0];
    if (!head || head.rows.length === 0 || !this.adapter.startsTurn(head.rows[0])) return false;
    return this.midMessageOf(head.rows) !== null;
  }

  /** Whether the oldest rows held begin inside a machine message, whatever
   *  leads them. A server that keeps its pages outside every message hands one
   *  like this only when the message outran its reach, and says so (`cut`):
   *  the rows then start wherever the budget ended, on no person's message.
   *  `saidCut` is that word, and stands in for the evidence the rows lack. */
  beginsMidMessage(saidCut: boolean): boolean {
    const head = this.segments[0];
    if (!head || head.rows.length === 0) return false;
    return this.midMessageOf(head.rows, saidCut) !== null;
  }

  /** Whether the rows from `from` on go on with a message that already has
   *  rows above, before the machine opens a new one: a server note or the echo
   *  of the person's words may come first, and neither clears the message the
   *  machine was still writing. */
  private continuesAbove(rows: readonly R[], from: number, open: ReadonlySet<string>): boolean {
    for (let j = from; j < rows.length; j += 1) {
      const message = this.namedBy(rows[j]);
      // The row that OPENS a message is where it starts, whatever named it
      // above — a token frame can run ahead of the row it settles into.
      const opens = this.adapter.opensPageMessage?.(rows[j]) ?? null;
      if (message !== null && message !== opens && open.has(message)) return true;
      if ((this.adapter.opensMachineMessage?.(rows[j]) ?? null) !== null) return false;
    }
    return false;
  }

  /** The message a row names for page-shape purposes. A streamed token frame
   *  names none: it is never in the durable record, so the server that cuts
   *  the pages never sees it, and it cannot hold a message open here either. */
  private namedBy(row: R): string | null {
    if (row.ephemeral) return null;
    return this.adapter.pageMessageOf?.(row) ?? null;
  }

  /** Forget everything: the reset path when the durable record says to start
   *  again from somewhere else. */
  reset(): void {
    this.segments = [];
    this.seen.clear();
    this.elided.clear();
  }

  private live(): TranscriptSegment<R> {
    const last = this.segments[this.segments.length - 1];
    if (last) return last;
    const segment: TranscriptSegment<R> = { lo: Infinity, rows: [], state: this.adapter.createState() };
    this.segments.push(segment);
    return segment;
  }

  private claim(row: R): boolean {
    if (row.eventId === null || row.eventId === "") return true;
    if (this.seen.has(row.eventId)) return false;
    this.seen.add(row.eventId);
    return true;
  }

  private noteElided(rows: readonly R[]): void {
    let grew = false;
    for (const row of rows) {
      for (const id of this.adapter.elidedTurnIds(row)) {
        if (!this.elided.has(id)) {
          this.elided.add(id);
          grew = true;
        }
      }
    }
    if (grew) for (const segment of this.segments) this.dim(segment);
  }

  /** A turn a compaction below elided reads exactly as one the fold dimmed in
   *  its own segment would: out of context, and saying WHY — a summary, not a
   *  cleared conversation, which is the note the tape prints at the top of the
   *  run. */
  private dim(segment: TranscriptSegment<R>): void {
    if (this.elided.size === 0) return;
    for (const turn of segment.state.turns) {
      if (!this.elided.has(turn.id)) continue;
      // Written straight onto the fold's turn, so the next snapshot has to
      // re-copy it rather than carry the undimmed one over.
      markAllTurnsDirty(segment.state);
      turn.cleared = true;
      turn.clearedReason = "summarised";
    }
  }
}

function lowest<R extends WindowRow>(rows: readonly R[], fallback: number): number {
  let low = fallback;
  for (const row of rows) if (row.seq !== null && row.seq < low) low = row.seq;
  return low;
}

/** The highest sequence in a batch; `-Infinity` when none is stamped, so one
 *  past it is not a cursor any read would honour. */
function highest<R extends WindowRow>(rows: readonly R[]): number {
  let high = -Infinity;
  for (const row of rows) if (row.seq !== null && row.seq > high) high = row.seq;
  return high;
}
