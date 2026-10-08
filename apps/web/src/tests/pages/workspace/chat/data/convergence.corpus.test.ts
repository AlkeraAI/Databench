// The convergence net over the real corpus.
//
// For every fixture chat (a recorded server log of a chat whose views diverged) and
// a sample of (n0, n) pairs, a tab that opened at n0 and folded live must show
// what a tab opening fresh at n shows, whichever of the REST page and the
// socket snapshot lands first, and both must agree with the reference: the
// whole log read in one page.
//
// Every divergence found is sorted into a named CLASS below, each with its
// cause and a matcher tied to the exact shape in the log. The classes a fixture
// shows today are pinned per fixture: this is a ratchet, not a pass. A
// divergence outside every class fails the test with the diff naming the turn,
// part and field; a class a fix closes fails it too — until its entry is
// removed, so the fix is recorded beside the bug it closed.

import { describe, expect, it } from "vitest";

import {
  convergenceOf,
  diffViews,
  describeDivergences,
  durableTurns,
  eventsUpTo,
  openAt,
  pageBefore,
  referenceView,
  snapshotView,
  turnStarts,
  turnStateAt,
  type ColdOpenOrder,
  type Divergence,
  type FoldView,
  type LogRow,
} from "@/pages/workspace/chat/data/convergence";

import geoDashboard from "@/tests/fixtures/chat-convergence/3f0f77f4-b296-4c47-bdf3-3dd55ce36256/log.json";
import geoDashboardDoc from "@/tests/fixtures/chat-convergence/3f0f77f4-b296-4c47-bdf3-3dd55ce36256/doc.json";
import sovDashboard from "@/tests/fixtures/chat-convergence/c1d571f0-5ee6-4a08-bb3a-529302609945/log.json";
import sovDashboardDoc from "@/tests/fixtures/chat-convergence/c1d571f0-5ee6-4a08-bb3a-529302609945/doc.json";
import qaScenarios from "@/tests/fixtures/chat-convergence/51969efe-5321-4341-bacf-604bfad5f914/log.json";
import qaScenariosDoc from "@/tests/fixtures/chat-convergence/51969efe-5321-4341-bacf-604bfad5f914/doc.json";
import qaRerun from "@/tests/fixtures/chat-convergence/11621878-7b8a-4ba6-8478-ee54378fa7af/log.json";
import qaRerunDoc from "@/tests/fixtures/chat-convergence/11621878-7b8a-4ba6-8478-ee54378fa7af/doc.json";
import sameWords from "@/tests/fixtures/chat-convergence/afcf4cf2-601e-40ae-8521-32de496593dd/log.json";
import sameWordsDoc from "@/tests/fixtures/chat-convergence/afcf4cf2-601e-40ae-8521-32de496593dd/doc.json";
import lateStop from "@/tests/fixtures/chat-convergence/52f6f441-d4f3-4cf6-a3eb-8b58b6f21fc8/log.json";
import lateStopDoc from "@/tests/fixtures/chat-convergence/52f6f441-d4f3-4cf6-a3eb-8b58b6f21fc8/doc.json";
import threeStops from "@/tests/fixtures/chat-convergence/ada0e7f2-3ecf-4f98-911a-21caf94591fa/log.json";
import threeStopsDoc from "@/tests/fixtures/chat-convergence/ada0e7f2-3ecf-4f98-911a-21caf94591fa/doc.json";
import granite from "@/tests/fixtures/chat-convergence/5e1c988b-de50-468d-bb77-9a92f5f5c00f/log.json";
import graniteDoc from "@/tests/fixtures/chat-convergence/5e1c988b-de50-468d-bb77-9a92f5f5c00f/doc.json";
import stopRace from "@/tests/fixtures/chat-convergence/6ba98e93-a741-4ff4-b7ad-156d9af92e95/log.json";
import stopRaceDoc from "@/tests/fixtures/chat-convergence/6ba98e93-a741-4ff4-b7ad-156d9af92e95/doc.json";
import inFlight from "@/tests/fixtures/chat-convergence/3bb12bb9-4e78-415c-a480-264f134f03a0/log.json";
import inFlightDoc from "@/tests/fixtures/chat-convergence/3bb12bb9-4e78-415c-a480-264f134f03a0/doc.json";
import qaKnowledge from "@/tests/fixtures/chat-convergence/19c4ed5f-450c-48dc-b20d-540e631cfc37/log.json";
import qaKnowledgeDoc from "@/tests/fixtures/chat-convergence/19c4ed5f-450c-48dc-b20d-540e631cfc37/doc.json";
import qaCompaction from "@/tests/fixtures/chat-convergence/99c0f6c6-aa5b-490d-8f47-1ed797877d98/log.json";
import qaCompactionDoc from "@/tests/fixtures/chat-convergence/99c0f6c6-aa5b-490d-8f47-1ed797877d98/doc.json";

// --- the log's own shapes -------------------------------------------------------

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;

