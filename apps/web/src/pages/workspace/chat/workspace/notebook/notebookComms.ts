// Widget comms for one notebook: the page's side of the frames' comm bridge.
//
// Where the engine's widget hub keeps the frames (`frame.message` events, each
// for one frame this tab attached), a frame is attached through the
// transport, which answers the id the server gave it and its replays; the
// frame is then routed its own traffic, and what it sends names that id. A
// reader may not attach (the route answers 403): the transport resolves
// `null` and the frame shows the widget read-only from the broadcast.
// The engine's comm events also arrive on the notebook channel as a
// broadcast, and this keeps each open
// model's latest state (merging `update` and `echo_update` the way the
// engine's widget hub does, so a frame mounted later is replayed the state it
// would have seen), fans every message out to the frames, and relays the
// kernel's `comm.idle` as the `comm.status` a widget waits on. A frame is
// handed only the models its view reaches: the closure of the displayed model
// over `IPY_MODEL_` references. What a person sends goes to the notebook's
// comm route.

import type { AttachedFrame, CommBridge, CommInbound, CommOpen, FrameAttach } from "@alkera/notebook-ui";

import { NotebookRequestError, deleteFrame, postFrame, type CommRequest, type FrameAttachRequest, type FrameAttached } from "@/api/notebooks";

const MODEL_REF = /^IPY_MODEL_(.+)$/;

const rec = (v: unknown): Record<string, unknown> => (typeof v === "object" && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {});

/** Standard base64 to bytes; anything else is dropped. */
export function bufferOf(value: unknown): ArrayBuffer | null {
  if (value instanceof ArrayBuffer) return value;
  if (typeof value !== "string") return null;
  try {
    const raw = atob(value);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
    return bytes.buffer;
  } catch {
    return null;
  }
}

export function base64Of(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let raw = "";
  for (let i = 0; i < bytes.length; i += 1) raw += String.fromCharCode(bytes[i]!);
  return btoa(raw);
}

/** Every model id a state names, at any depth. */
export function referencedModels(value: unknown, out: Set<string> = new Set()): Set<string> {
  if (typeof value === "string") {
    const hit = MODEL_REF.exec(value);
    if (hit) out.add(hit[1]!);
  } else if (Array.isArray(value)) {
    for (const item of value) referencedModels(item, out);
  } else if (typeof value === "object" && value !== null) {
    for (const item of Object.values(value)) referencedModels(item, out);
  }
  return out;
}

export type SendComm = (message: CommRequest) => void;

/** Attaching and detaching frames at the engine's widget hub. */
export interface FrameTransport {
  /** The frame's server id and comm-open replays; `null` when the person
   *  may not attach one. */
  attach(request: FrameAttachRequest): Promise<FrameAttached | null>;
  detach(frameId: string): void;
}

/** The notebook routes as a frame transport. `peerId` (this tab's socket)
 *  narrows a frame's events to the socket that shows it. A refusal to
 *  attach (403: a reader) is `null`, not an error. */
export function routeTransport(driveId: string, itemId: string, peerId: () => string | null, fetchImpl?: typeof fetch): FrameTransport {
  return {
    attach: async (request) => {
      const peer = peerId();
      try {
        return await postFrame(driveId, itemId, peer === null ? request : { ...request, peer_id: peer }, fetchImpl);
      } catch (error) {
        if (error instanceof NotebookRequestError && error.status === 403) return null;
        throw error;
      }
    },
    detach: (frameId) => void deleteFrame(driveId, itemId, frameId, fetchImpl).catch(() => undefined),
  };
}

/** How many messages for a frame not yet known are kept while an attach is
 *  in flight (the hub may route a frame traffic before its answer lands). */
const EARLY_PER_FRAME = 256;

