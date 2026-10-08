// A CodeMirror editor bound to a live file: tabs on one stand-in server, each
// with a real EditorView over a real Loro document. Pinned: the editor's text
// is the document's byte for byte (surrogate pairs and CRLF included),
// somebody else's edit leaves the reader's selection where it belongs, undo is
// only ever one's own, carets arrive under the name the server stamped, a
// reader's editor will not take typing, and a new epoch is carried over.

import { insertNewline, insertNewlineAndIndent } from "@codemirror/commands";
import { EditorSelection } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, describe, expect, it } from "vitest";

import { LiveDocChannel, type Timers } from "@/api/realtime/crdt/channel";
import { LoroCodeMirrorBinding } from "@/api/realtime/crdt/codeMirrorBinding";
import { SendBudget } from "@/api/realtime/crdt/sendBudget";
import { liveCarets } from "@/pages/workspace/chat/workspace/liveCarets";

import { LiveServer, loroNode, settle, type FakeSocket } from "./liveServer";

const NODE = "0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f";
const CHANNEL = `doc:file:${NODE}`;

/** Short timers run at once; the ack, idle and caret backstops never fire. */
const fastTimers: Timers = {
  setTimeout: (fn, ms) => (ms >= 1000 ? null : globalThis.setTimeout(fn, 0)),
  clearTimeout: (h) => {
    if (h !== null) globalThis.clearTimeout(h as ReturnType<typeof setTimeout>);
  },
};

interface Tab {
  socket: FakeSocket;
  channel: LiveDocChannel;
  binding: LoroCodeMirrorBinding;
  view: EditorView;
}

const views: EditorView[] = [];

afterEach(() => {
  for (const view of views.splice(0)) view.destroy();
});

async function pump(...sockets: FakeSocket[]): Promise<void> {
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

async function open(server: LiveServer, name: string, { focused = true } = {}): Promise<Tab> {
  const socket = server.socket(name);
  const channel = new LiveDocChannel({
    socket,
    loadLoro: () => Promise.resolve(loroNode),
    docType: "file",
    docId: NODE,
    timers: fastTimers,
    budget: new SendBudget(() => null),
    storage: null,
    pageEvents: null,
    account: null,
  });
  channel.start();
  await pump(socket);
  const binding = new LoroCodeMirrorBinding({
    channel,
    loro: loroNode,
    hueOf: ({ email }) => email.length * 10,
    carets: liveCarets,
    timers: fastTimers,
  });
  const parent = document.createElement("div");
  document.body.appendChild(parent);
  const view = new EditorView({ state: binding.createState(binding.keys()), parent });
  // jsdom never focuses a contenteditable: the tab is the one being typed in.
  Object.defineProperty(view, "hasFocus", { get: () => focused });
  views.push(view);
  return { socket, channel, binding, view };
}

const shown = (tab: Tab): string => tab.view.state.sliceDoc();

/** Type `text` at editor position `at`, as the person would. */
function type(tab: Tab, at: number, text: string): void {
  tab.view.dispatch({ changes: { from: at, insert: text }, selection: EditorSelection.cursor(at + text.length), userEvent: "input.type" });
}

describe("the editor's text is the document's", () => {
  it("carries what one tab types to the other and to the server", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "def f():\n    return 1\n");
    const a = await open(server, "ana");
    const b = await open(server, "ben");
    expect(shown(a)).toBe("def f():\n    return 1\n");
    type(a, 0, "# top\n");
    await pump(a.socket, b.socket);
    type(b, shown(b).length, "# end\n");
    await pump(a.socket, b.socket);
    const expected = "# top\ndef f():\n    return 1\n# end\n";
    expect(shown(a)).toBe(expected);
    expect(shown(b)).toBe(expected);
    expect(server.text(CHANNEL)).toBe(expected);
  });

  it("counts a surrogate pair as two on both sides", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "a😀b");
    const a = await open(server, "ana");
    const b = await open(server, "ben");
    type(a, 3, "X");
    await pump(a.socket, b.socket);
    server.edit(CHANNEL, 1, "🎉");
    await pump(a.socket, b.socket);
    expect(server.text(CHANNEL)).toBe("a🎉😀Xb");
    expect(shown(a)).toBe("a🎉😀Xb");
    expect(shown(b)).toBe("a🎉😀Xb");
  });

  it("goes back to the document when an edit would split a surrogate pair", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "a😀b");
    const a = await open(server, "ana");
    a.view.dispatch({ changes: { from: 2, to: 3 } });
    await pump(a.socket);
    expect(shown(a)).toBe(a.binding.text());
    expect(server.text(CHANNEL)).toBe(a.binding.text());
  });

  it("keeps CRLF line endings as they are, both ways", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "one\r\ntwo\r\n");
    const a = await open(server, "ana");
    expect(a.view.state.doc.lines).toBe(3);
    // The start of line 2 is editor position 4 and Loro offset 5.
    type(a, a.view.state.doc.line(2).from, "2: ");
    await pump(a.socket);
    expect(server.text(CHANNEL)).toBe("one\r\n2: two\r\n");
    server.edit(CHANNEL, server.text(CHANNEL).length, "three\r\nfour");
    await pump(a.socket);
    expect(shown(a)).toBe("one\r\n2: two\r\nthree\r\nfour");
    expect(a.view.state.doc.lines).toBe(4);
    // A newline typed in a CRLF file is a CRLF.
    a.view.dispatch({ selection: EditorSelection.cursor(a.view.state.doc.length) });
    insertNewline(a.view);
    await pump(a.socket);
    expect(server.text(CHANNEL)).toBe("one\r\n2: two\r\nthree\r\nfour\r\n");
  });

  it("keeps a lone LF or CR in an LF file as the character it is", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "a\nb\rc\r\n");
    const a = await open(server, "ana");
    type(a, a.view.state.doc.length, "z");
    await pump(a.socket);
    expect(server.text(CHANNEL)).toBe("a\nb\rc\r\nz");
    expect(shown(a)).toBe("a\nb\rc\r\nz");
  });

  it("is brought back to the document when half a CRLF is deleted elsewhere", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "one\r\ntwo");
    const a = await open(server, "ana");
    server.edit(CHANNEL, 3, "", 1);
    await pump(a.socket);
    expect(server.text(CHANNEL)).toBe("one\ntwo");
    expect(shown(a)).toBe("one\ntwo");
  });
});

