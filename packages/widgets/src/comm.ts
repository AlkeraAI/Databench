// A Jupyter comm whose wire is the frame's postMessage contract. Frontend
// messages leave as `comm.send` with a `msg_id` this frame chose; the parent
// relays the kernel's idle for that id back as `comm.status`, which is what
// releases a widget model's buffered changes.
import type { JSONObject, Post } from "./protocol";

type Handler = (msg: unknown) => void;

interface Callbacks {
  iopub?: { status?: (msg: unknown) => void };
}

export interface CommRouter {
  readonly post: Post;
  readonly viewOnly: boolean;
  /** Remembers which comm sent `msgId` so its idle reaches it. */
  track(msgId: string, comm: FrameComm): void;
}

let counter = 0;
export function newMsgId(): string {
  const random = Math.random().toString(36).slice(2, 10);
  return `frame-${random}-${++counter}`;
}

function toArrayBuffer(b: ArrayBuffer | ArrayBufferView): ArrayBuffer {
  if (b instanceof ArrayBuffer) return b;
  return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer;
}

export function toDataView(b: ArrayBuffer | ArrayBufferView): DataView {
  if (b instanceof ArrayBuffer) return new DataView(b);
  return new DataView(b.buffer, b.byteOffset, b.byteLength);
}

export class FrameComm {
  private onMsg: Handler | null = null;
  private onClose: Handler | null = null;
  private readonly callbacks = new Map<string, Callbacks>();

  /** `local`: a model the frontend created on its own (no kernel comm). It
   *  never sends: the parent would refuse an id the kernel did not open. */
  constructor(
    private readonly router: CommRouter,
    public readonly comm_id: string,
    public readonly target_name: string,
    private readonly local = false,
  ) {}

  open(): string {
    // Frontend-initiated comms are not part of the frame contract.
    return newMsgId();
  }

  send(data: unknown, callbacks?: Callbacks, _metadata?: JSONObject, buffers?: (ArrayBuffer | ArrayBufferView)[]): string {
    const msgId = newMsgId();
    if (this.local || this.router.viewOnly) {
      // Nothing leaves a read-only frame; release the model's throttle at once.
      queueMicrotask(() => callbacks?.iopub?.status?.(idle(msgId)));
      return msgId;
    }
    if (callbacks) this.callbacks.set(msgId, callbacks);
    this.router.track(msgId, this);
    this.router.post({
      type: "comm.send",
      comm_id: this.comm_id,
      msg_id: msgId,
      content: { comm_id: this.comm_id, data: data as JSONObject },
      buffers: (buffers ?? []).map(toArrayBuffer),
    });
    return msgId;
  }

  close(): string {
    // Closing is the kernel's to do; the view going away is not a close.
    return newMsgId();
  }

  on_msg(callback: Handler): void {
    this.onMsg = callback;
  }

  on_close(callback: Handler): void {
    this.onClose = callback;
  }

  /** A kernel `comm_msg`. `content` is the Jupyter comm content, `{comm_id, data}`. */
  deliver(content: JSONObject, buffers: (ArrayBuffer | ArrayBufferView)[], parentMsgId: string | null): void {
    const data = "data" in content ? content.data : content;
    this.onMsg?.({
      content: { comm_id: this.comm_id, data },
      buffers: buffers.map(toDataView),
      parent_header: parentMsgId ? { msg_id: parentMsgId } : {},
      metadata: {},
    });
  }

  /** The kernel finished handling the message this comm sent as `msgId`. */
  idle(msgId: string): void {
    const callbacks = this.callbacks.get(msgId);
    this.callbacks.delete(msgId);
    callbacks?.iopub?.status?.(idle(msgId));
  }

  closed(): void {
    this.onClose?.({ content: { comm_id: this.comm_id, data: {} } });
  }
}

function idle(msgId: string): unknown {
  return { content: { execution_state: "idle" }, parent_header: { msg_id: msgId }, header: { msg_type: "status" } };
}
