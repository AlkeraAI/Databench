// One synchronised document over the socket, driven through a real WsClient with a fake
// WebSocket: hello after the subscription is confirmed, the snapshot adopted, durable ops
// applied strictly in server-sequence order (early ones held, duplicates dropped, a gap that
// never fills resolved by a fresh snapshot), ephemeral ops passed straight through, our own
// ops resolved by their acks in send order, a stale epoch never applied silently, a reload
// or reset re-helloing, and a dropped socket settling what it can and re-helloing on return.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  DocOpError,
  openDoc,
  type AckPayload,
  type DocHandle,
  type DocMessage,
  type DocPhase,
} from "@/api/realtime/docSync";
import { WsClient, type ClientFrame, type Envelope } from "@/api/realtime/wsClient";

import { FakeWebSocket, advance, socketFactory, ticketMinter } from "./fakeWebSocket";

type State = { kb_version: number; fields: Record<string, unknown> };

const DOC = "0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f";
const CHANNEL = `doc:artifact:${DOC}`;

const srv = (kind: Envelope["kind"], epoch: number, seq: number, payload: Record<string, unknown>) => ({
  t: "doc",
  envelope: { doc_id: DOC, doc_type: "artifact", epoch, peer_id: "srv:0", seq, kind, payload },
});
const peerOp = (epoch: number, seq: number, opId: string, peerId = "p:other") => ({
  t: "doc",
  envelope: {
    doc_id: DOC,
    doc_type: "artifact",
    epoch,
    peer_id: peerId,
    seq,
    kind: "op",
    payload: { op_id: opId, intent: "set_fields", fields: { title: { value: opId, ts: seq } } },
  },
});
const snapshot = (epoch: number, seq: number, state: State = { kb_version: 1, fields: {} }) =>
  srv("snapshot", epoch, seq, { state, seq });

let sockets: ReturnType<typeof socketFactory>;
let minter: ReturnType<typeof ticketMinter>;
let client: WsClient;
let handle: DocHandle<State>;
let messages: DocMessage<State>[];
let phases: DocPhase[];

const docFrames = (socket: FakeWebSocket) =>
  socket.framesOf("doc").map((f) => (f as Extract<ClientFrame, { t: "doc" }>).envelope);
const hellos = (socket: FakeWebSocket) => docFrames(socket).filter((e) => e.kind === "hello");
const ops = (socket: FakeWebSocket) => docFrames(socket).filter((e) => e.kind === "op");

async function connect(peerId = "p:1") {
  client.start();
  await advance(0);
  sockets.last().welcome(peerId);
  await advance(0);
}

function open(opts: Parameters<typeof openDoc>[3] = {}) {
  let n = 0;
  handle = openDoc<State>(client, "artifact", DOC, { opId: () => `op-${++n}`, ...opts });
  handle.onMessage((m) => messages.push(m));
  handle.onPhase((p) => phases.push(p));
  return handle;
}

/** Subscribe → hello → snapshot, leaving the document live at (epoch, seq). */
async function live(epoch = 3, seq = 7) {
  const socket = sockets.last();
  socket.serverSend({ t: "subscribed", channel: CHANNEL, can_write: true });
  socket.serverSend(snapshot(epoch, seq));
  await advance(0);
}

const settled = <T,>(p: Promise<T>) =>
  p.then(
    (value) => ({ status: "resolved" as const, value }),
    (error: DocOpError) => ({ status: "rejected" as const, error }),
  );
/** A send whose outcome the test does not read; a later dispose may refuse it. */
const fire = (p: Promise<unknown>): void => void p.catch(() => undefined);

beforeEach(() => {
  vi.useFakeTimers();
  sockets = socketFactory();
  minter = ticketMinter();
  client = new WsClient({
    mintTicket: minter.mint,
    socketUrl: (path) => `ws://api.test${path}`,
    factory: sockets.factory,
    backoff: { jitter: () => 0 },
  });
  messages = [];
  phases = [];
});

afterEach(() => {
  handle?.dispose();
  client.stop();
  vi.useRealTimers();
});