function eventOf(row: LogRow): Record<string, unknown> | null {
  const payload = row.payload;
  if (!isRecord(payload)) return null;
  if (typeof payload.event_type === "string") return payload;
  return isRecord(payload.payload) ? payload.payload : null;
}

/** The request ids the log re-announces AFTER resolving: a `permission.request`
 *  row whose `request_id` already has a `permission.resolved` row at a lower
 *  sequence. The exact shape of the ask class, read off the log itself. */
export function reannouncedAsks(log: readonly LogRow[]): Set<string> {
  const resolvedAt = new Map<string, number>();
  const out = new Set<string>();
  for (const row of log) {
    const event = eventOf(row);
    const requestId = event?.request_id;
    if (typeof requestId !== "string") continue;
    if (row.kind === "permission.resolved") {
      if (!resolvedAt.has(requestId)) resolvedAt.set(requestId, row.seq);
    } else if (row.kind === "permission.request") {
      const resolved = resolvedAt.get(requestId);
      if (resolved !== undefined && resolved < row.seq) out.add(requestId);
    }
  }
  return out;
}

/** FNV-1a over `${seq}:${kind}|` per row: the guard that the fixture on disk is
 *  the log that was pulled, since both sides of the invariant fold the same
 *  file and a corrupted row would converge with itself. */
export function logDigest(log: readonly LogRow[]): string {
  let hash = 0x811c9dc5;
  for (const row of log) {
    for (const byte of new TextEncoder().encode(`${row.seq}:${row.kind}|`)) {
      hash = Math.imul(hash ^ byte, 0x01000193) >>> 0;
    }
  }
  return `0x${hash.toString(16).padStart(8, "0")}`;
}

// --- the classes -----------------------------------------------------------------

interface ClassContext {
  reannounced: ReadonlySet<string>;
  live: FoldView;
  snapshot: FoldView;
}

const turnIdOf = (path: string): string | null => /^turn\[\d+:([^\]]+)\]/.exec(path)?.[1] ?? null;

/** Whether the turn a path names carries, in either view, a permission part
 *  for an ask the log re-announced after resolving. */
function turnCarriesReannounced(path: string, ctx: ClassContext): boolean {
  const turnId = turnIdOf(path);
  if (!turnId) return false;
  return [ctx.live, ctx.snapshot].some((view) =>
    view.turns
      .find((turn) => turn.id === turnId)
      ?.parts.some((part) => part.kind === "permission" && ctx.reannounced.has(part.requestId)),
  );
}

/** Whether a view's composer reading stands on a re-announced ask: its last
 *  turn holds one, pending and prompting. */
function composerOnReannounced(view: FoldView, ctx: ClassContext): boolean {
  const last = view.turns[view.turns.length - 1];
  return Boolean(
    last?.parts.some(
      (part) =>
        part.kind === "permission" &&
        part.status === "pending" &&
        part.prompting === true &&
        ctx.reannounced.has(part.requestId),
    ),
  );
}

