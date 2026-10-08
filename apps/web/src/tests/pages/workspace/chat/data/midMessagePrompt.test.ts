// A person can speak while the machine is still writing: they press Stop and
// send their next message before the stopped turn's last rows have landed. The
// server records that message INSIDE the rows of the machine message above it,
// and anchors a page on it — so a page, a join of two pages, or a trim that
// treats it as a clean turn start holds that message's END and none of what it
// said. The granite chat (corpus `5e1c988b`) is the real one; these are the
// two shapes of it, and the two places the window itself makes the same cut.

import { describe, expect, it } from "vitest";

import {
  diffViews,
  liveView,
  openAt,
  referenceView,
  snapshotView,
  type FoldView,
  type LogRow,
} from "@/pages/workspace/chat/data/convergence";
import {
  createConversationFoldState,
  foldHarnessEvent,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import {
  TranscriptWindow,
  type TranscriptWindowAdapter,
  type WindowRow,
} from "@/pages/workspace/chat/data/transcriptWindow";

// --- a log, as the server records it ----------------------------------------------

let seq = 0;
const at = (n: number) => `2026-09-21T18:00:${String(n % 60).padStart(2, "0")}Z`;

function machine(role: string, event: HarnessEvent): LogRow {
  seq += 1;
  const eventId = `e${seq}`;
  const kind = String(event.event_type);
  return {
    id: `row-${seq}`,
    chat_id: "chat",
    seq,
    role,
    kind,
    event_id: eventId,
    payload: { kind, role, event_id: eventId, payload: { ...event, event_id: eventId, time: at(seq) } },
    created_at: at(seq),
  } as unknown as LogRow;
}

function prompt(clientId: string, text: string): LogRow {
  seq += 1;
  return {
    id: `row-${seq}`,
    chat_id: "chat",
    seq,
    role: "user",
    kind: "prompt",
    event_id: `usr:${clientId}`,
    payload: { kind: "prompt", text, client_id: clientId },
    created_at: at(seq),
  } as unknown as LogRow;
}

const part = (messageId: string, partId: string, type: "text" | "reasoning", text: string) => ({
  part_id: partId,
  message_id: messageId,
  type,
  text,
});
const started = (messageId: string, partId: string, type: "text" | "reasoning", text = ""): HarnessEvent => ({
  event_type: "part.started",
  message_id: messageId,
  part_id: partId,
  part_type: type,
  initial: { id: partId, messageID: messageId, type, text },
});
const created = (messageId: string, partId: string, type: "text" | "reasoning", text: string): HarnessEvent => ({
  event_type: "part.created",
  part: part(messageId, partId, type, text),
});

function echoOf(messageId: string, text: string): LogRow[] {
  return [
    machine("user", { event_type: "message.created", message_id: messageId, role: "user" }),
    machine("user", started(messageId, `${messageId}-words`, "text", text)),
  ];
}

function stopNote(id: string): LogRow[] {
  return [
    machine("system", { event_type: "message.created", message_id: id, role: "system" }),
    machine("system", {
      event_type: "part.created",
      part: { ...part(id, `${id}-part`, "text", "Stopped by Member One."), synthetic: true },
    }),
    machine("system", { event_type: "message.completed", message_id: id }),
  ];
}

/** A second, ordinary turn: what pushes the first one off the tail page. */
function laterTurn(n: number): LogRow[] {
  return [
    ...echoOf(`msg_u${n}`, `Message ${n}.`),
    machine("system", { event_type: "session.status_changed", status: "running" }),
    machine("assistant", { event_type: "message.created", message_id: `msg_a${n}`, role: "assistant" }),
    machine("assistant", created(`msg_a${n}`, `msg_a${n}-text`, "text", `Answer ${n}.`)),
    machine("assistant", { event_type: "message.completed", message_id: `msg_a${n}` }),
    machine("system", { event_type: "session.status_changed", status: "idle" }),
  ];
}

const STOPPED = "msg_stopped";

/** Stop while the thought is still being written; the next message lands
 *  before the thought settles. */
function stoppedMidThinking(): LogRow[] {
  seq = 0;
  return [
    prompt("web-1", "Write about granite."),
    ...echoOf("msg_u1", "Write about granite."),
    machine("system", { event_type: "session.status_changed", status: "running" }),
    machine("assistant", { event_type: "message.created", message_id: STOPPED, role: "assistant" }),
    machine("assistant", started(STOPPED, "prt_thought", "reasoning")),
    ...stopNote("stop-aaaa"),
    prompt("web-2", "Message 2."),
    machine("system", { event_type: "session.status_changed", status: "aborted", detail: null }),
    machine("assistant", started(STOPPED, "prt_thought", "reasoning", "Planning a piece on granite.")),
    machine("assistant", created(STOPPED, "prt_thought", "reasoning", "Planning a piece on granite.")),
    machine("assistant", { event_type: "message.completed", message_id: STOPPED }),
    ...laterTurn(2),
  ];
}

/** The granite chat's shape: the thought settled, the prose cut, the next
 *  message recorded between the prose's first settle and its last rows. */
function stoppedMidTextAfterThinking(): LogRow[] {
  seq = 0;
  return [
    prompt("web-1", "Write about granite."),
    ...echoOf("msg_u1", "Write about granite."),
    machine("system", { event_type: "session.status_changed", status: "running" }),
    machine("assistant", { event_type: "message.created", message_id: STOPPED, role: "assistant" }),
    machine("assistant", started(STOPPED, "prt_thought", "reasoning")),
    machine("assistant", created(STOPPED, "prt_thought", "reasoning", "Planning a piece on granite.")),
    machine("assistant", started(STOPPED, "prt_prose", "text")),
    ...stopNote("stop-bbbb"),
    machine("assistant", created(STOPPED, "prt_prose", "text", "Granite is a coarse-grained")),
    prompt("web-2", "Message 2."),
    machine("system", { event_type: "session.status_changed", status: "aborted", detail: null }),
    machine("assistant", started(STOPPED, "prt_prose", "text", "Granite is a coarse-grained")),
    machine("assistant", created(STOPPED, "prt_prose", "text", "Granite is a coarse-grained")),
    machine("assistant", { event_type: "message.completed", message_id: STOPPED }),
    ...laterTurn(2),
  ];
}

const kindsOf = (view: FoldView) => view.turns.find((turn) => turn.id === STOPPED)?.parts.map((p) => p.kind);

describe.each([
  ["stopped mid-thinking", stoppedMidThinking, ["thinking"]],
  ["stopped mid-text after thinking", stoppedMidTextAfterThinking, ["thinking", "text"]],
])("a message sent inside the stopped turn's last rows — %s", (_name, build, kinds) => {
  const log = build();
  const last = log[log.length - 1].seq;
  // A page small enough that its tail starts above the stopped message's
  // opening and is anchored on the message sent inside it.
  const PAGE = 8;

  it("the log places the next message inside the stopped message's rows", () => {
    const said = log.find((row) => row.event_id === "usr:web-2")?.seq ?? 0;
    const rowsOfStopped = log.filter((row) => JSON.stringify(row.payload).includes(STOPPED)).map((row) => row.seq);
    expect(Math.min(...rowsOfStopped)).toBeLessThan(said);
    expect(Math.max(...rowsOfStopped)).toBeGreaterThan(said);
  });

  // Two servers can answer the page read: one from before a page was kept
  // outside every message (`null`), and the current one. The portal does not
  // get to assume which — web and API are upgraded together, in either order.
  const SERVERS: Array<[string, number | null]> = [
    ["a server from before the page rule", null],
    ["a server that keeps a page outside every message", 2],
  ];

  it.each(SERVERS)("what the open costs in reads — %s", async (_server, serverMessageReach) => {
    const opened = await openAt(log, last, { order: "rest-first", pageRows: PAGE, windowFrom: last - 3, serverMessageReach });
    try {
      expect(kindsOf(await opened.view())).toEqual(kinds);
      const below = opened.reads.filter((read) => read.startsWith("before:"));
      // A server with no message reach splits the message, so the tab reads the
      // page below to make it whole. One with reach never does.
      if (serverMessageReach === null) expect(below.length).toBeGreaterThan(0);
      else expect(below).toEqual([]);
      expect(opened.reads.filter((read) => read === "tail")).toHaveLength(1);
    } finally {
      opened.close();
    }
  });

  it.each(
    SERVERS.flatMap(([server, reach]) =>
      (["rest-first", "socket-first"] as const).map((order) => [order, server, reach] as const),
    ),
  )(
    "a fresh open (%s, %s) shows the stopped message whole, as the tab that watched does",
    async (order, _server, serverMessageReach) => {
      const watched = await liveView(log, 1, last, { order, serverMessageReach });
      expect(kindsOf(watched)).toEqual(kinds);
      const fresh = await snapshotView(log, last, {
        order,
        pageRows: PAGE,
        windowFrom: last - 3,
        serverMessageReach,
      });
      expect(kindsOf(fresh), "what the fresh open shows of it").toEqual(kinds);
      expect(fresh.turns.filter((turn) => turn.id === STOPPED), "and shows it once").toHaveLength(1);
      const reference = await referenceView(log, last);
      const about = (d: { path: string }) => d.path.includes(STOPPED);
      expect(diffViews(fresh, reference).filter(about)).toEqual([]);
      expect(diffViews(watched, reference).filter(about)).toEqual([]);
    },
  );
});

// --- a message longer than the server's reach --------------------------------------

describe("a page the server says is cut inside a message", () => {
  // The current server reaches `limit × 2` rows below a page to get outside a
  // message, and no further: a longer one comes back from wherever the budget
  // ended, with `cut`. The reader keeps going until the message is whole.
  const LONG = "msg_long";
  function longMessage(): LogRow[] {
    seq = 0;
    return [
      prompt("web-1", "Write a lot."),
      ...echoOf("msg_u1", "Write a lot."),
      machine("system", { event_type: "session.status_changed", status: "running" }),
      machine("assistant", { event_type: "message.created", message_id: LONG, role: "assistant" }),
      machine("assistant", created(LONG, "prt_thought", "reasoning", "A plan.")),
      ...Array.from({ length: 40 }, (_, i) => machine("assistant", created(LONG, `prt_${i}`, "text", `Paragraph ${i}.`))),
      machine("assistant", { event_type: "message.completed", message_id: LONG }),
      machine("system", { event_type: "session.status_changed", status: "idle" }),
    ];
  }
  const partsOf = (view: FoldView) => view.turns.find((turn) => turn.id === LONG)?.parts.map((p) => p.id) ?? [];

  it("reads on below the cut page until the message has its opening and all it said", async () => {
    const log = longMessage();
    const last = log[log.length - 1].seq;
    const opened = await openAt(log, last, { order: "rest-first", pageRows: 4, windowFrom: last - 1 });
    try {
      const fresh = await opened.view();
      const reference = await referenceView(log, last);
      expect(partsOf(fresh)).toEqual(partsOf(reference));
      expect(partsOf(fresh)).toHaveLength(41);
      expect(fresh.turns.find((turn) => turn.id === LONG)?.startedAt).toBeDefined();
      expect(fresh.turns.filter((turn) => turn.id === LONG)).toHaveLength(1);
      expect(opened.reads.filter((read) => read.startsWith("before:")).length).toBeGreaterThan(0);
    } finally {
      opened.close();
    }
  });

  it("stops reading the moment the message is whole", async () => {
    const log = longMessage();
    const last = log[log.length - 1].seq;
    const opened = await openAt(log, last, { order: "rest-first", pageRows: 4, windowFrom: last - 1 });
    try {
      await opened.view();
      const below = opened.reads.filter((read) => read.startsWith("before:"));
      // Each read brings 4 rows plus the server's own 8-row reach: the 47 rows
      // under the tail take a handful of pages, never the whole budget of 8.
      expect(below.length).toBeLessThan(8);
      const lowest = Math.min(...below.map((read) => Number(read.slice("before:".length))));
      expect(lowest).toBeGreaterThan(1);
    } finally {
      opened.close();
    }
  });
});

// --- the window's own cuts ---------------------------------------------------------

interface Row extends WindowRow {
  role: string;
  event: HarnessEvent;
}

const adapter: TranscriptWindowAdapter<Row> = {
  createState: () => createConversationFoldState(),
  foldRows: (state, rows) => {
    for (const row of rows) foldHarnessEvent(state, row.event);
  },
  startsTurn: (row) => row.event.event_type === "message.created" && row.event.role === "user",
  pageMessageOf: (row) => {
    const named = row.event.message_id ?? (row.event.part as { message_id?: string } | undefined)?.message_id;
    return typeof named === "string" ? named : null;
  },
  opensPageMessage: (row) =>
    row.event.event_type === "message.created" ? String(row.event.message_id) : null,
  opensMachineMessage: (row) =>
    row.role === "assistant" && row.event.event_type === "message.created" ? String(row.event.message_id) : null,
  elidedTurnIds: () => [],
};

let n = 0;
const row = (role: string, event: HarnessEvent): Row => {
  n += 1;
  return { seq: n, eventId: `w${n}`, role, event };
};

/** u1 · the machine's message opens and speaks · u2 (sent inside it) · the
 *  message's last rows · the answer to u2. */
function splitMessage(): { above: Row[]; below: Row[] } {
  n = 0;
  const above = [
    row("user", { event_type: "message.created", message_id: "u1", role: "user" }),
    row("user", created("u1", "u1-words", "text", "First.")),
    row("assistant", { event_type: "message.created", message_id: "a1", role: "assistant" }),
    row("assistant", created("a1", "a1-thought", "reasoning", "A thought.")),
  ];
  const below = [
    row("user", { event_type: "message.created", message_id: "u2", role: "user" }),
    row("assistant", created("a1", "a1-prose", "text", "Cut prose.")),
    row("assistant", { event_type: "message.completed", message_id: "a1" }),
    row("user", created("u2", "u2-words", "text", "Second.")),
    row("assistant", { event_type: "message.created", message_id: "a2", role: "assistant" }),
    row("assistant", created("a2", "a2-prose", "text", "An answer.")),
  ];
  return { above, below };
}

const a1 = (window: TranscriptWindow<Row>) => window.turns().filter((turn) => turn.id === "a1");

describe("the window and a turn start inside a machine message", () => {
  it("says its oldest rows begin inside a message, and stops saying so once the page below joins", () => {
    const { above, below } = splitMessage();
    const window = new TranscriptWindow(adapter);
    window.appendLive(below);
    expect(window.opensMidMessage).toBe(true);
    window.prependOlder(above);
    expect(window.opensMidMessage).toBe(false);
  });

  it("joins the page below into ONE fold, so the message is one turn with all it said", () => {
    const { above, below } = splitMessage();
    const window = new TranscriptWindow(adapter);
    window.appendLive(below);
    window.prependOlder(above);
    expect(window.segmentCount).toBe(1);
    expect(a1(window)).toHaveLength(1);
    expect(a1(window)[0].parts.map((p) => p.kind)).toEqual(["thinking", "text"]);
  });

  it("still keeps a page that ends on a clean turn start as its own segment", () => {
    n = 0;
    const first = [
      row("user", { event_type: "message.created", message_id: "u1", role: "user" }),
      row("assistant", { event_type: "message.created", message_id: "a1", role: "assistant" }),
      row("assistant", created("a1", "a1-prose", "text", "Whole.")),
    ];
    const second = [
      row("user", { event_type: "message.created", message_id: "u2", role: "user" }),
      row("assistant", { event_type: "message.created", message_id: "a2", role: "assistant" }),
      row("assistant", created("a2", "a2-prose", "text", "Whole too.")),
    ];
    const window = new TranscriptWindow(adapter);
    window.appendLive(second);
    expect(window.opensMidMessage).toBe(false);
    window.prependOlder(first);
    expect(window.segmentCount).toBe(2);
  });

  it("does not trim at a turn start the message above runs through", () => {
    const { above, below } = splitMessage();
    const window = new TranscriptWindow(adapter);
    window.appendLive([...above, ...below]);
    // Over budget by enough that the first turn start far enough down is u2,
    // which sits inside a1. The cut passes it over; nothing later qualifies,
    // so the window stays whole rather than keep a1's end without a1.
    const { released } = window.releaseHead(7);
    expect(released).toBe(0);
    expect(a1(window)).toHaveLength(1);
    expect(a1(window)[0].parts.map((p) => p.kind)).toEqual(["thinking", "text"]);
  });

  it("still trims at a clean turn start", () => {
    n = 0;
    const rows = [
      row("user", { event_type: "message.created", message_id: "u1", role: "user" }),
      row("assistant", { event_type: "message.created", message_id: "a1", role: "assistant" }),
      row("assistant", created("a1", "a1-prose", "text", "Whole.")),
      row("user", { event_type: "message.created", message_id: "u2", role: "user" }),
      row("user", created("u2", "u2-words", "text", "Second.")),
      row("assistant", { event_type: "message.created", message_id: "a2", role: "assistant" }),
      row("assistant", created("a2", "a2-prose", "text", "Whole too.")),
    ];
    const window = new TranscriptWindow(adapter);
    window.appendLive(rows);
    expect(window.releaseHead(5).released).toBe(3);
    expect(window.turns().map((turn) => turn.id)).toEqual(["u2", "a2"]);
  });
});
