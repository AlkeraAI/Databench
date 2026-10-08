// A notebook edit the server has not taken is never dropped unseen. A SQL cell
// inserted on a busy server once vanished with its text about a minute later:
// the server poisoned it for timing out, and the tab reset onto the server's
// copy offering nothing back (a notebook has no single text to splice). Here,
// through the live notebook the tab really opens: busy and a closed socket
// keep the cell and send it again until it lands; a refusal, or a restarted
// history the cell cannot be carried onto, says so and offers its text back.

import { afterEach, describe, expect, it } from "vitest";

import type { LiveNotice } from "@/api/realtime/crdt/channel";
import { NotebookDocument } from "@/api/realtime/crdt/notebookDoc";
import { LiveNotebook, closeAllLiveNotebooks } from "@/pages/workspace/chat/workspace/notebook/liveNotebook";

import { LiveServer, loroNode, settle, type FakeSocket } from "./liveServer";

const NODE = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const CHANNEL = `doc:notebook:${NODE}`;
const QUERY = "SELECT category, sum(amount) AS total FROM df GROUP BY 1 ORDER BY 1";

afterEach(() => closeAllLiveNotebooks());

interface Opened {
  socket: FakeSocket;
  notebook: LiveNotebook;
  notices: (LiveNotice | null)[];
}

/** Deliver frames until nothing is left to deliver; the channel's retries run
 *  on real timers, so this also waits out their short waits. */
async function pump(socket: FakeSocket, until?: () => boolean): Promise<void> {
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    await settle();
    await socket.deliver();
    if (until ? until() : socket.inbox.length === 0) return;
    if (until) await new Promise((r) => setTimeout(r, 20));
  }
}

function serverCells(server: LiveServer): Record<string, { source?: string; kind?: string }> {
  return server.docOf(CHANNEL).doc.getMap("cells").toJSON() as Record<string, { source?: string; kind?: string }>;
}

function localSources(opened: Opened): string[] {
  const state = opened.notebook.state;
  if (state.kind !== "live") return [];
  return state.doc.snapshot().cells.map((c) => c.source);
}

async function open(server: LiveServer): Promise<Opened> {
  const socket = server.socket("ana");
  const notebook = new LiveNotebook(NODE, { socket, loadLoro: () => Promise.resolve(loroNode), account: null });
  const notices: (LiveNotice | null)[] = [];
  notebook.channel.listen({ notice: (n) => notices.push(n) });
  await pump(socket, () => notebook.state.kind === "live");
  return { socket, notebook, notices };
}

function seed(server: LiveServer): void {
  const { doc } = server.docOf(CHANNEL);
  new NotebookDocument({
    loro: loroNode,
    source: { doc, canWrite: true, listen: () => () => {}, localCommitted: () => {} },
    newId: () => "xxxxxxxxx0",
  }).apply([{ op: "insert", source: "x = 1" }]);
}

function insertSql(opened: Opened): string {
  const state = opened.notebook.state;
  if (state.kind !== "live") throw new Error("not live");
  const [created] = state.doc.apply([{ op: "insert", kind: "sql", source: QUERY }]).created as [string];
  return created;
}

describe("a notebook edit the server has not taken", () => {
  it("stays in the notebook while the server answers busy, and lands when it keeps up", async () => {
    const server = new LiveServer();
    seed(server);
    const ana = await open(server);
    server.busyUpdates = 3;
    const cell = insertSql(ana);
    await pump(ana.socket, () => server.busyUpdates === 2);
    expect(localSources(ana)).toEqual(["x = 1", QUERY]);
    await pump(ana.socket, () => serverCells(server)[cell]?.source === QUERY);
    expect(serverCells(server)[cell]).toMatchObject({ kind: "sql", source: QUERY });
    expect(localSources(ana)).toEqual(["x = 1", QUERY]);
    expect(ana.notices.filter((n) => n !== null)).toEqual([]);
  });

  it("stays in the notebook across a closed socket, and is sent again on the next", async () => {
    const server = new LiveServer();
    seed(server);
    const ana = await open(server);
    ana.socket.disconnect();
    const cell = insertSql(ana);
    await pump(ana.socket);
    expect(serverCells(server)[cell]).toBeUndefined();
    expect(localSources(ana)).toEqual(["x = 1", QUERY]);
    ana.socket.reconnect();
    await pump(ana.socket, () => serverCells(server)[cell]?.source === QUERY);
    expect(serverCells(server)[cell]).toMatchObject({ kind: "sql", source: QUERY });
    expect(localSources(ana)).toEqual(["x = 1", QUERY]);
  });

  it("refused, is offered back with its text rather than dropped unseen", async () => {
    const server = new LiveServer();
    seed(server);
    const ana = await open(server);
    server.failNext = { code: "crdt_rejected" };
    insertSql(ana);
    await pump(ana.socket, () => ana.notices.length > 0);
    expect(ana.notices.at(-1)).toEqual({ message: "Your last edit could not be shared.", restorable: QUERY });
  });

  it("that a restarted history cannot carry is offered back with its text", async () => {
    const server = new LiveServer();
    seed(server);
    const ana = await open(server);
    ana.socket.disconnect();
    insertSql(ana);
    // The history restarts while the tab is away: the new epoch holds what
    // the server had, and the cell was never part of it.
    const old = server.docOf(CHANNEL);
    const restarted = new loroNode.LoroDoc();
    restarted.setPeerId(2n);
    restarted.import(old.doc.export({ mode: "snapshot" }));
    server.docs.set(CHANNEL, { doc: restarted as typeof old.doc, epoch: old.epoch + 1 });
    ana.socket.reconnect();
    await pump(ana.socket, () => ana.notices.length > 0);
    expect(ana.notices.at(-1)).toEqual({ message: "Your latest edits could not be kept.", restorable: QUERY });
  });
});