describe("somebody else's edit and the reader's selection", () => {
  it("moves the caret by exactly what was typed before it", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "hello world");
    const a = await open(server, "ana");
    a.view.dispatch({ selection: EditorSelection.cursor(6) });
    server.edit(CHANNEL, 0, ">> ");
    await pump(a.socket);
    expect(a.view.state.selection.main.head).toBe(9);
  });

  it("leaves the caret before text typed exactly where it stands", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "hello world");
    const a = await open(server, "ana");
    a.view.dispatch({ selection: EditorSelection.cursor(5) });
    server.edit(CHANNEL, 5, ", dear");
    await pump(a.socket);
    expect(shown(a)).toBe("hello, dear world");
    expect(a.view.state.selection.main.head).toBe(5);
  });

  it("does not move a caret before somebody else's edit", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "hello world");
    const a = await open(server, "ana");
    a.view.dispatch({ selection: EditorSelection.cursor(2) });
    server.edit(CHANNEL, 8, "XYZ");
    await pump(a.socket);
    expect(a.view.state.selection.main.head).toBe(2);
  });

  it("keeps a selection around the words it held", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "alpha beta gamma");
    const a = await open(server, "ana");
    a.view.dispatch({ selection: EditorSelection.range(6, 10) });
    server.edit(CHANNEL, 0, "0 ");
    await pump(a.socket);
    const { from, to } = a.view.state.selection.main;
    expect(shown(a).slice(from, to)).toBe("beta");
  });
});

describe("undo", () => {
  it("takes back only this person's own edits", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "");
    const a = await open(server, "ana");
    const b = await open(server, "ben");
    type(a, 0, "mine");
    await pump(a.socket, b.socket);
    type(b, 4, " theirs");
    await pump(a.socket, b.socket);
    expect(a.binding.undo()).toBe(true);
    await pump(a.socket, b.socket);
    expect(shown(a)).toBe(" theirs");
    expect(shown(b)).toBe(" theirs");
    expect(a.binding.redo()).toBe(true);
    await pump(a.socket, b.socket);
    expect(shown(b)).toBe("mine theirs");
  });

  it("never undoes anything for a tab that may only read", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "kept");
    server.readers.add("ana");
    const a = await open(server, "ana");
    expect(a.binding.undo()).toBe(false);
    expect(shown(a)).toBe("kept");
  });
});

