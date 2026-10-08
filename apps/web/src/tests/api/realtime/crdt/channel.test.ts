// The live channel against a stand-in server holding real Loro documents:
// convergence, one update in flight, coalescing, every way of falling behind
// and catching up, a new epoch, a refusal, and leaving the lane.

import { describe, expect, it, vi } from "vitest";

import { ACK_TIMEOUT_MS, EPHEMERAL_INTERVAL_MS, IDLE_RESYNC_MS, LiveDocChannel, NO_ANSWER, type LiveFallback, type PageEvents, type PageVisibility, type Timers, UNLOAD_STASH_TTL_MS, UNSAVED_AFTER_MS, answerDeadline, busyWait, unloadStashKey } from "@/api/realtime/crdt/channel";
import { LIVE_BUSY_RETRY, LIVE_OPEN_DEADLINE } from "@/lib/limits";
import { toBase64 } from "@/api/realtime/crdt/bytes";
import { SendBudget } from "@/api/realtime/crdt/sendBudget";
import * as LoroNode from "loro-crdt/nodejs";

import { FakeSocket, LiveServer, loroNode, settle } from "./liveServer";

const DOC = "sess-1";
const CHANNEL = `doc:chat_draft:${DOC}`;

/** Timers the test fires by hand. */
class ManualTimers implements Timers {
  private next = 1;
  readonly pending = new Map<number, { fn: () => void; ms: number }>();
  /** Every wait the channel may arm on an unanswered subscribe or hello. */
  readonly deadlines: Set<number>;
  constructor(random: () => number) {
    this.deadlines = new Set(Array.from({ length: LIVE_OPEN_DEADLINE.attempts + 1 }, (_, n) => answerDeadline(n, random)));
  }
  private clocks(): Set<number> {
    return new Set([ACK_TIMEOUT_MS, IDLE_RESYNC_MS, UNSAVED_AFTER_MS, EPHEMERAL_INTERVAL_MS, ...this.deadlines]);
  }
  /** Fire the deadline on the answer the channel waits for, as if it never came. */
  fireDeadline(): void {
    for (const [id, t] of [...this.pending]) {
      if (!this.deadlines.has(t.ms)) continue;
      this.pending.delete(id);
      t.fn();
    }
  }
  /** The deadlines armed now. */
  deadlineWaits(): number[] {
    return [...this.pending.values()].map((t) => t.ms).filter((ms) => this.deadlines.has(ms));
  }
  setTimeout(fn: () => void, ms: number): unknown {
    const id = this.next++;
    this.pending.set(id, { fn, ms });
    return id;
  }
  clearTimeout(handle: unknown): void {
    this.pending.delete(handle as number);
  }
  /** Fire every wait the channel arms after a busy answer: everything but
   *  its named clocks (the ack, the idle check, the hello, the unsaved bound,
   *  carets). The waits climb a ladder, so a test does not name each one. */
  fireRetries(): void {
    const clocks = this.clocks();
    for (const [id, t] of [...this.pending]) {
      if (clocks.has(t.ms)) continue;
      this.pending.delete(id);
      t.fn();
    }
  }
  /** The waits armed now, other than the named clocks (see `fireRetries`). */
  retryWaits(): number[] {
    const clocks = this.clocks();
    return [...this.pending.values()].map((t) => t.ms).filter((ms) => !clocks.has(ms));
  }
  /** Fire every timer armed for exactly `ms` (or all, with no argument). */
  fire(ms?: number): void {
    for (const [id, t] of [...this.pending]) {
      if (ms === undefined || t.ms === ms) {
        this.pending.delete(id);
        t.fn();
      }
    }
  }
}

interface Tab {
  socket: FakeSocket;
  channel: LiveDocChannel;
  timers: ManualTimers;
  phases: string[];
  fallbacks: LiveFallback[];
  notices: unknown[];
}

/** A tab's sessionStorage, kept by the test across the pages it opens. */
class TabStorage {
  readonly items = new Map<string, string>();
  get(key: string): string | null {
    return this.items.get(key) ?? null;
  }
  set(key: string, value: string): boolean {
    this.items.set(key, value);
    return true;
  }
  remove(key: string): void {
    this.items.delete(key);
  }
}

/** Who the tab's stash is kept for. */
const ACCOUNT = { userId: "usr_dana", orgId: "org_a" };

function tab(
  server: LiveServer,
  name: string,
  opts: {
    loadLoro?: () => Promise<typeof loroNode>;
    storage?: TabStorage;
    page?: EventTarget;
    account?: { userId: string; orgId: string } | null;
    random?: () => number;
    visibility?: PageVisibility;
  } = {},
): Tab {
  const socket = server.socket(name);
  // A sliver of jitter unless a test asks for more: a wait rounds to its
  // rung, and one at the ladder's cap never equals a named clock.
  const random = opts.random ?? (() => 0.001);
  const timers = new ManualTimers(random);
  const channel = new LiveDocChannel({
    socket,
    loadLoro: opts.loadLoro ?? (() => Promise.resolve(loroNode)),
    docType: "chat_draft",
    docId: DOC,
    timers,
    budget: new SendBudget(() => null),
    storage: opts.storage ?? null,
    account: opts.account === undefined ? ACCOUNT : opts.account,
    pageEvents: (opts.page as unknown as PageEvents | undefined) ?? null,
    visibility: opts.visibility ?? null,
    random,
  });
  const phases: string[] = [];
  const fallbacks: LiveFallback[] = [];
  const notices: unknown[] = [];
  channel.listen({
    phase: (p, f) => {
      phases.push(p);
      if (f) fallbacks.push(f);
    },
    notice: (n) => notices.push(n),
  });
  channel.start();
  return { socket, channel, timers, phases, fallbacks, notices };
}

