// The live channel on a file: the same state machine as the draft, on the
// file's channel and its `content` text, and the one answer only a file gets
// (`not_editable`), which hands the file to the read-only view at once.

import { describe, expect, it } from "vitest";

import {
  ACK_TIMEOUT_MS,
  LiveDocChannel,
  UNSAVED_AFTER_MS,
  type LiveFallback,
  type Timers,
} from "@/api/realtime/crdt/channel";
import { acquireLiveFile, closeAllLiveFiles, type LiveFileState } from "@/api/realtime/crdt/liveFile";
import { SendBudget } from "@/api/realtime/crdt/sendBudget";
import { LIVE_BUSY_RETRY } from "@/lib/limits";

import { LiveServer, loroNode, settle, type FakeSocket } from "./liveServer";

const NODE = "0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f";
const CHANNEL = `doc:file:${NODE}`;
/** Who holds the file: a live file is never opened for nobody. */
const ACCOUNT = { userId: "usr_ana", orgId: "org_a" };

const fastTimers: Timers = {
  setTimeout: (fn, ms) => (ms >= 1000 ? null : globalThis.setTimeout(fn, 0)),
  clearTimeout: (h) => {
    if (h !== null) globalThis.clearTimeout(h as ReturnType<typeof setTimeout>);
  },
};

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

function channelOn(socket: FakeSocket): { channel: LiveDocChannel; fallbacks: LiveFallback[] } {
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
  const fallbacks: LiveFallback[] = [];
  channel.listen({ phase: (_phase, fallback) => fallback && fallbacks.push(fallback) });
  channel.start();
  return { channel, fallbacks };
}

/** Timers a test fires by hand, by the delay they were set for. */
function manualTimers(): { timers: Timers; fire: (ms: number) => void; pending: () => number[] } {
  const due: { fn: () => void; ms: number }[] = [];
  return {
    timers: {
      setTimeout: (fn, ms) => {
        const entry = { fn, ms };
        due.push(entry);
        return entry;
      },
      clearTimeout: (h) => {
        const at = due.indexOf(h as (typeof due)[number]);
        if (at >= 0) due.splice(at, 1);
      },
    },
    fire: (ms) => {
      for (const entry of due.filter((one) => one.ms === ms)) {
        due.splice(due.indexOf(entry), 1);
        entry.fn();
      }
    },
    pending: () => due.map((one) => one.ms),
  };
}

function manualChannel(socket: FakeSocket, timers: Timers): LiveDocChannel {
  return new LiveDocChannel({
    socket,
    loadLoro: () => Promise.resolve(loroNode),
    docType: "file",
    docId: NODE,
    timers,
    budget: new SendBudget(() => null),
    storage: null,
    pageEvents: null,
    account: null,
    // No jitter: the first wait is the ladder's floor exactly.
    random: () => 0,
  });
}

