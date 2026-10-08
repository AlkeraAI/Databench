// One synchronised document over the shared socket.
//
// The server owns a document's `epoch` and `seq`. A handle opened here subscribes the
// document's channel, says `hello` once the server confirms the subscription, adopts the
// `snapshot` that answers, and from then on applies the durable operations other peers make
// in server-sequence order, sends the caller's own operations at the current epoch and
// resolves each with its `ack`. Every rule the README spells for a client lives here:
//
// * an envelope from an older epoch is dropped; a `reload` (or an `error{stale_epoch}` on
//   one of our own ops) means the state was rebuilt — pending ops are refused, the state is
//   discarded and a fresh `hello` fetches the new epoch's snapshot — never silent divergence;
// * durable ops are applied strictly in `seq` order: one that arrives early is held until the
//   gap fills (replicas deliver the same rows, not always in the same instant), a duplicate
//   is dropped, and a gap that does not fill in time is resolved by a fresh snapshot;
// * ephemeral ops (`seq 0`) are delivered as they come and never move the cursor;
// * the server answers each op we send in the order we sent them — an `ack` names its
//   `op_id`, an `error` does not — so outstanding requests form a queue and an error settles
//   its head; nothing is removed from that queue locally, or the next answer would be
//   attributed to the wrong request;
// * a socket-level `reset` or a reconnect re-`hello`s the document.

import {
  channelOf,
  type DocType,
  type Envelope,
  type OpIntent,
  type ServerFrame,
  type WsClient,
} from "./wsClient";
import { DOC_SYNC_GAP_TIMEOUT_MS, DOC_SYNC_MAX_BUFFERED_OPS } from "@/lib/limits";

export type { DocType, Envelope, EnvelopeKind, OpIntent } from "./wsClient";

export interface FieldWrite<V = unknown> {
  value: V;
  ts: number;
}

/** The payload of an `op` envelope; `op_id` is echoed by the ack. */
export interface OpPayload {
  op_id: string;
  intent: OpIntent;
  events?: unknown[];
  fields?: Record<string, FieldWrite>;
  meta?: Record<string, unknown>;
}

export type OpInput = Omit<OpPayload, "op_id">;

/** Whether a rebroadcast op's payload has the two fields every op carries. */
export function isOpPayload(value: unknown): value is OpPayload {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { op_id?: unknown }).op_id === "string" &&
    typeof (value as { intent?: unknown }).intent === "string"
  );
}

export interface AckPayload {
  op_id: string;
  seq: number;
  changed: boolean;
}

/** Why an op was not applied: the server's refusal code, a stale epoch, a dropped socket
 *  (`disconnected`) or a closed handle (`disposed`). */
export class DocOpError extends Error {
  readonly code: string;

  constructor(code: string, message = "") {
    super(message || code);
    this.name = "DocOpError";
    this.code = code;
  }
}

export type DocPhaseName = "connecting" | "live" | "reloading" | "down" | "error";

export interface DocPhase {
  phase: DocPhaseName;
  epoch: number;
  seq: number;
  peerId: string | null;
  canWrite: boolean;
  /** Ops sent and awaiting the server's answer, plus ops queued until the document is live. */
  pending: number;
  /** The code the server refused the document with, when `phase` is `error`. */
  error: string | null;
}

export type DocMessage<S> =
  | { kind: "snapshot"; state: S; epoch: number; seq: number }
  | { kind: "op"; payload: OpPayload; peerId: string; epoch: number; seq: number; ephemeral: boolean }
  | { kind: "reload"; epoch: number; reason: string };

export interface DocHandle<S> {
  onMessage(listener: (message: DocMessage<S>) => void): () => void;
  onPhase(listener: (phase: DocPhase) => void): () => void;
  getPhase(): DocPhase;
  /** A durable op: queued while the document is not live, resolved with its ack, rejected
   *  with `{code}` when the server refuses it, the epoch moved under it (`stale_epoch`), the
   *  socket dropped before it was answered (`disconnected`) or the handle was disposed. */
  sendOp(op: OpInput): Promise<AckPayload>;
  dispose(): void;
}

export interface OpenDocOptions {
  /** How long a hole in the sequence may stay open before a fresh snapshot is fetched. */
  gapTimeoutMs?: number;
  /** How many early ops may be held before a fresh snapshot is fetched instead. */
  maxBuffered?: number;
  opId?: () => string;
  timers?: { setTimeout: (fn: () => void, ms: number) => unknown; clearTimeout: (handle: unknown) => void };
}

export const DEFAULT_GAP_TIMEOUT_MS = DOC_SYNC_GAP_TIMEOUT_MS;
export const DEFAULT_MAX_BUFFERED = DOC_SYNC_MAX_BUFFERED_OPS;

let opCounter = 0;
export function defaultOpId(): string {
  opCounter += 1;
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(36).slice(2, 10);
  return `op-${random}-${opCounter}`;
}

type Outstanding =
  | { kind: "hello" }
  | { kind: "op"; opId: string; resolve: (ack: AckPayload) => void; reject: (error: DocOpError) => void };

