// The notebook as a live Loro document. Pinned: the projection the editor
// reads (order, deleted cells, a lost order entry), structure edits written
// as the person's own peer and refused whole when one op is wrong, a cell's
// editor bound to its own `source` text, two people converging, undo taking
// back only one's own changes across cells, and a caret drawn only in the
// cell it stands in.

import { EditorSelection } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, describe, expect, it } from "vitest";

import { LiveDocChannel, type Timers } from "@/api/realtime/crdt/channel";
import { LoroCodeMirrorBinding } from "@/api/realtime/crdt/codeMirrorBinding";
import {
  CELL_ID_PATTERN,
  NotebookDocument,
  NotebookOpRefused,
  newCellId,
  type NotebookDocSource,
} from "@/api/realtime/crdt/notebookDoc";
import { SendBudget } from "@/api/realtime/crdt/sendBudget";

import { LiveServer, loroNode, settle, type FakeSocket } from "./liveServer";

type LoroDoc = InstanceType<typeof loroNode.LoroDoc>;

const NODE = "5e0c9a1b-2d3f-4a6b-8c7d-9e0f1a2b3c4d";
const CHANNEL = `doc:notebook:${NODE}`;

const fastTimers: Timers = {
  setTimeout: (fn, ms) => (ms >= 1000 ? null : globalThis.setTimeout(fn, 0)),
  clearTimeout: (h) => {
    if (h !== null) globalThis.clearTimeout(h as ReturnType<typeof setTimeout>);
  },
};

/** A document held directly, with no channel. */
function held(doc: LoroDoc, canWrite = true): NotebookDocSource & { commits: number } {
  return {
    doc,
    canWrite,
    commits: 0,
    listen: () => () => {},
    localCommitted() {
      this.commits += 1;
    },
  };
}

function sequentialIds(prefix = "a"): () => string {
  let n = 0;
  return () => `${prefix}${String(n++).padStart(9, "0")}`;
}

function fresh(peer = 7n): { doc: LoroDoc; nb: NotebookDocument; source: ReturnType<typeof held> } {
  const doc = new loroNode.LoroDoc();
  doc.setPeerId(peer);
  const source = held(doc);
  return { doc, nb: new NotebookDocument({ loro: loroNode, source, newId: sequentialIds(peer === 7n ? "a" : "b") }), source };
}

const ids = (nb: NotebookDocument): string[] => nb.snapshot().cells.map((c) => c.id);
const sources = (nb: NotebookDocument): string[] => nb.snapshot().cells.map((c) => c.source);

describe("cell ids", () => {
  it.each([
    ["all zero bits", new Uint8Array(7)],
    ["all one bits", new Uint8Array(7).fill(255)],
    ["mixed", Uint8Array.from([1, 2, 3, 4, 5, 6, 7])],
  ])("are ten Crockford digits (%s)", (_name, bytes) => {
    const id = newCellId((out) => {
      out.set(bytes);
      return out;
    });
    expect(id).toMatch(CELL_ID_PATTERN);
  });

  it("differ from draw to draw", () => {
    const seen = new Set(Array.from({ length: 200 }, () => newCellId()));
    expect(seen.size).toBe(200);
  });
});

