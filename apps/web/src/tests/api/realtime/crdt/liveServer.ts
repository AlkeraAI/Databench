// A stand-in for the server's half of the Loro lane, for the browser's tests.
//
// It holds a real Loro document per channel and answers the frames a channel
// sends the way the gateway does: `subscribed` (or a refusal), a `snapshot` for
// a `hello` (only what the vector lacks, when it can), an `ack` with the
// vector after "committing" an update, and the update broadcast to every other
// socket. Frames are queued per socket and delivered when the test says so,
// so a test can reorder, drop and delay them.

import * as LoroNode from "loro-crdt/nodejs";

import type { ClientFrame, EnvelopeKind, ServerFrame, SocketLimits } from "@/api/realtime/wsClient";
import type { LiveSocket } from "@/api/realtime/crdt/channel";
import type { LoroApi } from "@/api/realtime/crdt/loro";
import { fromBase64, toBase64 } from "@/api/realtime/crdt/bytes";
import { ChunkAssembler, CHUNK_BYTES, isChunk, split } from "@/api/realtime/crdt/chunks";
import { LIVE_DOC_TYPES, contentTextOf, type LiveDocType } from "@/api/realtime/crdt/docTypes";

export const loroNode = LoroNode as unknown as LoroApi;

const isLiveDocType = (value: string | undefined): value is LiveDocType => value !== undefined && Object.hasOwn(LIVE_DOC_TYPES, value);

/** The live document type a `doc:<type>:<id>` channel names. */
function docTypeOf(channel: string): LiveDocType {
  const docType = channel.split(":", 3)[1];
  if (!isLiveDocType(docType)) throw new Error(`not a live document channel: ${channel}`);
  return docType;
}

type Doc = InstanceType<typeof LoroNode.LoroDoc>;

export class FakeSocket implements LiveSocket {
  peerId: string | null;
  limits: SocketLimits | null = null;
  readonly inbox: ServerFrame[] = [];
  readonly sent: ClientFrame[] = [];
  connected = true;
  private frameListeners = new Set<(f: ServerFrame) => void>();
  private openListeners = new Set<(p: string) => void>();
  readonly held = new Set<string>();

  constructor(
    readonly server: LiveServer,
    readonly name: string,
  ) {
    this.peerId = `p:${name}`;
  }

  subscribe(channel: string): () => void {
    this.held.add(channel);
    if (this.connected) this.server.receive(this, { t: "subscribe", channel } as ClientFrame);
    return () => {
      this.held.delete(channel);
      if (this.connected) this.server.receive(this, { t: "unsubscribe", channel } as ClientFrame);
    };
  }

  send(frame: ClientFrame): boolean {
    if (!this.connected) return false;
    this.sent.push(frame);
    this.server.receive(this, frame);
    return true;
  }

  onFrame(listener: (f: ServerFrame) => void): () => void {
    this.frameListeners.add(listener);
    return () => void this.frameListeners.delete(listener);
  }

  onOpen(listener: (p: string) => void): () => void {
    this.openListeners.add(listener);
    return () => void this.openListeners.delete(listener);
  }

  /** Deliver every queued frame (or the first `n`), awaiting each handler's work. */
  async deliver(n = Infinity): Promise<void> {
    let count = 0;
    while (this.inbox.length > 0 && count < n) {
      const frame = this.inbox.shift()!;
      for (const l of [...this.frameListeners]) l(frame);
      count += 1;
      await settle();
    }
  }

  /** Drop the queued frames on the floor (a lost connection's tail). */
  drop(): void {
    this.inbox.length = 0;
  }

  disconnect(): void {
    this.connected = false;
    this.inbox.length = 0;
  }

  /** A new socket: welcomed, held channels re-subscribed, listeners told. */
  reconnect(): void {
    this.connected = true;
    this.peerId = `p:${this.name}:${Math.random().toString(36).slice(2, 6)}`;
    for (const channel of this.held) this.server.receive(this, { t: "subscribe", channel } as ClientFrame);
    for (const l of [...this.openListeners]) l(this.peerId);
  }

  sentKinds(): string[] {
    return this.sent.flatMap((f) => (f.t === "doc" ? [String(f.envelope.kind)] : []));
  }

  sentUpdates(): Record<string, unknown>[] {
    return this.sent.flatMap((f) =>
      f.t === "doc" && f.envelope.kind === "crdt" && f.envelope.payload.t === "update" ? [f.envelope.payload] : [],
    );
  }
}