interface Queued {
  payload: OpPayload;
  resolve: (ack: AckPayload) => void;
  reject: (error: DocOpError) => void;
}

const DEFAULT_TIMERS = {
  setTimeout: (fn: () => void, ms: number): unknown => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle: unknown): void => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export function openDoc<S>(
  client: WsClient,
  docType: DocType,
  docId: string,
  opts: OpenDocOptions = {},
): DocHandle<S> {
  const channel = channelOf(docType, docId);
  const gapTimeoutMs = opts.gapTimeoutMs ?? DEFAULT_GAP_TIMEOUT_MS;
  const maxBuffered = opts.maxBuffered ?? DEFAULT_MAX_BUFFERED;
  const opId = opts.opId ?? defaultOpId;
  const timers = opts.timers ?? DEFAULT_TIMERS;

  const messageListeners = new Set<(message: DocMessage<S>) => void>();
  const phaseListeners = new Set<(phase: DocPhase) => void>();

  let phase: DocPhaseName = "connecting";
  let epoch = 0;
  let seq = 0;
  let canWrite = false;
  let errorCode: string | null = null;
  let disposed = false;
  let helloPending = false;
  const outstanding: Outstanding[] = [];
  const queued: Queued[] = [];
  const early = new Map<number, () => void>();
  let gapTimer: unknown = null;

  const emit = (message: DocMessage<S>): void => {
    for (const listener of messageListeners) listener(message);
  };
  const snapshotPhase = (): DocPhase => ({
    phase,
    epoch,
    seq,
    peerId: client.peerId,
    canWrite,
    pending: outstanding.filter((o) => o.kind === "op").length + queued.length,
    error: errorCode,
  });
  const announce = (): void => {
    const current = snapshotPhase();
    for (const listener of phaseListeners) listener(current);
  };
  const setPhase = (next: DocPhaseName, code: string | null = null): void => {
    phase = next;
    errorCode = code;
    announce();
  };

  const clearGap = (): void => {
    if (gapTimer !== null) {
      timers.clearTimeout(gapTimer);
      gapTimer = null;
    }
    early.clear();
  };

  const rejectOutstanding = (error: DocOpError): void => {
    for (const entry of outstanding.splice(0)) if (entry.kind === "op") entry.reject(error);
    helloPending = false;
  };

  const sendEnvelope = (kind: "hello" | "op", payload: Record<string, unknown>): boolean => {
    const peerId = client.peerId;
    if (peerId === null) return false;
    return client.send({
      t: "doc",
      envelope: {
        doc_id: docId,
        doc_type: docType,
        // A hello says "I have no state yet"; an op names the epoch it was made against.
        epoch: kind === "hello" ? 0 : epoch,
        peer_id: peerId,
        seq: 0,
        kind,
        payload,
      },
    });
  };

  const ensureHello = (): void => {
    if (disposed || helloPending) return;
    if (!sendEnvelope("hello", {})) return;
    helloPending = true;
    outstanding.push({ kind: "hello" });
    setPhase(epoch === 0 ? "connecting" : "reloading");
  };

  const writeOp = (entry: Queued): boolean => {
    if (!sendEnvelope("op", entry.payload as unknown as Record<string, unknown>)) return false;
    outstanding.push({ kind: "op", opId: entry.payload.op_id, resolve: entry.resolve, reject: entry.reject });
    return true;
  };

  const flushQueued = (): void => {
    while (phase === "live" && queued.length > 0) {
      const next = queued[0];
      if (!writeOp(next)) break;
      queued.shift();
    }
    announce();
  };

  /** A sequenced item (another peer's durable op, or our own acked op) at `at`. */
  const sequence = (at: number, apply: () => void): void => {
    if (at <= seq) return; // a duplicate, or already covered by a snapshot
    if (at === seq + 1) {
      seq = at;
      apply();
      // Drain whatever became contiguous.
      for (;;) {
        const next = early.get(seq + 1);
        if (!next) break;
        early.delete(seq + 1);
        seq += 1;
        next();
      }
      if (early.size === 0 && gapTimer !== null) {
        timers.clearTimeout(gapTimer);
        gapTimer = null;
      }
      announce();
      return;
    }
    // Early: hold it until the gap fills, or give up and resync.
    early.set(at, apply);
    if (early.size > maxBuffered) {
      clearGap();
      ensureHello();
      return;
    }
    if (gapTimer === null) {
      gapTimer = timers.setTimeout(() => {
        gapTimer = null;
        if (early.size > 0) {
          early.clear();
          ensureHello();
        }
      }, gapTimeoutMs);
    }
  };

  const settleAck = (ack: AckPayload): void => {
    const index = outstanding.findIndex((o) => o.kind === "op" && o.opId === ack.op_id);
    if (index === -1) return;
    const [entry] = outstanding.splice(index, 1);
    if (entry.kind !== "op") return;
    if (ack.changed) sequence(ack.seq, () => undefined);
    entry.resolve(ack);
    announce();
  };

  const settleError = (error: DocOpError, envelopeEpoch: number): void => {
    const head = outstanding.shift();
    if (head === undefined) return;
    if (head.kind === "hello") {
      helloPending = false;
      setPhase("error", error.code);
      return;
    }
    head.reject(error);
    if (error.code === "stale_epoch") {
      // The server names the current epoch on the frame; the reload that follows re-hellos.
      clearGap();
      if (envelopeEpoch > epoch) setPhase("reloading");
    }
    announce();
  };

  const handleEnvelope = (envelope: Envelope): void => {
    switch (envelope.kind) {
      case "snapshot": {
        const helloIndex = outstanding.findIndex((o) => o.kind === "hello");
        if (helloIndex !== -1) outstanding.splice(helloIndex, 1);
        helloPending = false;
        clearGap();
        epoch = envelope.epoch;
        const payload = envelope.payload as { state?: unknown; seq?: unknown };
        seq = typeof payload.seq === "number" ? payload.seq : envelope.seq;
        errorCode = null;
        phase = "live";
        emit({ kind: "snapshot", state: (payload.state ?? {}) as S, epoch, seq });
        flushQueued();
        return;
      }
      case "ack": {
        const payload = envelope.payload as Partial<AckPayload>;
        if (typeof payload.op_id !== "string" || typeof payload.seq !== "number") return;
        settleAck({ op_id: payload.op_id, seq: payload.seq, changed: payload.changed === true });
        return;
      }
      case "error": {
        const payload = envelope.payload as Partial<DocOpError>;
        settleError(
          new DocOpError(typeof payload.code === "string" ? payload.code : "error", payload.message ?? ""),
          envelope.epoch,
        );
        return;
      }
      case "reload": {
        const payload = envelope.payload as { epoch?: unknown; reason?: unknown };
        const target = typeof payload.epoch === "number" ? payload.epoch : envelope.epoch;
        const reason = typeof payload.reason === "string" ? payload.reason : "";
        if (target <= epoch && phase === "live") return; // already at (or past) that epoch
        clearGap();
        emit({ kind: "reload", epoch: target, reason });
        ensureHello();
        return;
      }
      case "op": {
        if (phase !== "live" && phase !== "reloading") return; // no state to apply it to yet
        if (envelope.epoch < epoch) return; // a stale writer's op, refused by the server too
        if (envelope.epoch > epoch) {
          // We are the stale one and missed the reload: resync.
          clearGap();
          ensureHello();
          return;
        }
        if (!isOpPayload(envelope.payload)) return;
        const payload = envelope.payload;
        if (envelope.seq === 0) {
          emit({ kind: "op", payload, peerId: envelope.peer_id, epoch: envelope.epoch, seq: 0, ephemeral: true });
          return;
        }
        sequence(envelope.seq, () =>
          emit({ kind: "op", payload, peerId: envelope.peer_id, epoch: envelope.epoch, seq: envelope.seq, ephemeral: false }),
        );
        return;
      }
      default:
        return; // hello / presence / crdt never arrive addressed to a client
    }
  };

  const onFrame = (frame: ServerFrame): void => {
    if (disposed) return;
    switch (frame.t) {
      case "subscribed":
        if (frame.channel !== channel) return;
        canWrite = frame.can_write;
        announce();
        ensureHello();
        return;
      case "doc":
        if (channelOf(frame.envelope.doc_type, frame.envelope.doc_id) !== channel) return;
        handleEnvelope(frame.envelope);
        return;
      case "error":
        if (frame.channel !== channel) return;
        // The subscription itself was refused: nothing will ever be delivered.
        clearGap();
        rejectOutstanding(new DocOpError(frame.code, frame.message));
        for (const entry of queued.splice(0)) entry.reject(new DocOpError(frame.code, frame.message));
        setPhase("error", frame.code);
        return;
      case "reset":
        // The socket fell behind: every channel re-hellos.
        clearGap();
        ensureHello();
        return;
      default:
        return;
    }
  };

  const onClose = (): void => {
    if (disposed) return;
    clearGap();
    rejectOutstanding(new DocOpError("disconnected", "the live connection dropped"));
    setPhase("down");
  };

  const offFrame = client.onFrame(onFrame);
  const offClose = client.onClose(onClose);
  const release = client.subscribe(channel);

  return {
    onMessage(listener) {
      messageListeners.add(listener);
      return () => void messageListeners.delete(listener);
    },
    onPhase(listener) {
      phaseListeners.add(listener);
      return () => void phaseListeners.delete(listener);
    },
    getPhase: snapshotPhase,
    sendOp(op) {
      return new Promise<AckPayload>((resolve, reject) => {
        if (disposed) {
          reject(new DocOpError("disposed", "the document was closed"));
          return;
        }
        const entry: Queued = { payload: { ...op, op_id: opId() }, resolve, reject };
        if (phase === "live" && writeOp(entry)) {
          announce();
          return;
        }
        queued.push(entry);
        announce();
      });
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      clearGap();
      offFrame();
      offClose();
      release();
      rejectOutstanding(new DocOpError("disposed", "the document was closed"));
      for (const entry of queued.splice(0)) entry.reject(new DocOpError("disposed", "the document was closed"));
      messageListeners.clear();
      phaseListeners.clear();
    },
  };
}