describe("a live file's channel", () => {
  it("subscribes to the file's channel and holds its content text", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "print('hi')\n");
    const socket = server.socket("ana");
    const { channel } = channelOn(socket);
    await pump(socket);
    expect(socket.held.has(CHANNEL)).toBe(true);
    expect(channel.phase).toBe("live");
    expect(channel.textName).toBe("content");
    expect(channel.doc?.getText("content").toString()).toBe("print('hi')\n");
    // Typed into `content`, it reaches the server's file document.
    channel.doc!.getText("content").insert(0, "# ");
    channel.doc!.commit({ origin: "local" });
    channel.localCommitted();
    await pump(socket);
    expect(server.text(CHANNEL)).toBe("# print('hi')\n");
  });

  it("holds whether saving is paused, from any epoch, and says each change once", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "x\n");
    const socket = server.socket("ana");
    const { channel } = channelOn(socket);
    const said: (string | null)[] = [];
    channel.listen({ saving: (paused) => said.push(paused === null ? null : paused.reason) });
    await pump(socket);
    expect(channel.savingPaused).toBeNull();
    server.tell(CHANNEL, "crdt", { t: "saving", state: "paused", reason: "no_writer" });
    server.tell(CHANNEL, "crdt", { t: "saving", state: "paused", reason: "no_writer" });
    await pump(socket);
    expect(channel.savingPaused).toEqual({ reason: "no_writer" });
    // A notice is about the document, not the epoch it was sent in.
    server.rotate(CHANNEL);
    await pump(socket);
    server.tell(CHANNEL, "crdt", { t: "saving", state: "ok" });
    await pump(socket);
    expect(channel.savingPaused).toBeNull();
    expect(said).toEqual(["no_writer", null]);
  });

  it("a tab opening after saving paused is told by the sync that opens it", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "x\n");
    server.saving.set(CHANNEL, { state: "paused", reason: "files.frozen" });
    const socket = server.socket("ana");
    const { channel } = channelOn(socket);
    const said: (string | null)[] = [];
    channel.listen({ saving: (paused) => said.push(paused === null ? null : paused.reason) });
    await pump(socket);
    expect(channel.phase).toBe("live");
    expect(channel.savingPaused).toEqual({ reason: "files.frozen" });
    expect(said).toEqual(["files.frozen"]);
  });

  it("a tab that missed the resume notice is told by its next sync that saving is back", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "x\n");
    const socket = server.socket("ana");
    const { channel } = channelOn(socket);
    server.tell(CHANNEL, "crdt", { t: "saving", state: "paused", reason: "no_writer" });
    await pump(socket);
    expect(channel.savingPaused).toEqual({ reason: "no_writer" });
    // Saving resumed on another replica while this tab was away; the notice
    // is lost with the socket, and the sync it reconnects with says ok.
    socket.disconnect();
    server.saving.set(CHANNEL, { state: "ok", reason: "" });
    socket.reconnect();
    await pump(socket);
    expect(channel.phase).toBe("live");
    expect(channel.savingPaused).toBeNull();
  });

  it.each([
    { name: "a new epoch", move: (server: LiveServer) => server.rotate(CHANNEL) },
    {
      name: "a stale write",
      move: (server: LiveServer) => server.tell(CHANNEL, "reload", { epoch: 1, reason: "stale_epoch", saving: server.saving.get(CHANNEL) }),
    },
  ])("takes the saving state a reload carries ($name)", async ({ move }) => {
    const server = new LiveServer();
    server.seed(CHANNEL, "x\n");
    const socket = server.socket("ana");
    const { channel } = channelOn(socket);
    await pump(socket);
    expect(channel.savingPaused).toBeNull();
    server.saving.set(CHANNEL, { state: "paused", reason: "quarantined_lost" });
    move(server);
    // Only the reload is delivered: the sync it asks for is held back, so
    // the state shown comes from the reload itself.
    await socket.deliver(1);
    expect(channel.savingPaused).toEqual({ reason: "quarantined_lost" });
  });

  it("a sync or reload with no saving state leaves what the notices said", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "x\n");
    const socket = server.socket("ana");
    const { channel } = channelOn(socket);
    server.tell(CHANNEL, "crdt", { t: "saving", state: "paused", reason: "leased" });
    await pump(socket);
    server.rotate(CHANNEL);
    await pump(socket);
    expect(channel.helloCount).toBeGreaterThan(1);
    expect(channel.savingPaused).toEqual({ reason: "leased" });
  });

  it("asks again when the server refuses the channel for a moment, and never falls back", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "x\n");
    server.refused.set(CHANNEL, "internal");
    const socket = server.socket("ana");
    const { timers, fire } = manualTimers();
    const channel = manualChannel(socket, timers);
    const fallbacks: string[] = [];
    channel.listen({ phase: (_phase, fallback) => fallback && fallbacks.push(fallback.reason) });
    channel.start();
    await pump(socket);
    expect(channel.phase).toBe("connecting");
    server.refused.delete(CHANNEL);
    fire(LIVE_BUSY_RETRY.floorMs);
    await pump(socket);
    expect(channel.phase).toBe("live");
    expect(fallbacks).toEqual([]);
  });

  it("still falls back on a refusal that is final", async () => {
    const server = new LiveServer();
    server.refused.set(CHANNEL, "not_found");
    const socket = server.socket("ana");
    const { channel, fallbacks } = channelOn(socket);
    await pump(socket);
    expect(channel.phase).toBe("fallback");
    expect(fallbacks.map((f) => f.reason)).toEqual(["not_found"]);
  });

  it("says edits are not saved yet while the server sits on them, keeps them, and clears once taken", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "x\n");
    const socket = server.socket("ana");
    const { timers, fire } = manualTimers();
    const channel = manualChannel(socket, timers);
    const said: boolean[] = [];
    channel.listen({ unsaved: (unsaved) => said.push(unsaved) });
    channel.start();
    await pump(socket);
    expect(channel.phase).toBe("live");
    server.muteAcks = true;
    channel.doc!.getText("content").insert(0, "kept ");
    channel.doc!.commit({ origin: "local" });
    channel.localCommitted();
    await pump(socket);
    fire(UNSAVED_AFTER_MS);
    expect(channel.unsaved).toBe(true);
    expect(channel.doc!.getText("content").toString()).toBe("kept x\n");
    // The ack was lost; the server took the edit. The next sync says so.
    server.muteAcks = false;
    fire(ACK_TIMEOUT_MS);
    await pump(socket);
    expect(channel.unsaved).toBe(false);
    expect(said).toEqual([true, false]);
    expect(server.text(CHANNEL)).toBe("kept x\n");
  });

  it("hands a file the server will not open live to the read-only view, and asks no more", async () => {
    const server = new LiveServer();
    server.refuseHellosWith = "not_editable";
    const socket = server.socket("ana");
    const { channel, fallbacks } = channelOn(socket);
    await pump(socket);
    expect(channel.phase).toBe("fallback");
    expect(fallbacks.map((f) => f.reason)).toEqual(["not_editable"]);
    const hellos = socket.sentKinds().filter((k) => k === "hello").length;
    await pump(socket);
    expect(socket.sentKinds().filter((k) => k === "hello").length).toBe(hellos);
    // And it let go of the channel.
    expect(socket.held.has(CHANNEL)).toBe(false);
  });
});