describe("opening a document", () => {
  it("subscribes the channel, hellos once the server confirms it, and adopts the snapshot", async () => {
    await connect();
    open();
    const socket = sockets.last();
    expect(socket.framesOf("subscribe").map((f) => f.channel)).toEqual([CHANNEL]);
    expect(hellos(socket)).toHaveLength(0);
    expect(handle.getPhase().phase).toBe("connecting");

    socket.serverSend({ t: "subscribed", channel: CHANNEL, can_write: true });
    expect(hellos(socket)).toEqual([
      { doc_id: DOC, doc_type: "artifact", epoch: 0, peer_id: "p:1", seq: 0, kind: "hello", payload: {} },
    ]);
    expect(handle.getPhase().canWrite).toBe(true);

    socket.serverSend(snapshot(3, 7, { kb_version: 2, fields: { title: { value: "T" } } }));
    expect(messages).toEqual([
      { kind: "snapshot", state: { kb_version: 2, fields: { title: { value: "T" } } }, epoch: 3, seq: 7 },
    ]);
    expect(handle.getPhase()).toMatchObject({ phase: "live", epoch: 3, seq: 7, peerId: "p:1", pending: 0, error: null });
  });

  it("a subscription confirmed before the document opened is caught on the next reconnect, never blocking forever", async () => {
    await connect();
    open();
    // The server confirms; the document is live; the socket drops; on return the channel is
    // re-subscribed and re-confirmed, and the document re-hellos.
    await live();
    sockets.last().serverClose(1006);
    await advance(2_000);
    sockets.last().welcome("p:2");
    sockets.last().serverSend({ t: "subscribed", channel: CHANNEL, can_write: true });
    expect(hellos(sockets.last())).toHaveLength(1);
    expect(hellos(sockets.last())[0].peer_id).toBe("p:2");
  });

  it("a subscription refused in-band ends the document with that code and rejects what was queued", async () => {
    await connect();
    open();
    const queued = settled(handle.sendOp({ intent: "set_fields", fields: { title: { value: "x", ts: 1 } } }));
    sockets.last().serverSend({ t: "error", code: "not_found", message: "no such doc", channel: CHANNEL });
    await advance(0);
    expect(handle.getPhase()).toMatchObject({ phase: "error", error: "not_found", pending: 0 });
    const outcome = await queued;
    expect(outcome.status).toBe("rejected");
    expect(outcome).toMatchObject({ error: { code: "not_found", message: "no such doc" } });
    expect((outcome as { error: unknown }).error).toBeInstanceOf(DocOpError);
  });

  it("an error on another channel is not ours", async () => {
    await connect();
    open();
    sockets.last().serverSend({ t: "error", code: "not_found", message: "", channel: "doc:artifact:other" });
    expect(handle.getPhase().phase).toBe("connecting");
  });
});