describe("structure edits", () => {
  it("inserts at the end, after a cell and before a cell", () => {
    const { nb, source } = fresh();
    const [a] = nb.apply([{ op: "insert", source: "a = 1" }]).created;
    const [c] = nb.apply([{ op: "insert", source: "c = 3", after: a }]).created;
    const [b] = nb.apply([{ op: "insert", source: "b = 2", before: c }]).created;
    expect(ids(nb)).toEqual([a, b, c]);
    expect(sources(nb)).toEqual(["a = 1", "b = 2", "c = 3"]);
    expect(source.commits).toBe(3);
  });

  it("keeps the kind, name, config and meta it was given", () => {
    const { nb } = fresh();
    nb.apply([
      { op: "insert", kind: "sql", source: "SELECT 1", name: "orders", config: { hide_code: true }, meta: { connection: "Warehouse" } },
    ]);
    const [cell] = nb.snapshot().cells;
    expect(cell).toMatchObject({ kind: "sql", name: "orders", source: "SELECT 1", config: { hide_code: true }, meta: { connection: "Warehouse" } });
  });

  it("soft-deletes a cell and restores it where asked", () => {
    const { nb } = fresh();
    const [a, b, c] = nb.apply([
      { op: "insert", source: "a" },
      { op: "insert", source: "b" },
      { op: "insert", source: "c" },
    ]).created as [string, string, string];
    nb.apply([{ op: "delete", cell_id: b }]);
    expect(ids(nb)).toEqual([a, c]);
    expect(nb.snapshot().deleted.map((cell) => cell.id)).toEqual([b]);
    nb.apply([{ op: "restore", cell_id: b, after: c }]);
    expect(ids(nb)).toEqual([a, c, b]);
    expect(nb.snapshot().deleted).toEqual([]);
  });

  it.each([
    ["to the end", (x: string[]) => ({ cell_id: x[0]!, after: x[2]! }), [1, 2, 0]],
    ["to the start", (x: string[]) => ({ cell_id: x[2]!, before: x[0]! }), [2, 0, 1]],
    ["down one", (x: string[]) => ({ cell_id: x[0]!, after: x[1]! }), [1, 0, 2]],
    ["up one", (x: string[]) => ({ cell_id: x[2]!, before: x[1]! }), [0, 2, 1]],
    ["onto itself", (x: string[]) => ({ cell_id: x[1]!, after: x[0]! }), [0, 1, 2]],
  ])("moves a cell %s", (_name, move, expected) => {
    const { nb } = fresh();
    const made = nb.apply([{ op: "insert" }, { op: "insert" }, { op: "insert" }]).created;
    nb.apply([{ op: "move", ...move(made) }]);
    expect(ids(nb)).toEqual(expected.map((i) => made[i]));
  });

  it("renames, and refuses a name that is not an identifier", () => {
    const { nb } = fresh();
    const [a] = nb.apply([{ op: "insert" }]).created as [string];
    nb.apply([{ op: "rename", cell_id: a, name: "load" }]);
    expect(nb.snapshot().cells[0]!.name).toBe("load");
    expect(() => nb.apply([{ op: "rename", cell_id: a, name: "2fast" }])).toThrow(NotebookOpRefused);
    expect(nb.snapshot().cells[0]!.name).toBe("load");
  });

  it("refuses a whole batch when one op is wrong, writing nothing", () => {
    const { nb, doc } = fresh();
    const [a] = nb.apply([{ op: "insert", source: "x" }]).created as [string];
    const before = doc.oplogVersion().toJSON();
    let refusal: NotebookOpRefused | null = null;
    try {
      nb.apply([
        { op: "rename", cell_id: a, name: "fine" },
        { op: "set_kind", cell_id: a, kind: "cobol" },
      ]);
    } catch (error) {
      refusal = error as NotebookOpRefused;
    }
    expect(refusal).toMatchObject({ index: 1, code: "unknown_kind" });
    expect(doc.oplogVersion().toJSON()).toEqual(before);
    expect(nb.snapshot().cells[0]!.name).toBe("_");
  });

  it.each([
    ["an unknown cell", { op: "delete", cell_id: "zzzzzzzzzz" }, "cell_not_found"],
    ["an unknown config key", { op: "set_config", cell_id: "@", config: { colour: "red" } }, "invalid_config"],
    ["a mistyped config value", { op: "set_config", cell_id: "@", config: { disabled: "yes" } }, "invalid_config"],
    ["an edit that does not match", { op: "edit", cell_id: "@", edits: [{ old: "nope", new: "x" }] }, "edit_not_found"],
    ["an edit that matches twice", { op: "edit", cell_id: "@", edits: [{ old: "a", new: "b" }] }, "edit_ambiguous"],
    ["a second setup cell", { op: "insert", kind: "setup", before: "@" }, "setup_must_be_first"],
    ["a setup cell not first", { op: "insert", kind: "setup", after: "@" }, "setup_must_be_first"],
  ])("refuses %s", (_name, op, code) => {
    const { nb } = fresh();
    const [a] = nb.apply([{ op: "insert", kind: "setup", source: "a = a" }]).created as [string];
    const resolved = JSON.parse(JSON.stringify(op).replaceAll('"@"', JSON.stringify(a)));
    expect(() => nb.apply([resolved])).toThrow(expect.objectContaining({ code }));
  });

  it("refuses every write from a reader", () => {
    const doc = new loroNode.LoroDoc();
    const nb = new NotebookDocument({ loro: loroNode, source: held(doc, false) });
    expect(() => nb.apply([{ op: "insert" }])).toThrow(expect.objectContaining({ code: "read_only" }));
  });

  it("edits text by matching it, and replaces with the smallest change", () => {
    const { nb, doc } = fresh();
    const [a] = nb.apply([{ op: "insert", source: "x = 1\ny = 2\n" }]).created as [string];
    nb.apply([{ op: "edit", cell_id: a, edits: [{ old: "y = 2", new: "y = 3" }] }]);
    expect(sources(nb)).toEqual(["x = 1\ny = 3\n"]);
    const text = nb.sourceText(doc, a)!;
    const caret = text.getCursor(2)!;
    nb.apply([{ op: "replace", cell_id: a, source: "x = 1\ny = 3\nz = 4\n" }]);
    // The edit appended; a cursor before it did not move.
    expect(doc.getCursorPos(caret)?.offset).toBe(2);
  });

  it("stores only non-default config", () => {
    const { nb } = fresh();
    const [a] = nb.apply([{ op: "insert", config: { hide_code: true } }]).created as [string];
    nb.apply([{ op: "set_config", cell_id: a, config: { hide_code: false, disabled: true } }]);
    expect(nb.snapshot().cells[0]!.config).toEqual({ disabled: true });
  });

  it("sets a SQL cell's meta, keeping the keys it does not name, and null resets a key", () => {
    const { nb } = fresh();
    const [q] = nb.apply([{ op: "insert", kind: "sql", source: "SELECT 1", meta: { output_var: "_df" } }]).created as [string];
    nb.apply([{ op: "set_meta", cell_id: q, meta: { connection: "warehouse", show_output: false } }]);
    expect(nb.snapshot().cells[0]!.meta).toEqual({ output_var: "_df", connection: "warehouse", show_output: false });
    nb.apply([{ op: "set_meta", cell_id: q, meta: { connection: null, output_var: "orders" } }]);
    expect(nb.snapshot().cells[0]!.meta).toEqual({ output_var: "orders", show_output: false });
  });

  it.each([
    ["a key SQL cells do not have", "sql", { quote: "r" }],
    ["a result name that is not an identifier", "sql", { output_var: "1x" }],
    ["a connection name with a quote", "sql", { connection: 'wh"' }],
    ["a connection name that is not text", "sql", { connection: 7 }],
    ["show_output that is not a boolean", "sql", { show_output: "no" }],
    ["meta on a Python cell", "python", { connection: "wh" }],
  ])("refuses set_meta with %s, writing nothing", (_why, kind, meta) => {
    const { nb, doc } = fresh();
    const [c] = nb.apply([{ op: "insert", kind, source: "SELECT 1", meta: kind === "sql" ? { output_var: "_df" } : {} }]).created as [string];
    const before = doc.oplogVersion().toJSON();
    expect(() => nb.apply([{ op: "set_meta", cell_id: c, meta }])).toThrow(expect.objectContaining({ code: "invalid_config" }));
    expect(doc.oplogVersion().toJSON()).toEqual(before);
  });

  it("writes settings", () => {
    const { nb } = fresh();
    expect(nb.snapshot().settings.reactivity).toBe("autorun");
    nb.apply([{ op: "set_setting", key: "reactivity", value: "lazy" }]);
    expect(nb.snapshot().settings.reactivity).toBe("lazy");
  });

  it.each([
    ["an engine's environment id", "env", "default:.alkera/envs/default"],
    ["an absolute path", "env", "/opt/venv"],
    ["a row limit of none", "sql_row_limit", 0],
    ["a row limit as text", "sql_row_limit", "1000"],
    ["a reactivity nobody defined", "reactivity", "eager"],
    ["a setting nobody defined", "theme", "dark"],
  ])("refuses %s before anything is committed", (_label, key, value) => {
    const { nb, source } = fresh();
    const commits = source.commits;
    expect(() => nb.apply([{ op: "set_setting", key, value }])).toThrow(NotebookOpRefused);
    expect(source.commits).toBe(commits);
    expect(nb.snapshot().settings.stored ?? {}).not.toHaveProperty(key);
  });

  it.each([
    ["env", "./proj"],
    ["env", "script"],
    ["sql_row_limit", 1000],
  ])("takes %s = %j and keeps it as the file's own", (key, value) => {
    const { nb } = fresh();
    nb.apply([{ op: "set_setting", key, value }]);
    expect(nb.snapshot().settings.stored).toMatchObject({ [key]: value });
  });
});