function text(t: Tab): string {
  return t.channel.doc?.getText("draft").toString() ?? "";
}

function type(t: Tab, at: number, s: string): void {
  const doc = t.channel.doc!;
  doc.getText("draft").insert(at, s);
  doc.commit({ origin: "local" });
  t.channel.localCommitted();
}

/** Deliver every socket's frames until nothing moves. */
async function pump(...tabs: Tab[]): Promise<void> {
  // Quiet for several rounds in a row: a chunked transfer hashes asynchronously,
  // so one empty inbox is not yet the end of the conversation.
  let quiet = 0;
  for (let i = 0; i < 200 && quiet < 4; i += 1) {
    await settle();
    if (tabs.every((t) => t.socket.inbox.length === 0)) {
      quiet += 1;
      continue;
    }
    quiet = 0;
    for (const t of tabs) await t.socket.deliver();
  }
}

describe("LiveDocChannel", () => {
  it("opens on the server's text and goes live", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "held by the chat");
    const a = tab(server, "a");
    await pump(a);
    expect(a.channel.phase).toBe("live");
    expect(text(a)).toBe("held by the chat");
    expect(a.channel.peer).toBeGreaterThan(1023);
  });

  it("two tabs typing at once converge on the server's text", async () => {
    const server = new LiveServer();
    const [a, b] = [tab(server, "a"), tab(server, "b")];
    await pump(a, b);
    type(a, 0, "alpha ");
    type(b, 0, "beta ");
    await pump(a, b);
    expect(text(a)).toBe(text(b));
    expect(text(a)).toBe(server.text(CHANNEL));
    expect(text(a).split(" ").sort()).toEqual(["", "alpha", "beta"]);
  });

  it("keeps one update in flight and coalesces what is typed meanwhile into the next", async () => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    type(a, 0, "a");
    type(a, 1, "b");
    type(a, 2, "c");
    await settle();
    expect(a.socket.sentUpdates()).toHaveLength(1);
    await pump(a);
    expect(a.socket.sentUpdates()).toHaveLength(2);
    expect(server.text(CHANNEL)).toBe("abc");
  });

  it("an update the server never acknowledges is recovered by a resync, not lost", async () => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    server.muteAcks = true;
    type(a, 0, "kept");
    await pump(a);
    server.muteAcks = false;
    const hellos = a.channel.helloCount;
    a.timers.fire(ACK_TIMEOUT_MS);
    await pump(a);
    expect(a.channel.helloCount).toBe(hellos + 1);
    expect(server.text(CHANNEL)).toBe("kept");
    // And nothing is left pending: the next edit goes straight out.
    type(a, 4, "!");
    await pump(a);
    expect(server.text(CHANNEL)).toBe("kept!");
  });

  it("a broadcast lost on the way is caught by the idle check", async () => {
    const server = new LiveServer();
    const [a, b] = [tab(server, "a"), tab(server, "b")];
    await pump(a, b);
    type(a, 0, "missed");
    await settle();
    await a.socket.deliver();
    b.socket.drop();
    expect(text(b)).toBe("");
    b.timers.fire(IDLE_RESYNC_MS);
    await pump(a, b);
    expect(text(b)).toBe("missed");
    // Only the difference travelled.
    const last = b.socket.sent.filter((f) => f.t === "doc" && f.envelope.kind === "hello").at(-1);
    expect(last && last.t === "doc" && last.envelope.payload.vv_b64).toBeTruthy();
  });

  it("a reset from the server (its queue for this socket overflowed) catches up at once", async () => {
    const server = new LiveServer();
    const [a, b] = [tab(server, "a"), tab(server, "b")];
    await pump(a, b);
    type(a, 0, "missed");
    await settle();
    await a.socket.deliver();
    // The overflow dropped what was queued for b and left a reset in its place.
    b.socket.drop();
    b.socket.inbox.push({ t: "reset", reason: "overflow" });
    await pump(a, b);
    expect(text(b)).toBe("missed");
    expect(b.timers.pending.size).toBeGreaterThan(0);
  });

  it("an update that depends on one it missed asks for a resync and catches up", async () => {
    const server = new LiveServer();
    const [a, b] = [tab(server, "a"), tab(server, "b")];
    await pump(a, b);
    type(a, 0, "one ");
    await pump(a);
    b.socket.drop();
    type(a, 4, "two");
    await pump(a, b);
    expect(text(b)).toBe("one two");
  });

  it("survives a dropped socket with an unacknowledged edit and keeps its Loro peer", async () => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    const peer = a.channel.peer;
    server.muteAcks = true;
    type(a, 0, "typed offline");
    await settle();
    a.socket.disconnect();
    server.muteAcks = false;
    a.socket.reconnect();
    await pump(a);
    expect(server.text(CHANNEL)).toBe("typed offline");
    expect(a.channel.peer).toBe(peer);
  });

  it("rebases an edit the server never got onto a new epoch", async () => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    type(a, 0, "kept ");
    await pump(a);
    // Offline: the edit cannot leave, and meanwhile the history restarts.
    a.socket.disconnect();
    type(a, 5, "and new");
    server.rotate(CHANNEL);
    a.socket.reconnect();
    await pump(a);
    expect(text(a)).toBe("kept and new");
    expect(server.text(CHANNEL)).toBe("kept and new");
    expect(server.docOf(CHANNEL).epoch).toBe(2);
  });

  it("does not type an edit twice when the server committed it but the ack was lost before a new epoch", async () => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    type(a, 0, "kept ");
    await pump(a);
    // The edit reaches the server and commits; the ack dies with the socket.
    server.muteAcks = true;
    type(a, 5, "once");
    await pump(a);
    server.muteAcks = false;
    a.socket.disconnect();
    server.rotate(CHANNEL);
    a.socket.reconnect();
    await pump(a);
    expect(server.text(CHANNEL)).toBe("kept once");
    expect(text(a)).toBe("kept once");
  });

  it("never mixes another epoch's update into its document, and types nothing twice for it", async () => {
    const server = new LiveServer();
    const [a, b] = [tab(server, "a"), tab(server, "b")];
    await pump(a, b);
    // The history restarts on an empty draft (the new epoch holds no
    // operation at all), and b never hears about it.
    server.rotate(CHANNEL);
    b.socket.drop();
    await pump(a);
    expect(a.channel.docEpoch).toBe(2);
    // a's edit in the new epoch depends on nothing, so it would import
    // cleanly into b's old document if b let it.
    type(a, 0, "once");
    await settle();
    await a.socket.deliver();
    await pump(a, b);
    // b is still on the old epoch: the broadcast sent it back for the current
    // document rather than into its old one.
    expect(b.channel.docEpoch).toBe(2);
    await pump(a, b);
    expect(server.text(CHANNEL)).toBe("once");
    expect(text(b)).toBe("once");
    expect(text(a)).toBe("once");
  });

  it("a burst of broadcasts it cannot use asks for the document once, not once per broadcast", async () => {
    const server = new LiveServer();
    const [a, b] = [tab(server, "a"), tab(server, "b")];
    await pump(a, b);
    server.rotate(CHANNEL);
    b.socket.drop();
    await pump(a);
    const hellos = b.channel.helloCount;
    for (let i = 0; i < 6; i += 1) {
      type(a, 0, `${i}`);
      await settle();
      await a.socket.deliver();
    }
    // Six broadcasts from an epoch b has not reached, all queued before the
    // answer to its first hello.
    await b.socket.deliver();
    await pump(a, b);
    expect(b.channel.helloCount - hellos).toBeLessThanOrEqual(2);
    expect(b.channel.docEpoch).toBe(2);
    expect(text(b)).toBe(server.text(CHANNEL));
  });

  it("drops an update from an epoch it has already left", async () => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    server.seed(CHANNEL, "now");
    server.rotate(CHANNEL);
    await pump(a);
    expect(a.channel.docEpoch).toBe(2);
    const old = new LoroNode.LoroDoc();
    old.setPeerId(77n);
    old.getText("draft").insert(0, "from the past ");
    old.commit();
    a.socket.inbox.push({
      t: "doc",
      envelope: {
        doc_id: DOC,
        doc_type: "chat_draft",
        epoch: 1,
        peer_id: "p:other",
        seq: 0,
        kind: "crdt",
        payload: { t: "update", update_id: "u-old", data_b64: toBase64(old.export({ mode: "snapshot" })), loro_peer: 77 },
      },
    });
    await pump(a);
    expect(text(a)).toBe("now");
  });

  it("a refused update resets the copy to the server's and offers the lost text back", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "shared ");
    const a = tab(server, "a");
    await pump(a);
    server.failNext = { code: "crdt_rejected" };
    type(a, 7, "mine");
    await pump(a);
    expect(text(a)).toBe("shared ");
    expect(a.notices.at(-1)).toEqual({ message: "Your last edit could not be shared.", restorable: "mine" });
    expect(a.channel.phase).toBe("live");
  });

  it("a refused caret costs the tab nothing: no reset, no notice, edits keep landing", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "shared ");
    const a = tab(server, "a");
    await pump(a);
    const held = a.channel.doc;
    server.refuseCarets = true;
    a.channel.sendEphemeral(new Uint8Array([1, 2, 3]));
    a.timers.fire(0);
    await pump(a);
    expect(a.socket.sent.some((f) => JSON.stringify(f).includes('"ephemeral"'))).toBe(true);
    expect(a.channel.doc).toBe(held);
    expect(a.notices).toEqual([]);
    type(a, 7, "mine");
    await pump(a);
    expect(server.text(CHANNEL)).toBe("shared mine");
  });

  it("edits typed behind an unacknowledged one leave with the page", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const page = new EventTarget();
    const a = tab(server, "a", { storage: new TabStorage(), page });
    await pump(a);
    server.muteAcks = true;
    type(a, 5, " one");
    await pump(a);
    type(a, 9, " two");
    await pump(a);
    expect(server.text(CHANNEL)).toBe("draft one");
    page.dispatchEvent(new Event("pagehide"));
    expect(server.text(CHANNEL)).toBe("draft one two");
  });

  it("edits that could not leave with the page are folded in by the next page in the tab", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const storage = new TabStorage();
    const page = new EventTarget();
    const a = tab(server, "a", { storage, page });
    await pump(a);
    server.muteAcks = true;
    type(a, 5, " one");
    await pump(a);
    type(a, 9, " two");
    a.socket.disconnect();
    page.dispatchEvent(new Event("pagehide"));
    a.channel.close();
    expect(server.text(CHANNEL)).toBe("draft one");
    server.muteAcks = false;
    const next = tab(server, "a2", { storage, page: new EventTarget() });
    await pump(next);
    expect(server.text(CHANNEL)).toBe("draft one two");
    expect(text(next)).toBe("draft one two");
    expect(storage.items.size).toBe(0);
    // Read once: a later page does not replay it again.
    const third = tab(server, "a3", { storage, page: new EventTarget() });
    await pump(third);
    expect(text(third)).toBe("draft one two");
  });

  it("edits a page on the previous build stashed under the draft's old name are folded in", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const storage = new TabStorage();
    const page = new EventTarget();
    const a = tab(server, "a", { storage, page });
    await pump(a);
    a.socket.disconnect();
    type(a, 5, " kept");
    page.dispatchEvent(new Event("pagehide"));
    a.channel.close();
    // The previous build spelled the draft's channel doc:chat_workspace:<id>.
    const [key, raw] = [...storage.items][0]!;
    storage.remove(key);
    storage.set(unloadStashKey(ACCOUNT, `doc:chat_workspace:${DOC}`), raw);
    const next = tab(server, "a2", { storage, page: new EventTarget() });
    await pump(next);
    expect(server.text(CHANNEL)).toBe("draft kept");
    expect(storage.items.size).toBe(0);
  });

  it("edits the server will not take from this tab are dropped unseen, never offered back", async () => {
    // The page that stashed them belonged to someone whose session ended
    // without a sign-out; the next person in the tab must never see them.
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const storage = new TabStorage();
    const page = new EventTarget();
    const a = tab(server, "a", { storage, page });
    await pump(a);
    a.socket.disconnect();
    type(a, 5, " private");
    page.dispatchEvent(new Event("pagehide"));
    a.channel.close();
    server.failNext = { code: "crdt_rejected" };
    const next = tab(server, "b", { storage, page: new EventTarget() });
    const seen: string[] = [];
    next.channel.listen({ afterRemote: () => seen.push(text(next)) });
    await pump(next);
    expect(server.text(CHANNEL)).toBe("draft");
    expect(text(next)).toBe("draft");
    expect(seen.some((t) => t.includes("private"))).toBe(false);
    expect(next.notices).toEqual([]);
    expect(storage.items.size).toBe(0);
  });

  it("a stash left in one org is never replayed by a page in another, and waits for its own", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const storage = new TabStorage();
    const page = new EventTarget();
    const a = tab(server, "a", { storage, page });
    await pump(a);
    a.socket.disconnect();
    type(a, 5, " org a only");
    page.dispatchEvent(new Event("pagehide"));
    a.channel.close();
    expect(storage.items.size).toBe(1);

    // The same person, switched to another org in this tab.
    const elsewhere = tab(server, "b", {
      storage,
      page: new EventTarget(),
      account: { userId: ACCOUNT.userId, orgId: "org_b" },
    });
    await pump(elsewhere);
    expect(server.text(CHANNEL)).toBe("draft");
    expect(text(elsewhere)).toBe("draft");
    expect(storage.items.size).toBe(1);

    // Back in org A, the page there folds it in.
    const back = tab(server, "a2", { storage, page: new EventTarget() });
    await pump(back);
    expect(server.text(CHANNEL)).toBe("draft org a only");
    expect(storage.items.size).toBe(0);
  });

  it("keeps no stash for a channel that names no account", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const storage = new TabStorage();
    const page = new EventTarget();
    const a = tab(server, "a", { storage, page, account: null });
    await pump(a);
    a.socket.disconnect();
    type(a, 5, " unsent");
    page.dispatchEvent(new Event("pagehide"));
    a.channel.close();
    expect(storage.items.size).toBe(0);
  });

  it("a stash from an epoch the server has replaced is carried as one edit, shown and sent once", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const storage = new TabStorage();
    const page = new EventTarget();
    const a = tab(server, "a", { storage, page });
    await pump(a);
    a.socket.disconnect();
    type(a, 5, " kept");
    page.dispatchEvent(new Event("pagehide"));
    a.channel.close();
    // The history restarted while no page was open, somebody else's word in it.
    server.edit(CHANNEL, 5, " of ours");
    server.rotate(CHANNEL);
    const next = tab(server, "a2", { storage, page: new EventTarget() });
    await pump(next);
    expect(text(next)).toBe("draft kept of ours");
    expect(server.text(CHANNEL)).toBe("draft kept of ours");
    expect(next.notices).toEqual([]);
    expect(storage.items.size).toBe(0);
    // Settled: a later page carries nothing again.
    const third = tab(server, "a3", { storage, page: new EventTarget() });
    await pump(third);
    expect(server.text(CHANNEL)).toBe("draft kept of ours");
  });

  it("a stash from a visit nobody is coming back to is dropped", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const storage = new TabStorage();
    const page = new EventTarget();
    const a = tab(server, "a", { storage, page });
    await pump(a);
    a.socket.disconnect();
    type(a, 5, " stale");
    page.dispatchEvent(new Event("pagehide"));
    a.channel.close();
    const [key, raw] = [...storage.items][0]!;
    storage.set(key, JSON.stringify({ ...JSON.parse(raw), at: Date.now() - UNLOAD_STASH_TTL_MS - 1 }));
    const later = tab(server, "a2", { storage, page: new EventTarget() });
    await pump(later);
    expect(text(later)).toBe("draft");
    expect(server.text(CHANNEL)).toBe("draft");
    expect(storage.items.size).toBe(0);
  });

  it("a second reload before the server answered for the first page's edits keeps both pages' edits", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const storage = new TabStorage();
    const first = new EventTarget();
    const a = tab(server, "a", { storage, page: first });
    await pump(a);
    a.socket.disconnect();
    type(a, 5, " one");
    first.dispatchEvent(new Event("pagehide"));
    a.channel.close();
    // The second page syncs, but the server is too busy to take the replay.
    server.busyUpdates = 50;
    const second = new EventTarget();
    const b = tab(server, "a2", { storage, page: second });
    await pump(b);
    expect(server.text(CHANNEL)).toBe("draft");
    expect(storage.items.size).toBe(1);
    type(b, 0, "my ");
    b.socket.disconnect();
    second.dispatchEvent(new Event("pagehide"));
    b.channel.close();
    server.busyUpdates = 0;
    const c = tab(server, "a3", { storage, page: new EventTarget() });
    await pump(c);
    expect(server.text(CHANNEL)).toBe("my draft one");
    expect(text(c)).toBe("my draft one");
    expect(storage.items.size).toBe(0);
  });

  it("an answer for an earlier page's edits that is lost on the way is asked for again, and the channel lets go", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const storage = new TabStorage();
    const page = new EventTarget();
    const a = tab(server, "a", { storage, page });
    await pump(a);
    a.socket.disconnect();
    type(a, 5, " kept");
    page.dispatchEvent(new Event("pagehide"));
    a.channel.close();
    // The server takes the replay, and its ack never arrives.
    server.muteAcks = true;
    const next = tab(server, "a2", { storage, page: new EventTarget() });
    await pump(next);
    expect(server.text(CHANNEL)).toBe("draft kept");
    expect(next.channel.holdsUnacknowledged).toBe(true);
    expect(storage.items.size).toBe(1);
    server.muteAcks = false;
    next.timers.fire(IDLE_RESYNC_MS);
    await pump(next);
    expect(text(next)).toBe("draft kept");
    expect(server.text(CHANNEL)).toBe("draft kept");
    expect(next.channel.holdsUnacknowledged).toBe(false);
    expect(storage.items.size).toBe(0);
  });

  it("edits a server never gets to as they were written are typed again as one edit, never dropped", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const storage = new TabStorage();
    const page = new EventTarget();
    const a = tab(server, "a", { storage, page });
    await pump(a);
    a.socket.disconnect();
    type(a, 5, " kept");
    page.dispatchEvent(new Event("pagehide"));
    a.channel.close();
    // Busy for every one of the replay's tries, then free.
    server.busyUpdates = 10;
    const next = tab(server, "a2", { storage, page: new EventTarget() });
    for (let i = 0; i < 12; i += 1) {
      await pump(next);
      next.timers.fireRetries();
    }
    await pump(next);
    expect(server.busyUpdates).toBe(0);
    expect(text(next)).toBe("draft kept");
    expect(server.text(CHANNEL)).toBe("draft kept");
    expect(storage.items.size).toBe(0);
  });

  it("an epoch that changes while the server is answering for an earlier page's edits carries them onto it", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const storage = new TabStorage();
    const page = new EventTarget();
    const a = tab(server, "a", { storage, page });
    await pump(a);
    a.socket.disconnect();
    type(a, 5, " kept");
    page.dispatchEvent(new Event("pagehide"));
    a.channel.close();
    server.busyUpdates = 1;
    const next = tab(server, "a2", { storage, page: new EventTarget() });
    await pump(next);
    expect(server.text(CHANNEL)).toBe("draft");
    server.rotate(CHANNEL);
    await pump(next);
    next.timers.fireRetries();
    await pump(next);
    expect(text(next)).toBe("draft kept");
    expect(server.text(CHANNEL)).toBe("draft kept");
    expect(storage.items.size).toBe(0);
  });

  it("an earlier page's edits answered internal (a server that hit a lock timeout) are kept and sent again until taken", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const storage = new TabStorage();
    const page = new EventTarget();
    const a = tab(server, "a", { storage, page });
    await pump(a);
    a.socket.disconnect();
    type(a, 5, " kept");
    page.dispatchEvent(new Event("pagehide"));
    a.channel.close();
    server.failNext = { code: "internal", retry_after_ms: 500 };
    const next = tab(server, "a2", { storage, page: new EventTarget() });
    await pump(next);
    expect(server.text(CHANNEL)).toBe("draft");
    expect(storage.items.size).toBe(1);
    expect(next.notices).toEqual([]);
    next.timers.fireRetries();
    await pump(next);
    expect(server.text(CHANNEL)).toBe("draft kept");
    expect(text(next)).toBe("draft kept");
    expect(storage.items.size).toBe(0);
  });

  it("waits longer each time the server stays busy, never less than it asked, and starts over once it answers", async () => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    server.busyUpdates = 3;
    type(a, 0, "x");
    const waits: number[] = [];
    for (let i = 0; i < 3; i += 1) {
      await pump(a);
      waits.push(...a.timers.retryWaits());
      a.timers.fireRetries();
    }
    await pump(a);
    expect(server.text(CHANNEL)).toBe("x");
    // The fake server asks for 250 ms each time: the first wait is that, then the ladder climbs.
    expect(waits).toEqual([250, 500, 1000]);
    server.busyUpdates = 1;
    type(a, 1, "y");
    await pump(a);
    expect(a.timers.retryWaits()).toEqual([250]);
  });

  it("a reset offers back the edits the server never took, even when its ack named edits this tab has not seen", async () => {
    const server = new LiveServer();
    const [a, b] = [tab(server, "a"), tab(server, "b")];
    await pump(a, b);
    type(b, 0, "B");
    await pump(b);
    // b's broadcast never reaches a (a lost notification), so a's next ack
    // names an operation a does not hold.
    a.socket.inbox.length = 0;
    type(a, 0, "x");
    await pump(a);
    server.failNext = { code: "crdt_rejected" };
    type(a, 1, "y");
    await pump(a);
    expect(a.notices.at(-1)).toEqual({ message: "Your last edit could not be shared.", restorable: "y" });
  });

  it("a sync it cannot read is asked for again, and the tab still goes live", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "held");
    server.corruptNextSync = true;
    const a = tab(server, "a");
    await pump(a);
    a.timers.fireDeadline();
    await pump(a);
    expect(a.channel.phase).toBe("live");
    expect(text(a)).toBe("held");
  });

  it("a sync without a peer (a reader's) goes live with nothing to write as", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "read me");
    server.omitPeer = true;
    const a = tab(server, "a");
    await pump(a);
    expect(a.channel.phase).toBe("live");
    expect(text(a)).toBe("read me");
    expect(a.channel.peer).toBeNull();
  });

  it("a caret answered busy is dropped without asking for the document again", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "shared ");
    const a = tab(server, "a");
    await pump(a);
    const held = a.channel.doc;
    const hellos = (): number => a.socket.sent.filter((f) => JSON.stringify(f).includes('"hello"')).length;
    const before = hellos();
    server.busyCarets = true;
    a.channel.sendEphemeral(new Uint8Array([1, 2, 3]));
    a.timers.fire(0);
    await pump(a);
    a.timers.fire(500);
    await pump(a);
    expect(hellos()).toBe(before);
    expect(a.channel.doc).toBe(held);
  });

  it("unsent edits from an earlier page outlast a busy server", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "draft");
    const storage = new TabStorage();
    const page = new EventTarget();
    const a = tab(server, "a", { storage, page });
    await pump(a);
    a.socket.disconnect();
    type(a, 5, " kept");
    page.dispatchEvent(new Event("pagehide"));
    a.channel.close();
    server.busyUpdates = 5;
    const next = tab(server, "a2", { storage, page: new EventTarget() });
    await pump(next);
    for (let i = 0; i < 6; i += 1) {
      next.timers.fireRetries();
      await pump(next);
    }
    expect(server.text(CHANNEL)).toBe("draft kept");
    expect(text(next)).toBe("draft kept");
  });

  it("a busy server is retried after the hint it gave", async () => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    server.failNext = { code: "crdt_busy", retry_after_ms: 250 };
    type(a, 0, "later");
    await pump(a);
    expect(server.text(CHANNEL)).toBe("");
    a.timers.fire(250);
    await pump(a);
    expect(server.text(CHANNEL)).toBe("later");
  });

  it("a hello refused as busy is asked again after the hint, and the tab goes live", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "held");
    server.refuseHellos = 1;
    const a = tab(server, "a");
    await pump(a);
    expect(a.channel.phase).toBe("connecting");
    a.timers.fire(300);
    await pump(a);
    expect(a.channel.phase).toBe("live");
    expect(text(a)).toBe("held");
  });

  it("a live tab's resync refused as busy asks again after the hint", async () => {
    const server = new LiveServer();
    const [a, b] = [tab(server, "a"), tab(server, "b")];
    await pump(a, b);
    type(b, 0, "missed");
    await pump(b);
    a.socket.inbox.length = 0;
    server.refuseHellos = 1;
    a.timers.fire(IDLE_RESYNC_MS);
    await pump(a);
    expect(text(a)).toBe("");
    a.timers.fire(300);
    await pump(a);
    expect(text(a)).toBe("missed");
  });

  it("a hello the server never answers is asked again, and the tab goes live", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "held");
    server.ignoreHellos = 1;
    const a = tab(server, "a");
    await pump(a);
    expect(a.channel.phase).toBe("connecting");
    a.timers.fireDeadline();
    await pump(a);
    expect(a.channel.phase).toBe("live");
    expect(text(a)).toBe("held");
    // Live, the backstop is gone: it never fires a stray hello later.
    const hellos = a.channel.helloCount;
    a.timers.fireDeadline();
    expect(a.channel.helloCount).toBe(hellos);
  });

  it("a hello answer lost twice is asked again, each time after a longer jittered wait, and the tab goes live", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "held");
    server.ignoreHellos = 2;
    const a = tab(server, "a", { random: () => 0.5 });
    await pump(a);
    const first = a.timers.deadlineWaits();
    expect(first).toEqual([Math.round(LIVE_OPEN_DEADLINE.floorMs * 1.15)]);
    a.timers.fireDeadline();
    await pump(a);
    expect(a.channel.phase).toBe("connecting");
    expect(a.timers.deadlineWaits()).toEqual([Math.round(LIVE_OPEN_DEADLINE.floorMs * 2 * 1.15)]);
    a.timers.fireDeadline();
    await pump(a);
    expect(a.channel.phase).toBe("live");
    expect(text(a)).toBe("held");
  });

  it("a subscribe whose answer was lost is asked again, and the tab goes live", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "held");
    server.ignoreSubscribes = 1;
    const a = tab(server, "a");
    await pump(a);
    expect(a.channel.phase).toBe("connecting");
    expect(a.channel.helloCount).toBe(0);
    a.timers.fireDeadline();
    await pump(a);
    expect(a.channel.phase).toBe("live");
    expect(text(a)).toBe("held");
  });

  it("a socket that restarts mid-hello says hello again on the new socket, and the tab goes live", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "held");
    server.ignoreHellos = 1;
    const a = tab(server, "a");
    await pump(a);
    expect(a.channel.phase).toBe("connecting");
    a.socket.disconnect();
    a.socket.reconnect();
    await pump(a);
    expect(a.channel.phase).toBe("live");
    expect(text(a)).toBe("held");
  });

  it("a page shown again asks at once instead of waiting out its deadline", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "held");
    server.ignoreHellos = 1;
    const shown = Object.assign(new EventTarget(), { hidden: true });
    const a = tab(server, "a", { visibility: shown as unknown as PageVisibility });
    await pump(a);
    expect(a.channel.phase).toBe("connecting");
    shown.hidden = false;
    shown.dispatchEvent(new Event("visibilitychange"));
    await pump(a);
    expect(a.channel.phase).toBe("live");
    expect(text(a)).toBe("held");
  });

  it("a hello never answered falls back after the ladder's attempts, with the reason", async () => {
    const server = new LiveServer();
    server.ignoreHellos = Infinity;
    const a = tab(server, "a");
    await pump(a);
    for (let i = 0; i < LIVE_OPEN_DEADLINE.attempts - 1; i += 1) {
      a.timers.fireDeadline();
      await pump(a);
      expect(a.channel.phase).toBe("connecting");
    }
    a.timers.fireDeadline();
    await pump(a);
    expect(a.channel.phase).toBe("fallback");
    expect(a.fallbacks.map((f) => f.reason)).toEqual([NO_ANSWER]);
  });

  it("deadlines that pass while the page is hidden never make it give up", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "held");
    server.ignoreHellos = LIVE_OPEN_DEADLINE.attempts + 2;
    const shown = Object.assign(new EventTarget(), { hidden: true });
    const a = tab(server, "a", { visibility: shown as unknown as PageVisibility });
    await pump(a);
    for (let i = 0; i < LIVE_OPEN_DEADLINE.attempts + 2; i += 1) {
      a.timers.fireDeadline();
      await pump(a);
    }
    expect(a.channel.phase).toBe("live");
    expect(text(a)).toBe("held");
  });

  it("a live tab whose resync is never answered keeps asking and never falls back", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "held");
    const a = tab(server, "a");
    await pump(a);
    server.ignoreHellos = LIVE_OPEN_DEADLINE.attempts + 2;
    a.timers.fire(IDLE_RESYNC_MS);
    for (let i = 0; i < LIVE_OPEN_DEADLINE.attempts + 2; i += 1) {
      a.timers.fireDeadline();
      await pump(a);
    }
    expect(a.channel.phase).toBe("live");
    expect(a.fallbacks).toEqual([]);
  });

  it.each([["internal"], ["a_code_this_build_does_not_know"]])("an edit answered %s is sent again and lands", async (code) => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    server.failNext = { code };
    type(a, 0, "kept");
    await pump(a);
    expect(server.text(CHANNEL)).toBe("");
    a.timers.fireRetries();
    await pump(a);
    expect(server.text(CHANNEL)).toBe("kept");
    expect(text(a)).toBe("kept");
  });

  it.each([
    ["crdt_unsupported"],
    ["not_found"],
    ["a_code_this_build_does_not_know"],
  ])("a subscribe refused with %s falls back without loading Loro", async (code) => {
    const server = new LiveServer();
    server.refused.set(CHANNEL, code);
    const loadLoro = vi.fn(() => Promise.resolve(loroNode));
    const a = tab(server, "a", { loadLoro });
    await pump(a);
    expect(a.channel.phase).toBe("fallback");
    expect(a.fallbacks[0]?.reason).toBe(code);
    expect(loadLoro).not.toHaveBeenCalled();
  });

  it("answers a reload naming crdt_disabled by syncing again, staying live", async () => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    type(a, 0, "kept");
    await pump(a);
    const hellos = a.channel.helloCount;
    server.tell(CHANNEL, "reload", { epoch: 1, reason: "crdt_disabled" });
    await pump(a);
    expect(a.channel.phase).toBe("live");
    expect(a.channel.helloCount).toBeGreaterThan(hellos);
    expect(text(a)).toBe("kept");
  });

  it("stays live through an error naming crdt_disabled", async () => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    server.tell(CHANNEL, "error", { code: "crdt_disabled" });
    await pump(a);
    expect(a.channel.phase).toBe("live");
    expect(a.fallbacks).toEqual([]);
  });

  it("Loro failing to load falls back instead of hanging", async () => {
    const server = new LiveServer();
    const a = tab(server, "a", { loadLoro: () => Promise.reject(new Error("CSP")) });
    await pump(a);
    expect(a.channel.phase).toBe("fallback");
    expect(a.fallbacks[0]?.reason).toBe("load_failed");
  });

  it("a server that stops serving mid-session hands over the text and what it last confirmed", async () => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    type(a, 0, "confirmed");
    await pump(a);
    server.muteAcks = true;
    type(a, 9, " pending");
    await settle();
    server.unsupport(CHANNEL);
    await pump(a);
    expect(a.channel.phase).toBe("fallback");
    expect(a.fallbacks[0]).toEqual({
      reason: "crdt_unsupported",
      localText: "confirmed pending",
      ackedText: "confirmed",
      unacknowledged: " pending",
    });
  });

  // Skipped: pump() stops after four quiet settle rounds, and a chunked send
  // first awaits its sha256 off-thread, which a loaded runner can outlast.
  // Comes back once the rig waits on the channel's pending sends instead.
  it.skip("carries a large update and a large document in pieces", async () => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    const doc = a.channel.doc!;
    doc.getText("draft").insert(0, "x".repeat(40_000));
    doc.getText("draft").delete(1_000, 39_000);
    doc.commit({ origin: "local" });
    a.channel.localCommitted();
    await pump(a);
    expect(server.text(CHANNEL)).toBe("x".repeat(1_000));
    expect(a.socket.sentUpdates().some((u) => u.chunk !== undefined)).toBe(true);
    const b = tab(server, "b");
    await pump(a, b);
    expect(text(b)).toBe("x".repeat(1_000));
  });

  it("never sends more than its share of the socket's window", async () => {
    const server = new LiveServer();
    const socket = server.socket("tight");
    socket.limits = { frames_per_window: 8, bytes_per_window: 1 << 20, window_seconds: 10, max_frame_bytes: 1 << 21 };
    const timers = new ManualTimers(() => 0.001);
    const channel = new LiveDocChannel({
      socket,
      loadLoro: () => Promise.resolve(loroNode),
      docType: "chat_draft",
      docId: DOC,
      timers,
      budget: new SendBudget(() => socket.limits),
      account: null,
    });
    channel.start();
    const t: Tab = { socket, channel, timers, phases: [], fallbacks: [], notices: [] };
    await pump(t);
    for (let i = 0; i < 10; i += 1) {
      type(t, i, String(i));
      await pump(t);
    }
    // Half of eight frames per window: the rest wait on a timer, never dropped.
    expect(socket.sentUpdates().length).toBeLessThanOrEqual(4);
    expect([...timers.pending.values()].some((p) => p.ms > 0)).toBe(true);
  });

  it("closing releases the channel and sends nothing more", async () => {
    const server = new LiveServer();
    const a = tab(server, "a");
    await pump(a);
    a.channel.close();
    expect(a.socket.held.has(CHANNEL)).toBe(false);
    const sent = a.socket.sent.length;
    a.channel.localCommitted();
    await pump(a);
    expect(a.socket.sent.length).toBe(sent);
  });
});

describe("busyWait", () => {
  it.each([
    // rung, asked, random, wait
    [0, null, 0, LIVE_BUSY_RETRY.floorMs],
    [1, null, 0, LIVE_BUSY_RETRY.floorMs * 2],
    [3, null, 0, LIVE_BUSY_RETRY.floorMs * 8],
    [40, null, 0, LIVE_BUSY_RETRY.capMs],
    // The server's wait is a floor under the ladder, never undercut.
    [0, 1_000, 0, 1_000],
    [3, 1_000, 0, 2_000],
    // Jitter is added on top, up to 30% of the wait.
    [0, 1_000, 1, 1_300],
    [0, null, 0.5, 288],
  ])("rung %i, asked %s, random %s: %i ms", (rung, asked, random, wait) => {
    expect(busyWait(rung, asked, () => random)).toBe(wait);
  });

  it("two tabs refused together come back apart", () => {
    expect(busyWait(2, 250, () => 0.1)).not.toBe(busyWait(2, 250, () => 0.9));
  });
});