/** Let promise chains (Loro, digests, awaits in handlers) run. */
export async function settle(): Promise<void> {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
}

interface ServerDoc {
  doc: Doc;
  epoch: number;
}

export class LiveServer {
  /** Channels whose subscribe is refused, and with which code. */
  refused = new Map<string, string>();
  /** Answer the next update with this error instead of committing it. */
  failNext: { code: string; retry_after_ms?: number } | null = null;
  /** Send the next sync with a vector nobody can decode. */
  corruptNextSync = false;
  /** Send syncs without a peer, as a reader is sent them. */
  omitPeer = false;
  /** Refuse every caret as the gateway refuses a forged one. */
  refuseCarets = false;
  /** Answer every caret busy, as a sandbox under load does. */
  busyCarets = false;
  /** Answer this many updates busy before taking any. */
  busyUpdates = 0;
  /** Swallow acks (the socket keeps waiting). */
  muteAcks = false;
  /** Refuse this many hellos as busy (a sandbox restarting). */
  refuseHellos = 0;
  /** Never answer this many hellos (a server stuck behind something). */
  ignoreHellos = 0;
  /** Never answer this many subscribes (the answer lost to a socket that
   *  restarted in between). */
  ignoreSubscribes = 0;
  /** Refuse every hello with this code (a file the server will not open). */
  refuseHellosWith: string | null = null;
  /** The finer reason those refusals carry, as the gateway sends a seed's. */
  refuseHellosReason: string | null = null;
  /** Whether each channel's edits are saving, as the document's row says:
   *  carried by every sync and every reload, as the gateway carries it. A
   *  channel with no entry rests nowhere (or is served by an older server),
   *  and its frames carry nothing. */
  readonly saving = new Map<string, { state: "ok" | "paused"; reason: string }>();
  /** Sockets (by name) that may only read: told so, and refused writing. */
  readonly readers = new Set<string>();
  readonly docs = new Map<string, ServerDoc>();
  readonly sockets: FakeSocket[] = [];
  private nextPeer = 5000;
  private peers = new Map<FakeSocket, number>();
  private assembler = new ChunkAssembler();

  socket(name: string): FakeSocket {
    const s = new FakeSocket(this, name);
    this.sockets.push(s);
    return s;
  }

  docOf(channel: string): ServerDoc {
    let d = this.docs.get(channel);
    if (!d) {
      const doc = new LoroNode.LoroDoc();
      doc.setPeerId(1n);
      d = { doc, epoch: 1 };
      this.docs.set(channel, d);
    }
    return d;
  }

  /** The root text a channel's document keeps its content in. */
  textOf(channel: string): string {
    return contentTextOf(docTypeOf(channel));
  }

  text(channel: string): string {
    return this.docOf(channel).doc.getText(this.textOf(channel)).toString();
  }

  /** Seed the server's document (a draft from the op log, a file from the drive). */
  seed(channel: string, text: string): void {
    const { doc } = this.docOf(channel);
    doc.getText(this.textOf(channel)).insert(0, text);
    doc.commit();
  }

  /** Somebody else's edit, made on the server's copy and broadcast to every tab
   *  (as another browser's, or an outside change merged in). */
  edit(channel: string, at: number, insert: string, remove = 0): void {
    const server = this.docOf(channel);
    const before = server.doc.oplogVersion();
    server.doc.setPeerId(3n);
    const text = server.doc.getText(this.textOf(channel));
    if (remove > 0) text.delete(at, remove);
    if (insert) text.insert(at, insert);
    server.doc.commit();
    const delta = server.doc.export({ mode: "update", from: before });
    const vv = toBase64(server.doc.oplogVersion().encode());
    for (const other of this.sockets) {
      if (!other.held.has(channel) || !other.connected) continue;
      other.inbox.push(
        this.envelope(channel, server.epoch, "crdt", { t: "update", update_id: `srv-${at}`, data_b64: toBase64(delta), vv_b64: vv, loro_peer: 3 }, "p:other"),
      );
    }
  }