describe("acquireLiveFile", () => {
  it("shares one channel per file and goes live with Loro to bind to", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "x");
    const socket = server.socket("ana");
    const deps = { socket, loadLoro: () => Promise.resolve(loroNode), account: ACCOUNT };
    const first = acquireLiveFile(NODE, deps);
    const second = acquireLiveFile(NODE, deps);
    expect(second.file).toBe(first.file);
    const states: LiveFileState["kind"][] = [];
    first.file.subscribe((state) => states.push(state.kind));
    await pump(socket);
    expect(states).toEqual(["pending", "live"]);
    first.release();
    second.release();
    closeAllLiveFiles();
  });

  it("shows the file the ordinary way when Loro fails to load for the editor", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "x");
    const socket = server.socket("ana");
    let calls = 0;
    // The channel's own load works; the editor's (the second) fails.
    const loadLoro = () => (++calls === 1 ? Promise.resolve(loroNode) : Promise.reject(new Error("offline")));
    const held = acquireLiveFile(NODE, { socket, loadLoro, account: ACCOUNT });
    const states: LiveFileState[] = [];
    held.file.subscribe((state) => states.push(state));
    await pump(socket);
    expect(states.at(-1)).toEqual({ kind: "fallback", reason: "load_failed", unacknowledged: "" });
    held.release();
    closeAllLiveFiles();
  });

  it("opens a file that fell back afresh on the next hold", async () => {
    const server = new LiveServer();
    server.refuseHellosWith = "not_editable";
    const socket = server.socket("ana");
    const deps = { socket, loadLoro: () => Promise.resolve(loroNode), account: ACCOUNT };
    const held = acquireLiveFile(NODE, deps);
    await pump(socket);
    expect(held.file.state).toEqual({ kind: "fallback", reason: "not_editable", unacknowledged: "" });
    held.release();
    server.refuseHellosWith = null;
    server.seed(CHANNEL, "editable now");
    const again = acquireLiveFile(NODE, deps);
    expect(again.file).not.toBe(held.file);
    await pump(socket);
    expect(again.file.state.kind).toBe("live");
    again.release();
    closeAllLiveFiles();
  });
});