/** A frame message in the frame contract's shape, as the page relays it. */
export function inboundOf(message: Record<string, unknown>, buffers: unknown[] = []): CommInbound | null {
  const decoded = buffers.map(bufferOf).filter((b): b is ArrayBuffer => b !== null);
  const commId = typeof message.comm_id === "string" ? message.comm_id : null;
  switch (message.type) {
    case "comm.open":
      return commId === null
        ? null
        : {
            type: "comm.open",
            comm_id: commId,
            target_name: typeof message.target_name === "string" ? message.target_name : "jupyter.widget",
            data: rec(message.data),
            ...(decoded.length > 0 ? { buffers: decoded } : {}),
            ...(message.metadata ? { metadata: rec(message.metadata) } : {}),
          };
    case "comm.msg":
      return commId === null
        ? null
        : {
            type: "comm.msg",
            comm_id: commId,
            content: rec(message.content),
            ...(decoded.length > 0 ? { buffers: decoded } : {}),
            parent_msg_id: typeof message.parent_msg_id === "string" ? message.parent_msg_id : null,
          };
    case "comm.close":
      return commId === null ? null : { type: "comm.close", comm_id: commId };
    case "comm.status":
      return typeof message.msg_id === "string" ? { type: "comm.status", msg_id: message.msg_id, execution_state: "idle" } : null;
    default:
      return null;
  }
}

/** One of an attach's comm-open replays: a frame message (`{message,
 *  buffers}`) or the open itself. */
export function openOf(reply: Record<string, unknown>): CommOpen | null {
  const message = "message" in reply ? rec(reply.message) : reply;
  const inbound = inboundOf({ type: "comm.open", ...message }, Array.isArray(reply.buffers) ? reply.buffers : []);
  if (inbound?.type !== "comm.open") return null;
  return {
    comm_id: inbound.comm_id,
    target_name: inbound.target_name,
    data: inbound.data,
    ...(inbound.buffers ? { buffers: inbound.buffers } : {}),
    ...(inbound.metadata ? { metadata: inbound.metadata } : {}),
  };
}

export class NotebookComms implements CommBridge {
  /** Open models in creation order. */
  private readonly models = new Map<string, CommOpen>();
  private readonly listeners = new Set<(message: CommInbound) => void>();
  /** Attached frames by the server's id. */
  private readonly frames = new Map<string, (message: CommInbound) => void>();
  /** The server's id of each attached frame, by the frame host's own. */
  private readonly serverIds = new Map<string, string>();
  /** Traffic for frames not yet known, kept while an attach is in flight. */
  private readonly early = new Map<string, CommInbound[]>();
  private attaching = 0;

  constructor(
    private readonly sendComm: SendComm,
    private readonly transport?: FrameTransport,
  ) {}

  /** The bridge the frames are given: one that attaches frames when the hub
   *  keeps them, one over the broadcast otherwise. */
  bridge(): CommBridge {
    const base: CommBridge = {
      opensFor: (modelId) => this.opensFor(modelId),
      subscribe: (listener) => this.subscribe(listener),
      send: (message) => this.send(message),
    };
    const transport = this.transport;
    if (!transport) return base;
    return {
      ...base,
      attach: async (frame: FrameAttach, listener: (message: CommInbound) => void): Promise<AttachedFrame | null> => {
        let answer: FrameAttached | null;
        this.attaching += 1;
        try {
          answer = await transport.attach({ output_id: frame.output_id, model_ids: [frame.model_id] });
        } finally {
          this.attaching -= 1;
        }
        const early = answer === null ? [] : (this.early.get(answer.frame_id) ?? []);
        if (answer !== null) this.early.delete(answer.frame_id);
        if (this.attaching === 0) this.early.clear();
        if (answer === null) return null;
        const serverId = answer.frame_id;
        this.frames.set(serverId, listener);
        this.serverIds.set(frame.frame_id, serverId);
        const opens: CommOpen[] = [];
        for (const reply of answer.opens) {
          const inbound = openOf(reply);
          if (inbound !== null) opens.push(inbound);
        }
        return {
          opens,
          detach: () => {
            if (this.serverIds.get(frame.frame_id) === serverId) this.serverIds.delete(frame.frame_id);
            if (this.frames.delete(serverId)) transport.detach(serverId);
          },
          ...(early.length > 0 ? { pending: early } : {}),
        };
      },
    };
  }