  /** Restart the history: a new epoch holding the current text, every tab told. */
  rotate(channel: string): void {
    const old = this.docOf(channel);
    const text = old.doc.getText(this.textOf(channel)).toString();
    const doc = new LoroNode.LoroDoc();
    doc.setPeerId(2n);
    doc.getText(this.textOf(channel)).insert(0, text);
    doc.commit();
    const epoch = old.epoch + 1;
    this.docs.set(channel, { doc, epoch });
    for (const s of this.sockets) {
      if (s.held.has(channel)) s.inbox.push(this.envelope(channel, epoch, "reload", { epoch, reason: "compacted", ...this.savingOf(channel) }));
    }
  }

  /** The server can no longer serve the document: every tab on the channel
   *  is told `crdt_unsupported`. */
  unsupport(channel: string): void {
    const { epoch } = this.docOf(channel);
    for (const s of this.sockets) {
      if (s.held.has(channel)) s.inbox.push(this.envelope(channel, epoch, "error", { code: "crdt_unsupported" }));
    }
  }

  /** Send every tab on the channel a frame of the server's choosing. */
  tell(channel: string, kind: EnvelopeKind, payload: Record<string, unknown>): void {
    const { epoch } = this.docOf(channel);
    for (const s of this.sockets) if (s.held.has(channel)) s.inbox.push(this.envelope(channel, epoch, kind, payload));
  }

  private savingOf(channel: string): { saving?: { state: string; reason: string } } {
    const saving = this.saving.get(channel);
    return saving === undefined ? {} : { saving };
  }

  private envelope(channel: string, epoch: number, kind: EnvelopeKind, payload: Record<string, unknown>, peer = "srv:0"): ServerFrame {
    const docId = channel.split(":", 3)[2]!;
    return {
      t: "doc",
      envelope: { doc_id: docId, doc_type: docTypeOf(channel), epoch, peer_id: peer, seq: 0, kind, payload },
    };
  }

  receive(socket: FakeSocket, frame: ClientFrame): void {
    if (frame.t === "subscribe") {
      if (this.ignoreSubscribes > 0) {
        this.ignoreSubscribes -= 1;
        return;
      }
      const refusal = this.refused.get(frame.channel);
      if (refusal !== undefined) {
        socket.inbox.push({ t: "error", code: refusal, message: "refused", channel: frame.channel });
      } else {
        socket.inbox.push({ t: "subscribed", channel: frame.channel, can_write: !this.readers.has(socket.name) });
      }
      return;
    }
    if (frame.t !== "doc") return;
    const env = frame.envelope;
    const channel = `doc:${env.doc_type}:${env.doc_id}`;
    const server = this.docOf(channel);
    if (env.kind === "hello") {
      if (this.ignoreHellos > 0) {
        this.ignoreHellos -= 1;
        return;
      }
      if (this.refuseHellosWith !== null) {
        const reason = this.refuseHellosReason !== null ? { reason: this.refuseHellosReason } : {};
        socket.inbox.push(this.envelope(channel, Math.max(env.epoch, 1), "error", { code: this.refuseHellosWith, ...reason }));
        return;
      }
      if (this.refuseHellos > 0) {
        this.refuseHellos -= 1;
        socket.inbox.push(this.envelope(channel, Math.max(env.epoch, 1), "error", { code: "crdt_busy", retry_after_ms: 300 }));
        return;
      }
      const p = env.payload;
      let peer = this.peers.get(socket);
      if (peer === undefined || p.loro_peer === undefined || Number(p.loro_peer) !== peer) {
        peer = this.nextPeer++;
        this.peers.set(socket, peer);
      }
      let mode = "snapshot";
      let data: Uint8Array = server.doc.export({ mode: "snapshot" });
      if (typeof p.vv_b64 === "string" && p.epoch_seen === server.epoch) {
        mode = "updates";
        data = server.doc.export({ mode: "update", from: LoroNode.VersionVector.decode(fromBase64(p.vv_b64)) });
      }
      const corrupt = this.corruptNextSync;
      this.corruptNextSync = false;
      const common = {
        mode,
        vv_b64: corrupt ? "!!not base64!!" : toBase64(server.doc.oplogVersion().encode()),
        ...(this.omitPeer ? {} : { loro_peer: peer }),
        doc_schema: 1,
        limits: { chunk_bytes: CHUNK_BYTES, max_update_bytes: 512 * 1024, max_doc_bytes: 2 * 1024 * 1024, max_text_bytes: 16 * 1024 },
        ...this.savingOf(channel),
      };
      if (data.length <= CHUNK_BYTES) {
        socket.inbox.push(this.envelope(channel, server.epoch, "snapshot", { ...common, data_b64: toBase64(data) }));
      } else {
        void split(data, `sync-${peer}`).then((pieces) => {
          for (const chunk of pieces) socket.inbox.push(this.envelope(channel, server.epoch, "snapshot", { ...common, chunk }));
        });
      }
      return;
    }
    if (env.kind === "crdt" && env.payload.t === "update" && this.busyUpdates > 0) {
      this.busyUpdates -= 1;
      const id = String(env.payload.update_id ?? "");
      socket.inbox.push(this.envelope(channel, server.epoch, "error", { code: "crdt_busy", retry_after_ms: 250, update_id: id }));
      return;
    }
    if (env.kind === "crdt" && env.payload.t === "update" && this.readers.has(socket.name)) {
      const id = String(env.payload.update_id ?? "");
      socket.inbox.push(this.envelope(channel, server.epoch, "error", { code: "forbidden", update_id: id }));
      return;
    }
    if (env.kind === "crdt" && env.payload.t === "update") {
      void this.update(socket, channel, server, env.epoch, env.payload);
    }
    if (env.kind === "crdt" && env.payload.t === "ephemeral" && this.busyCarets) {
      socket.inbox.push(
        this.envelope(channel, server.epoch, "error", { code: "crdt_busy", reason: "ephemeral", message: "busy" }),
      );
      return;
    }
    if (env.kind === "crdt" && env.payload.t === "ephemeral" && this.refuseCarets) {
      socket.inbox.push(
        this.envelope(channel, server.epoch, "error", { code: "crdt_rejected", reason: "ephemeral", message: "refused" }),
      );
      return;
    }
    if (env.kind === "crdt" && env.payload.t === "ephemeral") {
      // Relayed to everyone else, stamped with who sent it.
      const stamped = {
        t: "ephemeral",
        data_b64: env.payload.data_b64,
        loro_peer: this.peers.get(socket),
        user_id: `user-${socket.name}`,
        display_name: socket.name.toUpperCase(),
        email: `${socket.name}@acme.test`,
      };
      for (const other of this.sockets) {
        if (other === socket || !other.held.has(channel) || !other.connected) continue;
        other.inbox.push(this.envelope(channel, server.epoch, "crdt", stamped, socket.peerId ?? ""));
      }
    }
  }