const CLASSES: Array<{
  label: string;
  cause: string;
  match: (d: Divergence, ctx: ClassContext) => boolean;
}> = [
  {
    label: "re-announced ask folded without its resolution",
    cause:
      "the mirror re-publishes `permission.request …-prompting` for an ask the policy already " +
      "resolved (3f0f77f4 1018→1082, c1d571f0 589→618); a page starting at the next turn folds " +
      "the re-announce alone and offers a card whose click is a 409 ask_already_answered",
    match: (d, ctx) => {
      if (d.path === "pendingAsk" || d.path === "awaitsResponse") {
        return composerOnReannounced(ctx.live, ctx) || composerOnReannounced(ctx.snapshot, ctx);
      }
      // Only a part-level difference on the turn that holds the re-announced
      // ask: the ask's own fields, the part count it shifted, or a part
      // shifted below it.
      return /\.parts(\.length|\[\d+\]\.)/.test(d.path) && turnCarriesReannounced(d.path, ctx);
    },
  },
  {
    label: "prompt row appended after the window it belongs inside",
    cause:
      "CLOSED — a person's prompt rides the relay lane, so the socket window never carries it; " +
      "when the snapshot landed first, `CloudDataSource.readForward` handed the REST tail's rows " +
      "at or above the window's floor to `appendLive`, which put the prompt AFTER the assistant " +
      "turn that answered it, so `pendingAskKind` offered no card until a reload. The tail now " +
      "merges into the live segment in sequence order (`TranscriptWindow.mergeLive`)",
    match: (d, ctx) => {
      if (d.path !== "pendingAsk" && d.path !== "awaitsResponse") return false;
      // A cold open holds only the tail, so the comparison is over the turns
      // the shorter view holds against the same count off the longer's end.
      const all = [ctx.live.turns.map((t) => t.id), ctx.snapshot.turns.map((t) => t.id)];
      const count = Math.min(all[0].length, all[1].length);
      const [liveIds, snapshotIds] = all.map((ids) => ids.slice(-count));
      const sameSet = [...liveIds].sort().join("|") === [...snapshotIds].sort().join("|");
      const reordered = sameSet && liveIds.join("|") !== snapshotIds.join("|");
      const tailIsPrompt = [liveIds, snapshotIds].some((ids) =>
        (ids[ids.length - 1] ?? "").startsWith("usr:"),
      );
      return reordered && tailIsPrompt;
    },
  },
  {
    label: "message waiting below a cut page",
    cause:
      "OPEN where a page is cut: a message sent while a turn ran stands below that turn until " +
      "the box takes it, and its prompt row sits under the cut page's floor, so the view that " +
      "says it has older turns it did not load does not hold it either",
    match: (d, ctx) => /^turn\[\d+:usr:[^\]]+\] missing from live$/.test(d.path) && ctx.live.hasOlder === true,
  },
  {
    label: "relayed prompt differs from its echo",
    cause:
      "CLOSED — a tab that heard the send as a `user_message` relay kept the synthetic " +
      "`usr:…-text` part and `running` status where a fresh open folded the box's echo " +
      "(`prt_…`, `done`). A machine row folded after a prompt now marks it taken live as it " +
      "does on a page, and the echo's part replaces the synthetic one in place under the " +
      "box's ids, whichever of the relay, the row and the echo lands first",
    match: (d) =>
      !/\.startedAt$/.test(d.path) &&
      (/^turn\[\d+:usr:/.test(d.path) ||
        /"usr:/.test(JSON.stringify([d.live, d.snapshot])) ||
        /\.author$/.test(d.path) ||
        (/\.parts\.length$/.test(d.path) && Math.min(Number(d.live), Number(d.snapshot)) === 0)),
  },
  {
    label: "first turn read from its middle",
    cause:
      "OPEN where a view's floor falls inside a turn: the tail page reaches down to the prompt " +
      "row its first turn began at within `limit * chat_page_turn_reach` rows, and past that " +
      "the page is cut and says so with `cut` (pinned by hand below); the socket window's " +
      "floor is wherever compaction left it. Either way the first loaded turn lacks its " +
      "opening rows — its start time, its status, its first parts — where the reference " +
      "holds it whole. Only the first loaded turn of a view can be cut, so only the first " +
      "turn of the shorter view is matched, on the fields its opening rows carry",
    match: (d) =>
      /^turn\[0:msg_[A-Za-z0-9]+\]\.(startedAt|status|completedAt|parts(\.length|\[\d+\]\.[a-zA-Z]+))$/.test(
        d.path,
      ),
  },
  {
    label: "prompt timestamps",
    cause:
      "OPEN for a prompt whose row and echo sit on different pages: a message sent while the " +
      "box was mid-turn is echoed turns later, so a page anchored above the echo but below the " +
      "row dates the turn by the echo's `time` where the reference reads the row's " +
      "`created_at`; and for the window-only region below the page floor, which holds echoes " +
      "and no rows. (Closed for the relay itself: it now carries the row's `at`)",
    match: (d) => /\.startedAt$/.test(d.path),
  },
  {
    label: "client-invented assistant turn",
    cause:
      "CLOSED — for a prompt recorded while the box was mid-turn the server anchors the page on " +
      "that prompt row, cutting the message the box was still writing, and its next part opened " +
      "an `assistant-…` turn with a made-up id for a message above the page. A window whose " +
      "oldest rows begin inside a machine message now reads the page below and folds the two " +
      "as one (`TranscriptWindow.opensMidMessage`), so the message is there from its opening",
    match: (d) => /^turn\[\d+:assistant-[a-z0-9]+\] missing from/.test(d.path),
  },
  {
    label: "client-invented error turn",
    cause:
      "OPEN for an `error` status: the fold adds an `assistant-error-…` card no row in the log " +
      "carries, named per fold, so two views of one log cannot match it up. The rule forbids " +
      "exactly this — it is a terminal state a client invented — and it is owned by the lane " +
      "that owns `recordError`",
    match: (d) => /^turn\[\d+:assistant-error-[a-z0-9]+\] missing from/.test(d.path),
  },
  {
    label: "background job left running on the live tab",
    cause:
      "CLOSED — a job the box backgrounded and a Stop then aborted (51969efe: `sleep 60` at " +
      "104, stopped at 116): a fresh open folded the job's card settled (`cancelled`, its step " +
      "`error`) while the tab that folded the same rows live kept it running. The turn's " +
      "`idle` now settles the live fold the way a cold open's replay does",
    match: (d) =>
      /^turn\[\d+:bgcard:bgjob:[^\]]+\]\.(status|parts\[\d+\]\.(state|streaming))$/.test(d.path),
  },
];

/** The class a divergence belongs to, or null for one nobody has named. */
export function classify(d: Divergence, ctx: ClassContext): string | null {
  return CLASSES.find((c) => c.match(d, ctx))?.label ?? null;
}