describe("applying other peers' ops", () => {
  beforeEach(async () => {
    await connect();
    open();
    await live(3, 7);
    messages.length = 0;
  });

  it("a durable op advances the cursor; an ephemeral op (seq 0) is delivered without moving it", () => {
    sockets.last().serverSend(peerOp(3, 8, "a"));
    expect(messages).toEqual([
      expect.objectContaining({ kind: "op", seq: 8, epoch: 3, peerId: "p:other", ephemeral: false }),
    ]);
    expect(handle.getPhase().seq).toBe(8);
    sockets.last().serverSend(peerOp(3, 0, "chunk"));
    expect(messages[1]).toMatchObject({ kind: "op", seq: 0, ephemeral: true });
    expect(handle.getPhase().seq).toBe(8);
  });

  it("ops arriving out of order are held and applied in sequence", () => {
    const socket = sockets.last();
    socket.serverSend(peerOp(3, 10, "c"));
    socket.serverSend(peerOp(3, 9, "b"));
    socket.serverSend(peerOp(3, 8, "a"));
    expect(messages.map((m) => (m.kind === "op" ? m.seq : m.kind))).toEqual([8, 9, 10]);
    expect(handle.getPhase().seq).toBe(10);
  });

  it("a duplicate or an already-covered seq is dropped", () => {
    const socket = sockets.last();
    socket.serverSend(peerOp(3, 8, "a"));
    socket.serverSend(peerOp(3, 8, "a"));
    socket.serverSend(peerOp(3, 7, "old"));
    socket.serverSend(peerOp(3, 2, "older"));
    expect(messages).toHaveLength(1);
    expect(handle.getPhase().seq).toBe(8);
  });

  it("a gap that never fills is resolved by a fresh snapshot, and the held ops are discarded", async () => {
    const socket = sockets.last();
    socket.serverSend(peerOp(3, 9, "b")); // 8 is missing
    expect(messages).toEqual([]);
    await advance(1_999);
    expect(hellos(socket)).toHaveLength(1);
    await advance(1);
    expect(hellos(socket)).toHaveLength(2);
    expect(handle.getPhase().phase).toBe("reloading");
    socket.serverSend(snapshot(3, 9));
    expect(messages).toEqual([expect.objectContaining({ kind: "snapshot", seq: 9 })]);
    expect(handle.getPhase()).toMatchObject({ phase: "live", seq: 9 });
  });

  it("a gap that fills in time cancels the resync", async () => {
    const socket = sockets.last();
    socket.serverSend(peerOp(3, 9, "b"));
    await advance(1_000);
    socket.serverSend(peerOp(3, 8, "a"));
    await advance(5_000);
    expect(hellos(socket)).toHaveLength(1);
    expect(messages.map((m) => (m.kind === "op" ? m.seq : m.kind))).toEqual([8, 9]);
  });

  it("holding more early ops than the bound triggers a resync at once", async () => {
    handle.dispose();
    open({ maxBuffered: 2 });
    await live(3, 7);
    const socket = sockets.last();
    socket.serverSend(peerOp(3, 9, "b"));
    socket.serverSend(peerOp(3, 10, "c"));
    expect(hellos(socket)).toHaveLength(2);
    socket.serverSend(peerOp(3, 11, "d"));
    expect(hellos(socket)).toHaveLength(3);
  });

  it("an op from an older epoch is dropped; an op from a newer epoch means we missed a reload and re-hello", () => {
    const socket = sockets.last();
    socket.serverSend(peerOp(2, 8, "stale"));
    expect(messages).toEqual([]);
    expect(handle.getPhase().seq).toBe(7);
    socket.serverSend(peerOp(4, 1, "newer"));
    expect(messages).toEqual([]);
    expect(hellos(socket)).toHaveLength(2);
  });

  it("an op whose payload is not an op is ignored", () => {
    sockets.last().serverSend(srv("op", 3, 8, { junk: true }));
    expect(messages).toEqual([]);
    expect(handle.getPhase().seq).toBe(7);
  });
});