describe("the projection", () => {
  it("reuses an untouched cell's object while another changes", () => {
    const { nb, doc } = fresh();
    const [a, b] = nb.apply([{ op: "insert", source: "a" }, { op: "insert", source: "b" }]).created as [string, string];
    const first = nb.snapshot();
    nb.sourceText(doc, b)!.insert(1, "!");
    doc.commit();
    const second = nb.snapshot();
    expect(second).not.toBe(first);
    expect(second.cells[0]).toBe(first.cells[0]);
    expect(second.cells[1]).not.toBe(first.cells[1]);
    expect(second.cells.map((c) => c.id)).toEqual([a, b]);
  });

  it("shows a repeated id once and a live cell the order lost at the end", () => {
    const { nb, doc } = fresh();
    const [a, b] = nb.apply([{ op: "insert" }, { op: "insert" }]).created as [string, string];
    const order = doc.getMovableList("order");
    order.insert(0, b);
    order.delete(2, 1);
    doc.commit();
    expect(order.toArray()).toEqual([b, a]);
    order.delete(1, 1);
    order.insert(0, b);
    doc.commit();
    // Order now [b, b]: `a` is live but unlisted.
    expect(ids(nb)).toEqual([b, a]);
  });

  it("notifies on anyone's change", () => {
    const { nb, doc } = fresh();
    let calls = 0;
    nb.subscribe(() => (calls += 1));
    const other = new loroNode.LoroDoc();
    other.setPeerId(9n);
    const theirs = new NotebookDocument({ loro: loroNode, source: held(other), newId: sequentialIds("b") });
    theirs.apply([{ op: "insert", source: "theirs" }]);
    doc.import(other.export({ mode: "snapshot" }));
    expect(calls).toBeGreaterThan(0);
    expect(sources(nb)).toEqual(["theirs"]);
  });
});