interface Fixture {
  name: string;
  log: LogRow[];
  /** The lowest sequence the retained socket window held when pulled. */
  windowFrom: number;
  /** What was pulled: row count, sequence bounds and the `(seq, kind)` digest. */
  integrity: { rows: number; first: number; last: number; digest: string };
  /** The classes this fixture shows today, per cold-open order. */
  known: Record<ColdOpenOrder, string[]>;
  /** Set when the chat was pulled for a shape other than the re-announced
   *  ask, so the corpus does not demand that class of it. */
  pulledFor?: string;
  /** The classes a cold open at the very end may show against the reference,
   *  where they differ from `known`. */
  atEnd?: Partial<Record<ColdOpenOrder, string[]>>;
}

const ASK = "re-announced ask folded without its resolution";
const STAMPS = "prompt timestamps";
const CUT = "first turn read from its middle";
const WAITING = "message waiting below a cut page";
const ERRORCARD = "client-invented error turn";

const FIXTURES: Fixture[] = [
  {
    name: "3bb12bb9 (a stop across the cancel's round trip, 187 rows)",
    log: inFlight.rows as unknown as LogRow[],
    windowFrom: inFlightDoc.window.first_seq,
    integrity: { rows: 187, first: 1, last: 187, digest: "0x00ddbff4" },
    // Two Stops landing after dispatch (113 and 130). The drop was armed only
    // once the adapter's cancel came back, so between the stop's note and its
    // terminal the turn went on publishing: its `running`, its echo of the
    // message, the shell of the answer. The rows are kept as they were.
    //
    // The same log holds the wordless-part shape: a turn that thought (159/160)
    // and had begun its prose (161, an open text part) when the Stop landed
    // (162..164, aborted at 166/167). The box never settled that part - no
    // part.created, no message.completed - so the log holds its opening and not
    // one word of it. The tab that watched held the streamed tokens and showed
    // them as settled prose once the next turn began; a fresh open showed the
    // thought alone. The log decides: what was never recorded is not shown as
    // recorded.
    pulledFor:
      "a turn publishing across the cancel that was ending it, and a text part the Stop cut before the box settled it",
    known: {
      "rest-first": [],
      "socket-first": [],
    },
  },
  {
    name: "6ba98e93 (four stops racing dispatch, 86 rows)",
    log: stopRace.rows as unknown as LogRow[],
    windowFrom: stopRaceDoc.window.first_seq,
    integrity: { rows: 86, first: 1, last: 86, digest: "0x87912f1f" },
    // The four Stops of the live check, each pressed within 300 ms of the
    // send. One (seq 1) got past dispatch: the server's `prompt.cancelled`
    // (2) and stop note (3..5) are followed by the box's own abort (6), the
    // harness's (7) — and then, before this was fixed, the whole re-run the
    // agent loop went on to do, tool call and paragraph and all. The other
    // three are the same race caught at different depths.
    pulledFor: "a Stop landing after the harness already had the prompt",
    // The `error` row at 65 draws the synthetic error turn, whose id is minted
    // per fold — so the two views name the same card differently. The class
    // the rule forbids, already owned; nothing about the stops diverges.
    known: {
      "rest-first": [ERRORCARD],
      "socket-first": [ERRORCARD],
    },
  },
  {
    name: "3f0f77f4 (weekly summary, 1264 rows)",
    log: geoDashboard.rows as unknown as LogRow[],
    windowFrom: geoDashboardDoc.window.first_seq,
    integrity: { rows: 1264, first: 1, last: 1264, digest: "0x4bc0440e" },
    // 398 is a prompt sent while the box was mid-turn (echoed at 829): a page
    // anchored to it cuts that turn, and a page above its row dates it by the
    // echo. The socket window opens at 77, inside a turn, below the page.
    // ASK never shows on a cold open now: a page reaches down to the turn's
    // prompt row and its resolution, and the window's rows fold in the
    // server's order around it.
    // The socket window opens at 77, inside the turn a person opened at 64.
    known: {
      "rest-first": [],
      "socket-first": [CUT, STAMPS],
    },
    // The REFERENCE — the whole log folded in order — still renders the
    // re-announce at 1082 as a pending ask (the fold's own bug, owned by the
    // ask-state lane), while a socket-first open folds it resolved: the two
    // differ on that turn's parts.
    atEnd: { "socket-first": [ASK, CUT, STAMPS] },
  },
  {
    name: "c1d571f0 (share-of-voice dashboard, 651 rows)",
    log: sovDashboard.rows as unknown as LogRow[],
    windowFrom: sovDashboardDoc.window.first_seq,
    integrity: { rows: 651, first: 1, last: 651, digest: "0x96f51901" },
    // The re-announce at 618 sits thirty rows above its resolution (589), so
    // every page the sample opens on still holds both; the class shows on this
    // chat only through a window that starts between them (see the cold-open
    // test at the end, which pins it by hand).
    // The socket window opens at 2, inside the first turn, below the page.
    known: {
      "rest-first": [],
      "socket-first": [CUT, STAMPS],
    },
  },
  {
    name: "51969efe (qa-scenarios, rows 91..150)",
    log: qaScenarios.rows as unknown as LogRow[],
    windowFrom: qaScenariosDoc.window.first_seq,
    integrity: { rows: 60, first: 91, last: 150, digest: "0x1fe59492" },
    // One page of the Playwright run's chat, as the mirror recorded it: a Stop
    // note (116..118) landing inside an assistant message still streaming,
    // a policy-resolved ask (101/103), and at 133 the adapter's synthetic
    // <system-reminder> part leading the person's message at 132 — the
    // steering shape the older corpora predate. The page starts inside a
    // turn (91 is a status row), so the first turn is read from its middle.
    pulledFor: "a synthetic part leading a user message, and a Stop note mid-stream",
    known: {
      "rest-first": [],
      "socket-first": [],
    },
  },
  {
    name: "11621878 (qa-rerun, 292 rows, the detector's own window)",
    log: qaRerun.rows as unknown as LogRow[],
    windowFrom: qaRerunDoc.window.first_seq,
    integrity: { rows: 292, first: 1, last: 292, digest: "0x281c7074" },
    // The window the browser's detector exported when it fired on the live
    // stack: four Stops (2..4, 10..12, 169..171, 252..254 — the server's own
    // rows, which no box publishes, so a tab folding live hears only the
    // relay), three answers recorded by the server with the member's name
    // (76, 113, 148 — 113 by a second viewer), and a person saying the same
    // words twice (1, stopped before any box took it; 168, echoed at 172).
    pulledFor: "Stop notes and named answers that reach a live tab only by a read-back",
    // The Stop notes and the named answers converge: the tab reads them back
    // on the relay. What is left is the two classes above — the assistant
    // turn a page invents for a message above it, and a prompt dated by its
    // echo — which the detector logged on this very chat.
    // (The `startedAt` the detector logged on this chat — an echo dated by
    // an earlier saying of the same words — converges now that an echo is
    // matched to its saying by its place in the transcript.)
    known: {
      "rest-first": [],
      "socket-first": [STAMPS],
    },
    atEnd: { "rest-first": [], "socket-first": [STAMPS] },
  },
  {
    name: "afcf4cf2 (same words said twice, 252 rows, the detector's window)",
    log: sameWords.rows as unknown as LogRow[],
    windowFrom: sameWordsDoc.window.first_seq,
    integrity: { rows: 252, first: 1, last: 252, digest: "0xd6c856d3" },
    // A finished chat, cold-loaded: the person said one thing at 1 and again
    // at 42, and another at 26 and again at 61, each echoed by the box a row
    // later and stopped. The 200-row page holds the prompt rows from 61 up and
    // the socket window holds every echo, so the row at 61 met two echoes of
    // its words — 27 and 62 — and took the first, and the older page's row at
    // 26 was left the second: the turn at 62 dated by the saying at 26. The
    // scrub keeps equal texts equal and different ones different, since that
    // is the whole shape.
    pulledFor: "two sayings of the same words, each with its own echo",
    // The cross-dating is closed. What a socket-first open still shows is the
    // window's own floor: below the page the window holds echoes and no prompt
    // rows, so those turns are dated by the echo and the first one is read
    // from its middle — the two classes every windowed chat shows.
    known: {
      "rest-first": [],
      "socket-first": [CUT, STAMPS],
    },
  },
  {
    name: "52f6f441 (two Stops one turn apart, 45 rows, the detector's window)",
    log: lateStop.rows as unknown as LogRow[],
    windowFrom: lateStopDoc.window.first_seq,
    integrity: { rows: 45, first: 1, last: 45, digest: "0xfe0d8c57" },
    // A Stop mid-tool (20..22) and, one turn later, a Stop mid-prose whose
    // rows (42..44) land between the text part's `part.started` (41) and its
    // `part.created` (45). The detector's hit on it — `parts.length` live 2 vs
    // snapshot 1 — was the part the token frames were still writing, which no
    // durable fold can hold; the folds themselves agree at every row.
    pulledFor: "a Stop note landing inside a streaming text part, after an earlier Stop",
    known: {
      "rest-first": [],
      "socket-first": [],
    },
  },
  {
    name: "ada0e7f2 (three Stops: mid-tool, mid-thought, mid-prose; 68 rows)",
    log: threeStops.rows as unknown as LogRow[],
    windowFrom: threeStopsDoc.window.first_seq,
    integrity: { rows: 68, first: 1, last: 68, digest: "0x6ef456b2" },
    // The chat a browser probe read an "empty" first Stop notice on. The rows
    // fold to three notes with their sentence however they are met; the empty
    // reading was `innerText` on a block `content-visibility: auto` had taken
    // out of layout (threeStopNotices.test.tsx). Kept in the net for the
    // shape: a Stop under a failed tool group, one inside an open reasoning
    // part (40..42 between 39 and 43), one inside 3,000 characters of prose.
    pulledFor: "three Stop notes, each landing inside a different kind of open part",
    known: {
      "rest-first": [],
      "socket-first": [],
    },
  },
  {
    name: "19c4ed5f (the knowledge session the live detector fired on, 173 rows)",
    log: qaKnowledge.rows as unknown as LogRow[],
    windowFrom: qaKnowledgeDoc.window.first_seq,
    integrity: { rows: 173, first: 1, last: 173, digest: "0x28fe94b3" },
    // Four turns on the live stack — write a notes file, summarise it, promote
    // the summary to the knowledge base, share the item — with three asks, each
    // resolved (39/40, 122/123, 160/161), and the box's own file.edited /
    // file.watcher.updated rows. The runtime detector reported this log as
    // `n0: 1, n: 173` twice while the fourth turn was still streaming, and a
    // fresh open of the same chat afterwards folded identically.
    pulledFor: "a knowledge session the live detector reported diverging at n=173",
    known: {
      "rest-first": [],
      "socket-first": [],
    },
  },
  {
    name: "99c0f6c6 (the compaction drives the live detector fired on, 221 rows)",
    log: qaCompaction.rows as unknown as LogRow[],
    windowFrom: qaCompactionDoc.window.first_seq,
    integrity: { rows: 221, first: 1, last: 221, digest: "0xeddb7c51" },
    // Two drives at the compaction threshold: a 220-line paste and a 400-line
    // file read back whole, 137 s and 32 s of streaming. The detector reported
    // `n: 130` once and `n: 221` three times, each while a turn streamed.
    pulledFor: "long pastes and whole-file reads the live detector reported diverging at n=130 and n=221",
    known: {
      "rest-first": [],
      "socket-first": [],
    },
  },
  {
    name: "5e1c988b (a message sent inside the stopped turn's last rows, 372 rows)",
    log: granite.rows as unknown as LogRow[],
    windowFrom: graniteDoc.window.first_seq,
    integrity: { rows: 372, first: 1, last: 372, digest: "0xdf19709f" },
    // A turn stopped mid-prose after it had thought (reasoning 163/164, prose
    // from 165), and the person's next message recorded at 170 — BEFORE the
    // stopped turn's last rows landed (171 aborted, 172/173 the prose settled,
    // 174 completed). The server anchors the 200-row tail on that message, so
    // the page held the stopped message's end and none of its opening: a
    // fresh open showed it with its prose alone and no start time, where a tab
    // that watched had the thought above it. The log holds the thought; the
    // fresh open was the side that was wrong.
    pulledFor: "a person's message recorded inside the rows of the machine message above it",
    // What a socket-first open still shows is the window's own floor, as on
    // every windowed chat: below the page it holds echoes and no prompt rows.
    known: {
      "rest-first": [],
      "socket-first": [CUT, STAMPS],
    },
    atEnd: { "socket-first": [CUT, STAMPS] },
  },
];