  /** Fold one engine comm event; false when it is not one. */
  handle(event: Record<string, unknown>): boolean {
    const type = event.type;
    if (type === "frame.message") {
      const frameId = typeof event.frame_id === "string" ? event.frame_id : null;
      const inbound = inboundOf(rec(event.message), Array.isArray(event.buffers) ? event.buffers : []);
      if (frameId === null || inbound === null) return true;
      const listener = this.frames.get(frameId);
      if (listener) {
        listener(inbound);
      } else if (this.attaching > 0) {
        const kept = this.early.get(frameId) ?? [];
        if (kept.length < EARLY_PER_FRAME) this.early.set(frameId, [...kept, inbound]);
      }
      return true;
    }
    const commId = typeof event.comm_id === "string" ? event.comm_id : null;
    const content = rec(event.content);
    const buffers = (Array.isArray(event.buffers) ? event.buffers : []).map(bufferOf).filter((b): b is ArrayBuffer => b !== null);
    const parent = typeof event.parent_msg_id === "string" ? event.parent_msg_id : null;
    switch (type) {
      case "comm.open": {
        if (commId === null) return true;
        const open: CommOpen = {
          comm_id: commId,
          target_name: typeof content.target_name === "string" ? content.target_name : "jupyter.widget",
          data: rec(content.data),
          ...(buffers.length > 0 ? { buffers } : {}),
          ...(content.metadata ? { metadata: rec(content.metadata) } : {}),
        };
        this.models.set(commId, open);
        this.emit({ type: "comm.open", ...open });
        return true;
      }
      case "comm.msg": {
        if (commId === null) return true;
        const data = rec(content.data);
        const model = this.models.get(commId);
        if (model && (data.method === "update" || data.method === "echo_update")) {
          const state = { ...rec(model.data.state), ...rec(data.state) };
          this.models.set(commId, { ...model, data: { ...model.data, state } });
        }
        this.emit({ type: "comm.msg", comm_id: commId, content, ...(buffers.length > 0 ? { buffers } : {}), parent_msg_id: parent });
        return true;
      }
      case "comm.close":
        if (commId !== null) {
          this.models.delete(commId);
          this.emit({ type: "comm.close", comm_id: commId });
        }
        return true;
      case "comm.idle":
        if (typeof event.msg_id === "string") this.emit({ type: "comm.status", msg_id: event.msg_id, execution_state: "idle" });
        return true;
      default:
        return false;
    }
  }

  /** Comm-open replays for a displayed model and every model it reaches. */
  opensFor(modelId: string): CommOpen[] {
    const reach = new Set<string>([modelId]);
    const queue = [modelId];
    while (queue.length > 0) {
      const id = queue.shift()!;
      const model = this.models.get(id);
      if (!model) continue;
      for (const ref of referencedModels(model.data.state)) {
        if (!reach.has(ref)) {
          reach.add(ref);
          queue.push(ref);
        }
      }
    }
    return [...this.models.values()].filter((m) => reach.has(m.comm_id));
  }

  subscribe(listener: (message: CommInbound) => void): () => void {
    this.listeners.add(listener);
    return () => void this.listeners.delete(listener);
  }

  /** A frame's message to the kernel, named by the frame's server id. A frame
   *  that is not attached sends nothing: the comm route takes only attached
   *  frames. */
  send(message: { comm_id: string; msg_id: string; content: Record<string, unknown>; buffers: ArrayBuffer[]; frame_id?: string }): void {
    const frameId = message.frame_id === undefined ? undefined : this.serverIds.get(message.frame_id);
    if (frameId === undefined) return;
    this.sendComm({
      frame_id: frameId,
      comm_id: message.comm_id,
      msg_id: message.msg_id,
      content: message.content,
      buffers: message.buffers.map(base64Of),
    });
  }

  private emit(message: CommInbound): void {
    this.listeners.forEach((l) => l(message));
  }

  reset(): void {
    this.models.clear();
  }
}