describe("two peers on one notebook", () => {
  function sync(x: LoroDoc, y: LoroDoc): void {
    y.import(x.export({ mode: "update", from: y.oplogVersion() }));
    x.import(y.export({ mode: "update", from: x.oplogVersion() }));
  }

  it("converge on concurrent inserts, moves and text", () => {
    const ana = fresh(7n);
    const ben = fresh(8n);
    const [a, b] = ana.nb.apply([{ op: "insert", source: "a" }, { op: "insert", source: "b" }]).created as [string, string];
    sync(ana.doc, ben.doc);
    ana.nb.apply([{ op: "move", cell_id: a, after: b }]);
    ben.nb.apply([{ op: "insert", source: "c", after: a }]);
    ben.nb.sourceText(ben.doc, b)!.insert(1, "2");
    ben.doc.commit();
    sync(ana.doc, ben.doc);
    expect(ids(ana.nb)).toEqual(ids(ben.nb));
    expect(sources(ana.nb)).toEqual(sources(ben.nb));
    expect(sources(ana.nb)).toContain("b2");
  });

  it("keeps typing that raced a kind change in the old text, not the new one", () => {
    const ana = fresh(7n);
    const ben = fresh(8n);
    const [a] = ana.nb.apply([{ op: "insert", source: "SELECT 1" }]).created as [string];
    sync(ana.doc, ben.doc);
    const old = ben.nb.sourceText(ben.doc, a)!;
    ana.nb.apply([{ op: "set_kind", cell_id: a, kind: "sql" }]);
    old.insert(8, " + 1");
    ben.doc.commit();
    sync(ana.doc, ben.doc);
    expect(sources(ana.nb)).toEqual(["SELECT 1"]);
    expect(sources(ben.nb)).toEqual(["SELECT 1"]);
    expect(old.toString()).toBe("SELECT 1 + 1");
  });

  it("undo takes back only this person's change, wherever it was", () => {
    const ana = fresh(7n);
    const ben = fresh(8n);
    const [a, b] = ana.nb.apply([{ op: "insert", source: "a" }, { op: "insert", source: "b" }]).created as [string, string];
    sync(ana.doc, ben.doc);
    ben.nb.apply([{ op: "rename", cell_id: a, name: "bens" }]);
    sync(ana.doc, ben.doc);
    ana.nb.apply([{ op: "delete", cell_id: b }]);
    sync(ana.doc, ben.doc);
    const touched = ana.nb.undo();
    expect(touched).toContain(b);
    sync(ana.doc, ben.doc);
    // Ana's delete is undone; Ben's rename stands.
    expect(ids(ben.nb)).toEqual([a, b]);
    expect(ben.nb.snapshot().cells[0]!.name).toBe("bens");
    expect(ana.nb.redo()).toContain(b);
    expect(ids(ana.nb)).toEqual([a]);
  });
});

// -- through the live channel, with real editors -------------------------------