const ORDERS: ColdOpenOrder[] = ["rest-first", "socket-first"];

/** A bounded, deterministic sample of `n`: every turn start, the rows around
 *  every ask and its resolution, and the log's end. */
function sampleN(log: LogRow[]): number[] {
  const last = log[log.length - 1].seq;
  const picks = new Set<number>([last]);
  for (const start of turnStarts(log)) picks.add(start);
  for (const row of log) {
    if (row.kind.startsWith("permission.") || row.kind.startsWith("question.")) {
      picks.add(row.seq);
      picks.add(Math.min(row.seq + 1, last));
    }
  }
  return [...picks].sort((a, b) => a - b);
}

/** The n0 a cold open could have happened at before `n`: the turn start
 *  nearest `n`, a few turns back, one row back, and the very first row. */
function sampleN0(log: LogRow[], n: number): number[] {
  const starts = turnStarts(log).filter((seq) => seq < n);
  const picks = new Set<number>([log[0].seq]);
  if (starts.length) picks.add(starts[starts.length - 1]);
  if (starts.length > 4) picks.add(starts[starts.length - 4]);
  picks.add(Math.max(n - 1, log[0].seq));
  return [...picks].filter((seq) => seq < n).sort((a, b) => a - b);
}

describe.each(FIXTURES)("convergence over $name", ({ log, windowFrom, integrity, known, atEnd, pulledFor }) => {
  const last = log[log.length - 1].seq;
  const reannounced = reannouncedAsks(log);

  it("is the log that was pulled", () => {
    expect({
      rows: log.length,
      first: log[0].seq,
      last,
      digest: logDigest(log),
    }).toEqual(integrity);
    if (!pulledFor) {
      expect(reannounced.size, "the ask class the corpus was pulled for").toBeGreaterThan(0);
    }
  });

  it.each(ORDERS)("a cold open (%s) at the end agrees with the reference", async (order) => {
    const reference = await referenceView(log, last);
    const fresh = await snapshotView(log, last, { order, windowFrom });
    const ctx: ClassContext = { reannounced, live: fresh, snapshot: reference };
    const divergences = diffViews(fresh, reference);
    const unnamed = divergences.filter((d) => classify(d, ctx) === null);
    expect(unnamed, `unnamed divergences:\n${describeDivergences(unnamed)}`).toEqual([]);
    const found = new Set(divergences.map((d) => classify(d, ctx)));
    const allowed = atEnd?.[order] ?? known[order];
    for (const label of found) expect(allowed, `class at the end (${order})`).toContain(label);
  });

  it.each(ORDERS)(
    "a tab folding live from n0 shows what a fresh open (%s) at n shows",
    async (order) => {
      const converge = convergenceOf(log, { order, windowFrom });
      const found = new Map<string, string>();
      const unnamed: string[] = [];
      for (const n of sampleN(log)) {
        for (const n0 of sampleN0(log, n)) {
          const { divergences, live, snapshot } = await converge(n0, n);
          const ctx: ClassContext = { reannounced, live, snapshot };
          for (const d of divergences) {
            const label = classify(d, ctx);
            const where = `${order} n0=${n0} n=${n}: ${describeDivergences([d])}`;
            if (label === null) unnamed.push(where);
            else if (!found.has(label)) found.set(label, where);
          }
        }
      }
      expect(unnamed.slice(0, 12), "divergences outside every named class").toEqual([]);
      const report = [...found.entries()].map(([label, where]) => `${label}\n  ${where}`).join("\n");
      expect([...found.keys()].sort(), `classes found:\n${report}`).toEqual([...known[order]].sort());
    },
    180_000,
  );
});

