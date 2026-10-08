// The field bound to a live draft: two tabs on one stand-in server, each with
// a view that records what the binding shows it. Pinned: edits land as the
// person made them, somebody else's edit leaves the caret beside the words it
// was beside, a composition is never rewritten under the input method, undo is
// only ever one's own, a send deletes only what the sender saw, and carets
// arrive under the name the server stamped.

import { describe, expect, it, vi } from "vitest";

import type { TextSelection, TextView } from "@alkera/ui";
import { LiveDocChannel, type Timers } from "@/api/realtime/crdt/channel";
import { SendBudget } from "@/api/realtime/crdt/sendBudget";
import { CARET_REFRESH_MS, CARET_TIMEOUT_MS, LoroTextBinding, rebaseSplice } from "@/api/realtime/crdt/textBinding";

import { LiveServer, loroNode, settle, type FakeSocket } from "./liveServer";

const DOC = "sess-1";
const CHANNEL = `doc:chat_draft:${DOC}`;

/** Short timers run at once; the ack and idle backstops never fire on their own. */
const fastTimers: Timers = {
  setTimeout: (fn, ms) => (ms >= 1000 ? null : globalThis.setTimeout(fn, 0)),
  clearTimeout: (h) => {
    if (h !== null) globalThis.clearTimeout(h as ReturnType<typeof setTimeout>);
  },
};

/** Timers the test fires by hand (the binding's caret sweep and refresh). */
class ManualTimers implements Timers {
  private next = 1;
  readonly pending = new Map<number, { fn: () => void; ms: number }>();
  setTimeout(fn: () => void, ms: number): unknown {
    const id = this.next++;
    this.pending.set(id, { fn, ms });
    return id;
  }
  clearTimeout(handle: unknown): void {
    this.pending.delete(handle as number);
  }
  /** Fire every timer armed for exactly `ms`. */
  fire(ms: number): void {
    for (const [id, t] of [...this.pending]) {
      if (t.ms === ms) {
        this.pending.delete(id);
        t.fn();
      }
    }
  }
}

class View implements TextView {
  text = "";
  selection: TextSelection | null = null;
  composing = false;
  focused = true;
  shown = 0;
  /** A field that has been told what to show but not yet drawn it (React has
   *  not committed): keystrokes still land on the old text. */
  lagging = false;
  private queued: { text: string; selection: TextSelection | null } | null = null;
  /** Draw what the binding last asked for. */
  catchUp(): void {
    const queued = this.queued;
    this.queued = null;
    if (queued !== null) this.draw(queued.text, queued.selection);
  }
  getText(): string {
    return this.text;
  }
  getSelection(): TextSelection | null {
    return this.focused ? this.selection : null;
  }
  setText(text: string, selection: TextSelection | null): void {
    this.shown += 1;
    if (this.lagging) this.queued = { text, selection };
    else this.draw(text, selection);
  }
  private draw(text: string, selection: TextSelection | null): void {
    this.text = text;
    if (selection !== null) this.selection = selection;
  }
  isComposing(): boolean {
    return this.composing;
  }
}

interface Tab {
  socket: FakeSocket;
  channel: LiveDocChannel;
  binding: LoroTextBinding;
  view: View;
  carets: ManualTimers;
}

async function open(server: LiveServer, name: string): Promise<Tab> {
  const socket = server.socket(name);
  const channel = new LiveDocChannel({
    socket,
    loadLoro: () => Promise.resolve(loroNode),
    docType: "chat_draft",
    account: null,
    docId: DOC,
    timers: fastTimers,
    budget: new SendBudget(() => null),
  });
  channel.start();
  await pumpOne(socket);
  const carets = new ManualTimers();
  const binding = new LoroTextBinding({ channel, loro: loroNode, hueOf: ({ email }) => email.length * 10, timers: carets });
  const view = new View();
  binding.attach(view);
  return { socket, channel, binding, view, carets };
}

async function pumpOne(...sockets: FakeSocket[]): Promise<void> {
  let quiet = 0;
  for (let i = 0; i < 200 && quiet < 4; i += 1) {
    await settle();
    if (sockets.every((s) => s.inbox.length === 0)) {
      quiet += 1;
      continue;
    }
    quiet = 0;
    for (const s of sockets) await s.deliver();
  }
}

const pump = (...tabs: Tab[]): Promise<void> => pumpOne(...tabs.map((t) => t.socket));