interface Tab {
  socket: FakeSocket;
  channel: LiveDocChannel;
  nb: NotebookDocument;
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

async function open(server: LiveServer, name: string): Promise<Tab> {
  const socket = server.socket(name);
  const channel = new LiveDocChannel({
    socket,
    loadLoro: () => Promise.resolve(loroNode),
    docType: "notebook",
    docId: NODE,
    timers: fastTimers,
    budget: new SendBudget(() => null),
    storage: null,
    pageEvents: null,
    account: null,
  });
  channel.start();
  await pump(socket);
  return { socket, channel, nb: new NotebookDocument({ loro: loroNode, source: channel, newId: sequentialIds(name[0]) }) };
}

function editor(tab: Tab, cellId: string, focused = true): { binding: LoroCodeMirrorBinding; view: EditorView } {
  const binding = new LoroCodeMirrorBinding({
    channel: tab.channel,
    loro: loroNode,
    hueOf: () => 0,
    text: (doc) => tab.nb.sourceText(doc, cellId),
    history: tab.nb.sharedHistory,
    sharedCarets: true,
    timers: fastTimers,
  });
  const parent = document.createElement("div");
  document.body.appendChild(parent);
  const view = new EditorView({ state: binding.createState(binding.keys()), parent });
  Object.defineProperty(view, "hasFocus", { get: () => focused });
  views.push(view);
  return { binding, view };
}

function type(view: EditorView, at: number, text: string): void {
  view.dispatch({ changes: { from: at, insert: text }, selection: EditorSelection.cursor(at + text.length), userEvent: "input.type" });
}

describe("cell editors on the live channel", () => {
  it("carry typing in a cell to the other person's editor of that cell only", async () => {
    const server = new LiveServer();
    const ana = await open(server, "ana");
    const ben = await open(server, "ben");
    const [x, y] = ana.nb.apply([{ op: "insert", source: "x = 1" }, { op: "insert", source: "y = 2" }]).created as [string, string];
    await pump(ana.socket, ben.socket);
    const anaX = editor(ana, x);
    const benX = editor(ben, x);
    const benY = editor(ben, y);
    type(anaX.view, 5, "0");
    await pump(ana.socket, ben.socket);
    expect(benX.view.state.sliceDoc()).toBe("x = 10");
    expect(benY.view.state.sliceDoc()).toBe("y = 2");
    type(benY.view, 0, "# ");
    await pump(ana.socket, ben.socket);
    expect(sources(ana.nb)).toEqual(["x = 10", "# y = 2"]);
  });

  it("undo in any cell takes back this person's last change and not the other's", async () => {
    const server = new LiveServer();
    const ana = await open(server, "ana");
    const ben = await open(server, "ben");
    const [x, y] = ana.nb.apply([{ op: "insert", source: "x" }, { op: "insert", source: "y" }]).created as [string, string];
    await pump(ana.socket, ben.socket);
    const anaX = editor(ana, x);
    const anaY = editor(ana, y);
    const benX = editor(ben, x);
    type(anaX.view, 1, "1");
    await pump(ana.socket, ben.socket);
    type(benX.view, 0, "b");
    await pump(ana.socket, ben.socket);
    // Undo pressed in cell y still undoes Ana's typing in cell x.
    expect(anaY.binding.undo()).toBe(true);
    await pump(ana.socket, ben.socket);
    expect(anaX.view.state.sliceDoc()).toBe("bx");
    expect(benX.view.state.sliceDoc()).toBe("bx");
    expect(server.docOf(CHANNEL).doc.getMap("cells").toJSON()).toBeTruthy();
  });

  it("follows a kind change onto the cell's new text", async () => {
    const server = new LiveServer();
    const ana = await open(server, "ana");
    const ben = await open(server, "ben");
    const [x] = ana.nb.apply([{ op: "insert", source: "SELECT 1" }]).created as [string];
    await pump(ana.socket, ben.socket);
    const benX = editor(ben, x);
    ana.nb.apply([{ op: "set_kind", cell_id: x, kind: "sql" }]);
    await pump(ana.socket, ben.socket);
    type(benX.view, 8, ", 2");
    await pump(ana.socket, ben.socket);
    expect(sources(ana.nb)).toEqual(["SELECT 1, 2"]);
  });

  it("places a caret only in the cell whose text it names", async () => {
    const server = new LiveServer();
    const ana = await open(server, "ana");
    const [x, y] = ana.nb.apply([{ op: "insert", source: "abc" }, { op: "insert", source: "def" }]).created as [string, string];
    await pump(ana.socket);
    const inX = editor(ana, x);
    const inY = editor(ana, y, false);
    inX.view.dispatch({ selection: EditorSelection.cursor(2) });
    const cursors = inX.binding.selectionCursors();
    expect(cursors).not.toBeNull();
    expect(inX.binding.editorPosition(cursors!.focus)).toBe(2);
    expect(inY.binding.editorPosition(cursors!.focus)).toBeNull();
    expect(inY.binding.selectionCursors()).toBeNull();
  });
});