describe("a turn longer than the page's reach", () => {
  // 3f0f77f4: the turn a person opened at 193 runs past 700, with two more
  // messages (398, 611) sent while it ran. A page of 8 rows
  // reaches 32 rows down for its prompt row; opened at 700 it does not get
  // there, so the page is cut and says so, and the view it gives holds a turn
  // from its middle where the reference holds it whole.
  const log = geoDashboard.rows as unknown as LogRow[];

  it("is cut, and the page says so — a server from before pages were kept outside a message", async () => {
    // 698..705 opens after the box's message.created at 693: the turn's parts
    // without the message they belong to.
    const page = pageBefore(eventsUpTo(log, 705), null, 8, undefined, null);
    expect(page.cut).toBe(true);
    expect(page.items.length).toBe(8);
    expect(page.items[0].kind).not.toBe("prompt");
    const whole = pageBefore(eventsUpTo(log, 705), null, 8, 100, null);
    expect(whole.cut).toBe(false);
    expect(whole.items[0].seq).toBe(611);
    const reference = await referenceView(log, 705);
    const fresh = await snapshotView(log, 705, {
      order: "rest-first",
      windowFrom: 698,
      pageRows: 8,
      serverMessageReach: null,
    });
    // The page opens on the box's message at 693. The reference holds the
    // message said at 611 below it: it was sent while the turn begun at 193
    // ran, and it waits behind that turn until the box takes it at 726.
    const lastAnswer = reference.turns.filter((turn) => turn.author === "assistant").at(-1);
    expect(fresh.turns[0]?.id).toBe(lastAnswer?.id);
    expect(reference.turns.at(-1)?.id).toBe("usr:web-30d088f0-1");
    const ctx: ClassContext = { reannounced: reannouncedAsks(log), live: fresh, snapshot: reference };
    const classes = diffViews(fresh, reference).map((d) => classify(d, ctx));
    expect(classes.length).toBeGreaterThan(0);
    expect(new Set(classes)).toEqual(new Set([CUT, WAITING]));
  });

  it("the current server settles inside the turn's reach and says the page is cut", () => {
    // The same read. The boundary at 698 sits inside the message opened at 693;
    // the two reaches alternate down from there, and settle within a message's
    // reach of the descent's floor — still above the person's message the
    // turn began at (611), which is past the ceiling of `limit + limit × (4 +
    // 2)` rows. That close to its floor the rule cannot look a message under
    // the page, so it reports rather than reaches: whatever the page still
    // carries the end of, it says `cut`. Where exactly it settles is the
    // rule's own arithmetic, pinned by the fixture replay, not here.
    const page = pageBefore(eventsUpTo(log, 705), null, 8);
    expect(page.items[0].seq).toBeGreaterThan(611);
    expect(page.items[0].seq).toBeLessThan(693);
    expect(page.items[0].kind).not.toBe("prompt");
    expect(page.items.length).toBeLessThanOrEqual(8 + 8 * (4 + 2));
    expect(page.cut).toBe(true);
  });
});