describe("carets", () => {
  it("draws the other tab's caret under the name the server stamped", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "hello world");
    const a = await open(server, "ana");
    const b = await open(server, "ben", { focused: false });
    a.view.dispatch({ selection: EditorSelection.cursor(5) });
    await pump(a.socket, b.socket);
    const names = [...b.view.dom.querySelectorAll(".alk-cm-caret__name")].map((el) => el.textContent);
    expect(names).toEqual(["ANA"]);
    expect(b.binding.remoteCarets()).toEqual([
      { id: expect.any(String), name: "ANA", hue: "ana@acme.test".length * 10, head: 5, anchor: 5, moves: 1 },
    ]);
    // And it rides the text: an edit before it moves it along.
    server.edit(CHANNEL, 0, "> ");
    await pump(a.socket, b.socket);
    expect(b.binding.remoteCarets()[0]?.head).toBe(7);
    const moved = b.binding.remoteCarets()[0]?.moves ?? 0;
    const label = b.view.dom.querySelector(".alk-cm-caret__name");
    // Its owner moving it draws the caret afresh, so the name shows again.
    a.view.dispatch({ selection: EditorSelection.cursor(1) });
    await pump(a.socket, b.socket);
    expect(b.binding.remoteCarets()[0]?.moves).toBe(moved + 1);
    expect(b.view.dom.querySelector(".alk-cm-caret__name")).not.toBe(label);
  });

  it("hangs the name below a caret on the first line, where above it the editor's edge would cut it", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "first\nsecond");
    const a = await open(server, "ana");
    const b = await open(server, "ben", { focused: false });
    a.view.dispatch({ selection: EditorSelection.cursor(2) });
    await pump(a.socket, b.socket);
    expect(b.view.dom.querySelector(".alk-cm-caret__name")?.classList.contains("alk-cm-caret__name--below")).toBe(true);
    a.view.dispatch({ selection: EditorSelection.cursor(8) });
    await pump(a.socket, b.socket);
    expect(b.view.dom.querySelector(".alk-cm-caret__name")?.classList.contains("alk-cm-caret__name--below")).toBe(false);
  });

  it("draws the name outside the scroller, so a one-line editor's edge cannot clip it", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "x = 1");
    const a = await open(server, "ana");
    const b = await open(server, "ben", { focused: false });
    a.view.dispatch({ selection: EditorSelection.cursor(3) });
    await pump(a.socket, b.socket);
    // The bar stands in the text; the name is in a layer beside the scroller.
    expect(b.view.scrollDOM.querySelectorAll(".alk-cm-caret")).toHaveLength(1);
    const flags = [...b.view.dom.querySelectorAll(".alk-cm-caret__name")];
    expect(flags.map((flag) => flag.textContent)).toEqual(["ANA"]);
    expect(b.view.scrollDOM.contains(flags[0]!)).toBe(false);
    // It goes with its caret.
    server.tell(CHANNEL, "crdt", { t: "gone", loro_peer: a.channel.peer });
    await pump(b.socket);
    expect(b.view.dom.querySelectorAll(".alk-cm-caret__name")).toHaveLength(0);
  });

  it("removes a caret when its tab leaves", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "hello");
    const a = await open(server, "ana");
    const b = await open(server, "ben", { focused: false });
    a.view.dispatch({ selection: EditorSelection.cursor(2) });
    await pump(a.socket, b.socket);
    expect(b.binding.remoteCarets()).toHaveLength(1);
    server.tell(CHANNEL, "crdt", { t: "gone", loro_peer: a.channel.peer });
    await pump(b.socket);
    expect(b.binding.remoteCarets()).toEqual([]);
    expect(b.view.dom.querySelectorAll(".alk-cm-caret")).toHaveLength(0);
  });
});

describe("a reader", () => {
  it("gets an editor that will not take typing until it may write", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "read me");
    server.readers.add("ana");
    const a = await open(server, "ana");
    expect(a.view.state.readOnly).toBe(true);
    // Enter, as the editor's keymap binds it.
    expect(insertNewlineAndIndent(a.view)).toBe(false);
    expect(shown(a)).toBe("read me");
    // Somebody else's edits still arrive.
    server.edit(CHANNEL, 0, ">> ");
    await pump(a.socket);
    expect(shown(a)).toBe(">> read me");
    // Granted writing, the editor takes typing.
    server.readers.delete("ana");
    a.socket.inbox.push({ t: "subscribed", channel: CHANNEL, can_write: true });
    await pump(a.socket);
    expect(a.view.state.readOnly).toBe(false);
  });
});

describe("a new epoch", () => {
  it("carries the editor onto the new document and keeps writing", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "before");
    const a = await open(server, "ana");
    type(a, 6, " typed");
    await pump(a.socket);
    server.rotate(CHANNEL);
    await pump(a.socket);
    expect(a.channel.docEpoch).toBe(2);
    expect(shown(a)).toBe("before typed");
    type(a, shown(a).length, "!");
    await pump(a.socket);
    expect(server.text(CHANNEL)).toBe("before typed!");
  });
});