describe("sending ops", () => {
  beforeEach(async () => {
    await connect();
    open();
    await live(3, 7);
  });

  const fields = (value: string) => ({ intent: "set_fields" as const, fields: { title: { value, ts: 1 } } });

  it("writes an op envelope at the current epoch with a fresh op_id and resolves on its ack", async () => {
    const socket = sockets.last();
    const pending = handle.sendOp(fields("x"));
    const [sent] = ops(socket);
    expect(sent).toMatchObject({ doc_id: DOC, doc_type: "artifact", epoch: 3, peer_id: "p:1", seq: 0, kind: "op" });
    expect(sent.payload).toEqual({ op_id: "op-1", intent: "set_fields", fields: { title: { value: "x", ts: 1 } } });
    expect(handle.getPhase().pending).toBe(1);
    socket.serverSend(srv("ack", 3, 8, { op_id: "op-1", seq: 8, changed: true }));
    await expect(pending).resolves.toEqual({ op_id: "op-1", seq: 8, changed: true } satisfies AckPayload);
    expect(handle.getPhase()).toMatchObject({ seq: 8, pending: 0 });
  });

  it("an ack with changed=false resolves without moving the cursor", async () => {
    const pending = handle.sendOp(fields("x"));
    sockets.last().serverSend(srv("ack", 3, 7, { op_id: "op-1", seq: 7, changed: false }));
    await expect(pending).resolves.toMatchObject({ changed: false });
    expect(handle.getPhase().seq).toBe(7);
  });

  it("our own acked op takes its place in the sequence: another peer's earlier op still applies first", async () => {
    const socket = sockets.last();
    const pending = handle.sendOp(fields("x"));
    // Our op landed at seq 9; peer's seq 8 is still in flight over the hub.
    socket.serverSend(srv("ack", 3, 9, { op_id: "op-1", seq: 9, changed: true }));
    await pending;
    expect(handle.getPhase().seq).toBe(7);
    socket.serverSend(peerOp(3, 8, "a"));
    expect(messages.filter((m) => m.kind === "op").map((m) => m.seq)).toEqual([8]);
    expect(handle.getPhase().seq).toBe(9);
  });

  it("acks resolve by op_id, whichever order they arrive in", async () => {
    const socket = sockets.last();
    const first = handle.sendOp(fields("a"));
    const second = handle.sendOp(fields("b"));
    socket.serverSend(srv("ack", 3, 9, { op_id: "op-2", seq: 9, changed: true }));
    socket.serverSend(srv("ack", 3, 8, { op_id: "op-1", seq: 8, changed: true }));
    await expect(second).resolves.toMatchObject({ op_id: "op-2", seq: 9 });
    await expect(first).resolves.toMatchObject({ op_id: "op-1", seq: 8 });
    expect(handle.getPhase().seq).toBe(9);
  });

  it("the server refuses an op: the error settles the oldest outstanding op only", async () => {
    const socket = sockets.last();
    const first = settled(handle.sendOp(fields("a")));
    const second = handle.sendOp(fields("b"));
    socket.serverSend(srv("error", 3, 0, { code: "forbidden", message: "only the owner" }));
    expect(await first).toMatchObject({ status: "rejected", error: { code: "forbidden", message: "only the owner" } });
    expect(handle.getPhase().pending).toBe(1);
    socket.serverSend(srv("ack", 3, 8, { op_id: "op-2", seq: 8, changed: true }));
    await expect(second).resolves.toMatchObject({ op_id: "op-2" });
  });

  it("stale epoch: the op is rejected, exactly one hello follows, the new snapshot replaces the state, later ops use the new epoch", async () => {
    const socket = sockets.last();
    const pending = settled(handle.sendOp(fields("a")));
    socket.serverSend(srv("error", 4, 0, { code: "stale_epoch", message: "moved on" }));
    socket.serverSend(srv("reload", 4, 0, { epoch: 4, reason: "stale_epoch" }));
    await advance(0);
    expect(await pending).toMatchObject({ status: "rejected", error: { code: "stale_epoch", message: "moved on" } });
    expect(hellos(socket)).toHaveLength(2);
    expect(handle.getPhase().phase).toBe("reloading");
    expect(messages).toContainEqual({ kind: "reload", epoch: 4, reason: "stale_epoch" });

    socket.serverSend(snapshot(4, 0, { kb_version: 3, fields: {} }));
    expect(handle.getPhase()).toMatchObject({ phase: "live", epoch: 4, seq: 0 });
    fire(handle.sendOp(fields("b")));
    expect(ops(socket).at(-1)?.epoch).toBe(4);
  });

  it("two stale ops in flight cost one hello, not two", async () => {
    const socket = sockets.last();
    const a = settled(handle.sendOp(fields("a")));
    const b = settled(handle.sendOp(fields("b")));
    socket.serverSend(srv("error", 4, 0, { code: "stale_epoch", message: "" }));
    socket.serverSend(srv("reload", 4, 0, { epoch: 4, reason: "stale_epoch" }));
    socket.serverSend(srv("error", 4, 0, { code: "stale_epoch", message: "" }));
    socket.serverSend(srv("reload", 4, 0, { epoch: 4, reason: "stale_epoch" }));
    expect((await a).status).toBe("rejected");
    expect((await b).status).toBe("rejected");
    expect(hellos(socket)).toHaveLength(2);
  });

  it("a reload broadcast while an op is in flight re-hellos but leaves the op to the server's answer", async () => {
    const socket = sockets.last();
    const pending = handle.sendOp(fields("a"));
    socket.serverSend(srv("reload", 4, 0, { epoch: 4, reason: "publisher_snapshot" }));
    expect(hellos(socket)).toHaveLength(2);
    // The server applied it before the rebuild after all: its ack still arrives and still counts.
    socket.serverSend(srv("ack", 3, 8, { op_id: "op-1", seq: 8, changed: true }));
    await expect(pending).resolves.toMatchObject({ op_id: "op-1" });
  });

  it("a reload at or below the current epoch is ignored while live", () => {
    const socket = sockets.last();
    socket.serverSend(srv("reload", 3, 0, { epoch: 3, reason: "late" }));
    socket.serverSend(srv("reload", 2, 0, { epoch: 2, reason: "later still" }));
    expect(hellos(socket)).toHaveLength(1);
    expect(handle.getPhase().phase).toBe("live");
  });

  it("ops queued while connecting flush after the snapshot, in order, each with its own op_id", async () => {
    handle.dispose();
    open();
    const socket = sockets.last();
    fire(handle.sendOp(fields("a")));
    fire(handle.sendOp(fields("b")));
    expect(ops(socket)).toHaveLength(0);
    expect(handle.getPhase().pending).toBe(2);
    await live(5, 1);
    expect(ops(socket).map((e) => (e.payload as { op_id: string }).op_id)).toEqual(["op-1", "op-2"]);
    expect(ops(socket).every((e) => e.epoch === 5)).toBe(true);
  });

  it("a socket-level reset re-hellos the document", () => {
    sockets.last().serverSend({ t: "reset", reason: "overflow" });
    expect(hellos(sockets.last())).toHaveLength(2);
    expect(handle.getPhase().phase).toBe("reloading");
  });

  it("a dropped socket rejects in-flight ops as disconnected, keeps queued ones, and re-hellos with epoch 0 after the reconnect", async () => {
    const socket = sockets.last();
    const inFlight = settled(handle.sendOp(fields("a")));
    socket.serverClose(1006);
    await advance(0);
    expect(await inFlight).toMatchObject({ status: "rejected", error: { code: "disconnected", message: "the live connection dropped" } });
    expect(handle.getPhase().phase).toBe("down");
    const queued = handle.sendOp(fields("b"));
    expect(handle.getPhase().pending).toBe(1);

    await advance(2_000);
    const next = sockets.last();
    next.welcome("p:2");
    next.serverSend({ t: "subscribed", channel: CHANNEL, can_write: true });
    expect(hellos(next)).toEqual([expect.objectContaining({ epoch: 0, peer_id: "p:2", kind: "hello" })]);
    next.serverSend(snapshot(3, 12));
    expect(ops(next)).toHaveLength(1);
    expect(ops(next)[0]).toMatchObject({ epoch: 3, peer_id: "p:2" });
    next.serverSend(srv("ack", 3, 13, { op_id: "op-2", seq: 13, changed: true }));
    await expect(queued).resolves.toMatchObject({ seq: 13 });
  });

  it("dispose releases the channel and rejects everything pending", async () => {
    const socket = sockets.last();
    const inFlight = settled(handle.sendOp(fields("a")));
    handle.dispose();
    expect(socket.framesOf("unsubscribe").map((f) => f.channel)).toEqual([CHANNEL]);
    expect(await inFlight).toMatchObject({ status: "rejected", error: { code: "disposed", message: "the document was closed" } });
    expect(await settled(handle.sendOp(fields("b")))).toMatchObject({ status: "rejected", error: { code: "disposed" } });
    // Frames after dispose are not ours any more.
    socket.serverSend(peerOp(3, 8, "late"));
    expect(messages.filter((m) => m.kind === "op")).toHaveLength(0);
  });
});