describe("a window that starts between an ask's resolution and its re-announce", () => {
  // c1d571f0: per_0c3e3bc7d001YdngCqsLUGX2Mw resolved by policy at 589,
  // re-announced `prompting` at 618 inside the turn the person opened at 421.
  // A page reaches down to that prompt row only within its reach: an 8-row
  // page reaches 32 rows, so the page is cut at 612, folds the re-announce
  // alone, and offers a card the server would answer 409 to. With the prompt
  // row within reach the page carries the resolution and offers no ask.
  const log = sovDashboard.rows as unknown as LogRow[];

  it("offers an ask the reference shows as resolved when the page is cut", async () => {
    // A server from before pages were kept outside a message: its 8-row page
    // is cut at 612 and carries the re-announce without the resolution.
    expect(pageBefore(eventsUpTo(log, 619), null, 8, undefined, null).cut).toBe(true);
    const reference = await referenceView(log, 619);
    const fresh = await snapshotView(log, 619, {
      order: "rest-first",
      windowFrom: 614,
      pageRows: 8,
      serverMessageReach: null,
    });
    const ctx: ClassContext = { reannounced: reannouncedAsks(log), live: fresh, snapshot: reference };
    expect(ctx.reannounced).toContain("per_0c3e3bc7d001YdngCqsLUGX2Mw");
    const divergences = diffViews(fresh, reference);
    expect(divergences.map((d) => classify(d, ctx))).toContain(ASK);
    expect(fresh.pendingAsk, "the re-announce alone reads as an open ask").toBe("permission");
    expect(reference.pendingAsk, "the whole log reads it as resolved").toBeNull();
  });

  it("folds the resolution and offers no ask when the page reaches the prompt row", async () => {
    // The portal's page (200 rows) reaches 800: the prompt at 421 is within it.
    expect(pageBefore(eventsUpTo(log, 619), null, 8, 100).cut).toBe(false);
    const reference = await referenceView(log, 619);
    const fresh = await snapshotView(log, 619, { order: "rest-first", windowFrom: 614 });
    expect(fresh.pendingAsk, "the resolution below the window is on the page").toBeNull();
    expect(reference.pendingAsk).toBeNull();
  });
});