  private async update(socket: FakeSocket, channel: string, server: ServerDoc, epoch: number, p: Record<string, unknown>): Promise<void> {
    const id = String(p.update_id);
    let data: Uint8Array;
    if (isChunk(p.chunk)) {
      const whole = await this.assembler.add(p.chunk);
      if (whole === null) return;
      data = whole;
    } else {
      data = fromBase64(String(p.data_b64 ?? ""));
    }
    if (epoch !== server.epoch) {
      socket.inbox.push(this.envelope(channel, server.epoch, "error", { code: "stale_epoch", update_id: id }));
      socket.inbox.push(this.envelope(channel, server.epoch, "reload", { epoch: server.epoch, reason: "stale_epoch", ...this.savingOf(channel) }));
      return;
    }
    if (this.failNext !== null) {
      const failure = this.failNext;
      this.failNext = null;
      socket.inbox.push(this.envelope(channel, server.epoch, "error", { ...failure, update_id: id }));
      return;
    }
    const before = server.doc.oplogVersion();
    const status = server.doc.import(data);
    if (status.pending !== null && status.pending.size > 0) {
      socket.inbox.push(this.envelope(channel, server.epoch, "error", { code: "crdt_resync", update_id: id }));
      return;
    }
    const delta = server.doc.export({ mode: "update", from: before });
    const vv = toBase64(server.doc.oplogVersion().encode());
    if (!this.muteAcks) {
      socket.inbox.push(this.envelope(channel, server.epoch, "ack", { update_id: id, changed: delta.length > 0, vv_b64: vv }));
    }
    for (const other of this.sockets) {
      if (other === socket || !other.held.has(channel) || !other.connected) continue;
      other.inbox.push(
        this.envelope(channel, server.epoch, "crdt", { t: "update", update_id: id, data_b64: toBase64(delta), vv_b64: vv, loro_peer: this.peers.get(socket) }, socket.peerId ?? ""),
      );
    }
  }
}