const at = (n: number): TextSelection => ({ start: n, end: n, direction: "none" });

/** The person types `text` at the caret, the way the Composer reports it. */
function typeAt(t: Tab, index: number, text: string): void {
  const before = t.view.text;
  const after = before.slice(0, index) + text + before.slice(index);
  t.view.text = after;
  t.view.selection = at(index + text.length);
  t.binding.edit(before, after, t.view.selection);
}

describe("LoroTextBinding", () => {
  it("shows the shared text on attach and sends the person's edit", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "hello");
    const a = await open(server, "a");
    expect(a.view.text).toBe("hello");
    typeAt(a, 5, " world");
    await pump(a);
    expect(server.text(CHANNEL)).toBe("hello world");
  });

  it("keeps the reader's caret beside their words when somebody types above it", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "first line\nsecond line");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    // B's caret sits after "second".
    b.view.selection = at(17);
    typeAt(a, 0, ">> ");
    await pump(a, b);
    expect(b.view.text).toBe(">> first line\nsecond line");
    expect(b.view.selection).toEqual(at(20));
    expect(b.view.text.slice(0, 20).endsWith("second")).toBe(true);
  });

  it("two people typing at the same place keep their own words whole", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "note: ");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    a.view.selection = at(6);
    b.view.selection = at(6);
    const words = { a: "alpha", b: "beta!" };
    // A keystroke each, then each sees the other's, for the length of the words.
    for (let i = 0; i < 5; i += 1) {
      typeAt(a, a.view.selection!.start, words.a[i]!);
      typeAt(b, b.view.selection!.start, words.b[i]!);
      await pump(a, b);
    }
    expect(a.view.text).toBe(b.view.text);
    expect(a.view.text).toContain("alpha");
    expect(a.view.text).toContain("beta!");
  });

  it("a caret at the very end stays before what somebody else adds there", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "end");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    b.view.selection = at(3);
    typeAt(a, 3, "+more");
    await pump(a, b);
    expect(b.view.text).toBe("end+more");
    expect(b.view.selection).toEqual(at(3));
  });

  it("keeps a selection a selection, in its direction, across a remote edit", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "pick these words");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    b.view.selection = { start: 5, end: 10, direction: "backward" };
    typeAt(a, 0, "please ");
    await pump(a, b);
    expect(b.view.selection).toEqual({ start: 12, end: 17, direction: "backward" });
    expect(b.view.text.slice(12, 17)).toBe("these");
  });

  it("never splits an emoji when a caret sits inside one", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "a😀b");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    // A field never reports the middle of a pair, but a stale offset can be one.
    b.view.selection = at(2);
    typeAt(a, 0, "x");
    await pump(a, b);
    expect(b.view.text).toBe("xa😀b");
    expect([2, 4]).toContain(b.view.selection?.start);
  });

  it("leaves the field alone while an input method composes, then catches up", async () => {
    const server = new LiveServer();
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    b.view.composing = true;
    const shown = b.view.shown;
    typeAt(a, 0, "while you compose");
    await pump(a, b);
    expect(b.view.shown).toBe(shown);
    expect(b.binding.text()).toBe("while you compose");
    b.view.composing = false;
    b.binding.compositionEnded();
    expect(b.view.text).toBe("while you compose");
  });

  it("an edit on a field that has not drawn somebody else's yet lands where it was made", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "ab");
    const [a, b, c] = [await open(server, "a"), await open(server, "b"), await open(server, "c")];
    b.view.lagging = true;
    // Two people change the text at both ends; b's field still reads "ab".
    typeAt(a, 0, "X");
    await pump(a, b, c);
    typeAt(c, 3, "Y");
    await pump(a, b, c);
    expect(b.view.text).toBe("ab");
    typeAt(b, 1, "Z");
    await pump(a, b, c);
    expect(server.text(CHANNEL)).toBe("XaZbY");
    b.view.lagging = false;
    b.view.catchUp();
    for (const t of [a, b, c]) expect(t.view.text).toBe("XaZbY");
    expect(b.view.selection).toEqual(at(3));
  });

  it("two people typing in turn at two places keep their runs whole while a field lags", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "[]");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    a.view.selection = at(1);
    b.view.selection = at(2);
    for (let i = 0; i < 12; i += 1) {
      // Every other round b's field has not drawn a's last letter yet.
      b.view.lagging = i % 2 === 0;
      typeAt(a, a.view.selection.end, "a");
      await pump(a, b);
      typeAt(b, b.view.selection!.end, "b");
      await pump(a, b);
      b.view.lagging = false;
      b.view.catchUp();
    }
    const want = `[${"a".repeat(12)}]${"b".repeat(12)}`;
    expect(server.text(CHANNEL)).toBe(want);
    expect(a.view.text).toBe(want);
    expect(b.view.text).toBe(want);
  });

  it("a reader typing at the end keeps their run in order while the field lags behind edits at the start", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "MIDDLE");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    a.view.selection = at(0);
    b.view.selection = at(6);
    b.view.lagging = true;
    const mine = "ZYXWVU";
    for (let i = 0; i < mine.length; i += 1) {
      typeAt(a, a.view.selection.end, String(i));
      await pump(a, b);
      // Every other round the field draws a's letter before b's next key;
      // otherwise b types on text that has not drawn it yet.
      if (i % 2 === 1) b.view.catchUp();
      typeAt(b, b.view.selection!.end, mine[i]!);
      await pump(a, b);
    }
    b.view.lagging = false;
    b.view.catchUp();
    expect(server.text(CHANNEL)).toBe(`012345MIDDLE${mine}`);
    expect(b.view.text).toBe(`012345MIDDLE${mine}`);
  });

  it("a stale edit during a composition does not redraw the field until it ends", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "AAA BBB");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    b.view.composing = true;
    b.view.lagging = true;
    typeAt(a, 0, "zz");
    await pump(a, b);
    b.view.lagging = false;
    b.view.catchUp();
    const shown = b.view.shown;
    // The composition's steps, each typed on the field as the input method holds it.
    const steps = ["に", "にほ", "にほん"];
    let field = "AAA BBB";
    for (const step of steps) {
      const next = `AAA BBB${step}`;
      b.view.text = next;
      b.view.selection = at(next.length);
      b.binding.edit(field, next, b.view.selection);
      field = next;
    }
    expect(b.view.shown).toBe(shown);
    b.view.text = "AAA BBB日本";
    b.binding.edit(field, "AAA BBB日本", at(9));
    b.view.composing = false;
    b.binding.compositionEnded();
    await pump(a, b);
    expect(server.text(CHANNEL)).toBe("zzAAA BBB日本");
    expect(b.view.text).toBe("zzAAA BBB日本");
  });

  it("a caret at the very start stays there while somebody types there", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "world");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    b.view.selection = at(0);
    a.view.selection = at(0);
    for (const ch of "hello ") {
      typeAt(a, a.view.selection.end, ch);
      await pump(a, b);
    }
    expect(b.view.selection).toEqual(at(0));
    typeAt(b, 0, "X");
    await pump(a, b);
    expect(server.text(CHANNEL)).toBe("Xhello world");
  });

  it("undoes a word typed while the field had not drawn somebody else's edit", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "ab");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    b.view.lagging = true;
    typeAt(a, 0, "X");
    await pump(a, b);
    typeAt(b, 2, "Z");
    await pump(a, b);
    expect(server.text(CHANNEL)).toBe("XabZ");
    b.view.lagging = false;
    b.view.catchUp();
    expect(b.binding.undo()).toBe(true);
    await pump(a, b);
    expect(server.text(CHANNEL)).toBe("Xab");
    expect(b.view.text).toBe("Xab");
  });

  it("undoes only this person's own edit", async () => {
    const server = new LiveServer();
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    typeAt(a, 0, "mine");
    await pump(a, b);
    typeAt(b, 4, " theirs");
    await pump(a, b);
    expect(a.binding.undo()).toBe(true);
    await pump(a, b);
    expect(a.view.text).toBe(" theirs");
    expect(server.text(CHANNEL)).toBe(" theirs");
    expect(a.binding.redo()).toBe(true);
    await pump(a, b);
    expect(server.text(CHANNEL)).toBe("mine theirs");
  });

  it("a send clears only what the sender saw", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "send this");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    // B types, and A clears before B's words reach A.
    typeAt(b, 9, " and this too");
    await settle();
    await b.socket.deliver();
    const before = a.view.text;
    a.view.text = "";
    a.binding.edit(before, "", at(0));
    await pump(a, b);
    expect(server.text(CHANNEL)).toBe(" and this too");
    expect(a.view.text).toBe(" and this too");
  });

  it("an edit raced by a remote one the field has not shown lands without dropping either", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "abc");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    typeAt(b, 0, "X");
    await pump(b);
    await a.socket.deliver();
    // The document took B's edit; A's field still says "abc" when A types.
    a.binding.edit("abc", "abcD", at(4));
    await pump(a, b);
    expect(server.text(CHANNEL)).toContain("X");
    expect(server.text(CHANNEL)).toContain("D");
  });

  it("shows another person's caret under the name and colour the server gave it", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "hello world");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    const seen: unknown[][] = [];
    b.binding.subscribeCarets((carets) => seen.push(carets));
    a.binding.select({ start: 0, end: 5, direction: "forward" });
    await pump(a, b);
    const last = seen.at(-1) as { name: string; offset: number; anchor: number; hue: number }[];
    expect(last).toHaveLength(1);
    expect(last[0]).toMatchObject({ name: "A", offset: 5, anchor: 0, hue: "a@acme.test".length * 10 });
    // A's own caret is never drawn for A.
    const own: unknown[][] = [];
    a.binding.subscribeCarets((c) => own.push(c));
    expect(own.at(-1)).toEqual([]);
  });
});