describe("the sequences the runtime detector reported", () => {
  // The detector's comparison is not the corpus sample's: its live side is the
  // portal's own tab, opened on a 200-row page and folded forward off the
  // socket, and its fresh side reads the WHOLE log in one page
  // (`checkConvergence` asks for `pageRows: n + 1`) and is handed the live
  // tab's turn word. These are the exact (n0, n) it logged on the live stack,
  // folded from the rows the same run left behind. They converge here, so what
  // the detector saw was not in the durable log: the live frames differed from
  // the rows.
  const cases: Array<{ name: string; log: LogRow[]; n0: number; n: number }> = [
    { name: "19c4ed5f n0=1 n=173", log: qaKnowledge.rows as unknown as LogRow[], n0: 1, n: 173 },
    { name: "99c0f6c6 n0=1 n=130", log: qaCompaction.rows as unknown as LogRow[], n0: 1, n: 130 },
    { name: "99c0f6c6 n0=1 n=221", log: qaCompaction.rows as unknown as LogRow[], n0: 1, n: 221 },
  ];

  it.each(cases)("$name folds the same live and fresh", async ({ log, n0, n }) => {
    const rows = eventsUpTo(log, n);
    const opened = await openAt(log, n0, { order: "rest-first" });
    let live: FoldView;
    try {
      await opened.push(rows.filter((row) => row.seq > n0));
      const word = turnStateAt(rows, n);
      if (word) await opened.setTurnState(word);
      const view = await opened.view();
      live = { ...view, turns: durableTurns(view.turns) };
    } finally {
      opened.close();
    }
    const fresh = await snapshotView(log, n, { pageRows: n + 1, turnState: live.turnState });
    const divergences = diffViews(live, fresh);
    expect(divergences, describeDivergences(divergences)).toEqual([]);
  }, 60_000);
});

describe("the class table", () => {
  it("names a cause for every class", () => {
    for (const c of CLASSES) expect(c.cause.length, c.label).toBeGreaterThan(20);
  });

  it("the ask matcher does not absorb a part difference on a turn without a re-announced ask", () => {
    const empty: FoldView = { turns: [], pendingAsk: null, awaitsResponse: false, turnState: null };
    const ctx: ClassContext = { reannounced: new Set(["per_x"]), live: empty, snapshot: empty };
    // Not on the first loaded turn, so the cut class does not take it either.
    const stray: Divergence = { path: "turn[1:msg_a].parts[1].status", live: "pending", snapshot: "resolved" };
    expect(classify(stray, ctx)).toBeNull();
    const onFirst: Divergence = { path: "turn[0:msg_a].parts[1].status", live: "pending", snapshot: "resolved" };
    expect(classify(onFirst, ctx), "a first turn's field is the cut class").toBe(CUT);
    const composer: Divergence = { path: "pendingAsk", live: "permission", snapshot: null };
    expect(classify(composer, ctx)).toBeNull();
  });
});