describe("carets of tabs that left", () => {
  async function seenBy(b: Tab): Promise<() => unknown[]> {
    const seen: unknown[][] = [[]];
    b.binding.subscribeCarets((carets) => seen.push(carets));
    return () => seen.at(-1)!;
  }

  it("a caret goes the moment the server says its tab left", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "hello world");
    const [a, b] = [await open(server, "a"), await open(server, "b")];
    const latest = await seenBy(b);
    a.binding.select({ start: 2, end: 2, direction: "none" });
    await pump(a, b);
    expect(latest()).toHaveLength(1);
    b.socket.inbox.push({
      t: "doc",
      envelope: {
        doc_id: DOC,
        doc_type: "chat_draft",
        epoch: b.channel.docEpoch,
        peer_id: "p:a",
        seq: 0,
        kind: "crdt",
        payload: { t: "gone", loro_peer: a.channel.peer },
      },
    });
    await pump(b);
    expect(latest()).toEqual([]);
    // The same peer back again is shown again.
    a.binding.select({ start: 4, end: 4, direction: "none" });
    await pump(a, b);
    expect(latest()).toHaveLength(1);
  });

  it("a caret nobody refreshes is cleared after the timeout, and a refreshed one is not", async () => {
    // Loro's store expires carets on its own interval, against the clock.
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"], now: new Date("2026-10-01T12:00:00Z") });
    try {
      const server = new LiveServer();
      server.seed(CHANNEL, "hello world");
      const [a, b] = [await open(server, "a"), await open(server, "b")];
      const latest = await seenBy(b);
      a.binding.select({ start: 3, end: 3, direction: "none" });
      await pump(a, b);
      expect(latest()).toHaveLength(1);
      // Still there and not moving, but refreshing: well past the timeout
      // it is still drawn.
      for (let i = 0; i < 6; i += 1) {
        vi.advanceTimersByTime(CARET_REFRESH_MS);
        a.carets.fire(CARET_REFRESH_MS);
        await pump(a, b);
      }
      expect(latest()).toHaveLength(1);
      // Gone without a word (a crash): cleared once the timeout passes.
      vi.advanceTimersByTime(CARET_TIMEOUT_MS * 2);
      expect(latest()).toEqual([]);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("rebaseSplice", () => {
  it.each([
    ["an edit after the remote change shifts with it", "abc", "Xabc", { index: 3, remove: 0, insert: "D" }, { index: 4, remove: 0, insert: "D" }],
    ["an edit before it stays", "abc", "abcX", { index: 0, remove: 0, insert: "D" }, { index: 0, remove: 0, insert: "D" }],
    ["a deletion after it shifts", "abcdef", "Xabcdef", { index: 4, remove: 2, insert: "" }, { index: 5, remove: 2, insert: "" }],
    ["an overlapping insert goes beside it, deleting nothing", "abcdef", "abXYZef", { index: 3, remove: 1, insert: "Q" }, { index: 5, remove: 0, insert: "Q" }],
    ["an overlapping pure deletion is dropped", "abcdef", "abXYZef", { index: 3, remove: 1, insert: "" }, null],
  ])("%s", (_name, seen, current, edit, expected) => {
    expect(rebaseSplice(seen, current, edit)).toEqual(expected);
  });
});
