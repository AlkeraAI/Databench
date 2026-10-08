// The notebook tab's side of one framed output.
//
// A framed output runs code the notebook produced (an HTML report's scripts, a
// widget library, a chart runtime) in a sandboxed frame on the content origin,
// where it is an opaque origin with no network. The frame can still talk to
// this page by `postMessage`, so everything it says is treated as untrusted:
// the host checks who sent it (this frame's window, an opaque origin, this
// frame's nonce), what it is (a known type with well-typed fields), and
// whether the frame may say it (only widget frames that may run send comm
// messages, only to the comms this frame was given, within the size and rate
// caps). A refused message is counted with its reason and otherwise ignored;
// nothing here throws on what a frame sends.

import type { AttachedFrame, CommBridge, CommInbound, CommOpen, FrameServices, OutputTheme } from "../outputs/types";
import {
  COMM_SENDS_PER_SECOND,
  FRAME_MODULE_BY_MIME,
  FRAME_PROTOCOL,
  MAX_COMM_SEND_BYTES,
  MAX_FRAME_HEIGHT,
  WIDGET_VIEW_MIME,
  type ParentMessage,
} from "./protocol";
import { WIDGETS_MODULE, withWidgetsAdapter } from "./widgetsAdapter";

/** Why a frame's message was refused. */
export type DropReason =
  | "wrong_source"
  | "wrong_origin"
  | "bad_envelope"
  | "wrong_nonce"
  | "detached"
  | "unknown_type"
  | "bad_fields"
  | "not_widget"
  | "readonly"
  | "unknown_comm"
  | "too_large"
  | "rate_limited"
  | "bad_link"
  | "no_link_handler"
  | "too_many_modules"
  | "too_many_errors";

/** What the host tells the component around it. */
export interface FrameHostEvents {
  /** The frame's bootstrap answered. */
  onReady?(): void;
  /** The height the frame asked for, already clamped. */
  onSize?(height: number): void;
  /** An error the reader should see. */
  onError?(message: string): void;
  /** The frame's document was replaced by something else; the host stopped. */
  onNavigated?(): void;
  /** The frame was recycled: render a fresh frame at the new `src`. */
  onRecycle?(nonce: string, src: string): void;
}

export interface FrameHostOptions {
  services: FrameServices;
  outputId: string;
  mime: string;
  data: unknown;
  theme: OutputTheme;
  readonly: boolean;
  events?: FrameHostEvents;
  /** Milliseconds, for the send rate. Defaults to `performance.now`. */
  now?: () => number;
  /** Fills a buffer with random bytes, for the nonce. Defaults to `crypto`. */
  randomBytes?: (buffer: Uint8Array) => Uint8Array;
}

/** The parts of a `MessageEvent` the host reads. */
export interface InboundMessage {
  source: unknown;
  origin: string;
  data: unknown;
}

/** The part of the frame's window the host writes to. */
export interface FrameWindow {
  postMessage(message: unknown, targetOrigin: string, transfer?: Transferable[]): void;
}

const MODEL_REF = "IPY_MODEL_";
const MAX_MODULE_REQUESTS = 64;
const MAX_ERRORS = 50;
const MAX_PENDING_SENDS = 1024;
const MAX_ERROR_CHARS = 500;
const MAX_ID_CHARS = 256;
const MAX_HREF_CHARS = 4096;
const LINK_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isId(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= MAX_ID_CHARS;
}

function isArrayBuffer(value: unknown): value is ArrayBuffer {
  return Object.prototype.toString.call(value) === "[object ArrayBuffer]";
}

/** A frame's buffers as `ArrayBuffer`s, or null when one is anything else. */
function toBuffers(value: unknown): ArrayBuffer[] | null {
  if (value === undefined) return [];
  if (!Array.isArray(value)) return null;
  const out: ArrayBuffer[] = [];
  for (const item of value) {
    if (isArrayBuffer(item)) {
      out.push(item);
    } else if (ArrayBuffer.isView(item)) {
      const copy = new Uint8Array(item.byteLength);
      copy.set(new Uint8Array(item.buffer, item.byteOffset, item.byteLength));
      out.push(copy.buffer);
    } else {
      return null;
    }
  }
  return out;
}

/** UTF-8 size of the content as JSON plus every buffer, or null when the
 *  content cannot be serialized (a cycle, a BigInt). */
function sendSize(content: Record<string, unknown>, buffers: ArrayBuffer[]): number | null {
  let json: string;
  try {
    json = JSON.stringify(content);
  } catch {
    return null;
  }
  return new TextEncoder().encode(json).byteLength + buffers.reduce((n, b) => n + b.byteLength, 0);
}

/** Every model id an `IPY_MODEL_` reference names inside a message. */
export function modelReferences(value: unknown): string[] {
  const found = new Set<string>();
  let budget = 10_000;
  const walk = (node: unknown, depth: number): void => {
    if (budget-- <= 0 || depth > 32) return;
    if (typeof node === "string") {
      if (node.startsWith(MODEL_REF) && node.length > MODEL_REF.length) found.add(node.slice(MODEL_REF.length));
    } else if (Array.isArray(node)) {
      for (const item of node) walk(item, depth + 1);
    } else if (isRecord(node)) {
      for (const item of Object.values(node)) walk(item, depth + 1);
    }
  };
  walk(value, 0);
  return [...found];
}

/** A frame id for the engine's hub: random, never the nonce (which the
 *  frame knows and so must not double as a name the page acts on). */
function randomFrameId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(12));
  return `f${[...bytes].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}

function defaultRandomBytes(buffer: Uint8Array): Uint8Array {
  return crypto.getRandomValues(buffer);
}

export class FrameHost {
  /** Refused messages by reason, for tests and diagnostics. */
  readonly drops: Record<DropReason, number> = {
    wrong_source: 0,
    wrong_origin: 0,
    bad_envelope: 0,
    wrong_nonce: 0,
    detached: 0,
    unknown_type: 0,
    bad_fields: 0,
    not_widget: 0,
    readonly: 0,
    unknown_comm: 0,
    too_large: 0,
    rate_limited: 0,
    bad_link: 0,
    no_link_handler: 0,
    too_many_modules: 0,
    too_many_errors: 0,
  };

  private readonly services: FrameServices;
  private readonly outputId: string;
  private readonly mime: string;
  private readonly data: unknown;
  private readonly readonly: boolean;
  private readonly events: FrameHostEvents;
  private readonly now: () => number;
  private readonly randomBytes: (buffer: Uint8Array) => Uint8Array;
  private readonly isWidget: boolean;

  private theme: OutputTheme;
  private currentNonce: string;
  private getWindow: () => FrameWindow | null | undefined = () => null;
  private generation = 0;
  private started = false;
  private loads = 0;
  private navigated = false;
  private disposed = false;
  private errors = 0;
  private allowed = new Set<string>();
  private pending = new Set<string>();
  private sendTimes: number[] = [];
  private requested = new Set<string>();
  private unsubscribe: (() => void) | null = null;
  /** This frame's id at the engine's hub; a recycled frame attaches anew. */
  private frameId = randomFrameId();
  /** The hub refused to attach this frame: it shows the widget read-only. */
  private refused = false;
  /** Whether the hub routes this frame its traffic (else the broadcast does). */
  private hubbed = false;

  constructor(options: FrameHostOptions) {
    this.services = options.services;
    this.outputId = options.outputId;
    this.mime = options.mime;
    this.data = options.data;
    this.theme = options.theme;
    this.events = options.events ?? {};
    this.now = options.now ?? (() => performance.now());
    this.randomBytes = options.randomBytes ?? defaultRandomBytes;
    this.isWidget = options.mime === WIDGET_VIEW_MIME;
    // A widget with no comm bridge has no kernel to talk to: it renders, but
    // as a reader's would.
    this.readonly = options.readonly || (this.isWidget && !this.services.comms);
    this.currentNonce = this.mintNonce();
  }

  get nonce(): string {
    return this.currentNonce;
  }

  /** The frame's address: the bootstrap page with this frame's nonce in the
   *  fragment, which the browser never sends to a server. */
  get src(): string {
    const base = this.services.bootstrapUrl.split("#", 1)[0];
    return `${base}#n=${this.currentNonce}`;
  }

  /** Whether the frame may send comm messages at all. */
  get canSend(): boolean {
    return this.isWidget && !this.readOnly;
  }

  /** Read-only by the options, or because the hub refused this frame. */
  private get readOnly(): boolean {
    return this.readonly || this.refused;
  }

  /** Points the host at the frame's window (read on every use, so a remounted
   *  frame is picked up). */
  attach(getWindow: () => FrameWindow | null | undefined): void {
    this.getWindow = getWindow;
  }

  /** The frame finished loading a document. The first load is the bootstrap;
   *  a second one without a recycle means the output navigated its own frame
   *  somewhere else, which nothing here will talk to. */
  handleLoad(): void {
    if (this.disposed || this.navigated) return;
    this.loads += 1;
    if (this.loads === 1) {
      // Browsers that cannot name the parent to the frame wait for our first
      // message to learn it, so the load is a start signal as good as `ready`.
      void this.start();
      return;
    }
    // The document now in the frame is not the one the nonce was given to:
    // the nonce is discarded and nothing is sent until a recycle loads the
    // bootstrap again and a fresh `ready` arrives under a new one.
    this.navigated = true;
    this.currentNonce = this.mintNonce();
    this.stopComms();
    this.events.onNavigated?.();
  }

  /** Reloads the output in a fresh frame with a new nonce. Anything the old
   *  frame still sends carries the old nonce and is refused. */
  recycle(): void {
    this.frameId = randomFrameId();
    this.refused = false;
    this.hubbed = false;
    if (this.disposed) return;
    this.generation += 1;
    this.stopComms();
    this.started = false;
    this.loads = 0;
    this.navigated = false;
    this.errors = 0;
    this.allowed = new Set();
    this.pending = new Set();
    this.sendTimes = [];
    this.requested = new Set();
    this.currentNonce = this.mintNonce();
    this.events.onRecycle?.(this.currentNonce, this.src);
  }

  setTheme(theme: OutputTheme): void {
    if (theme === this.theme) return;
    this.theme = theme;
    if (this.started) this.post({ type: "theme", theme });
  }

  dispose(): void {
    this.disposed = true;
    this.generation += 1;
    this.stopComms();
  }

  /** Validates and acts on one message from the page's `message` event.
   *  Returns whether it was accepted. */
  handleMessage(event: InboundMessage): boolean {
    const frameWindow = this.getWindow();
    if (!frameWindow || event.source !== frameWindow) return this.drop("wrong_source");
    if (event.origin !== "null") return this.drop("wrong_origin");
    const message = event.data;
    if (!isRecord(message) || message.alk !== FRAME_PROTOCOL || typeof message.frame !== "string" || typeof message.type !== "string") {
      return this.drop("bad_envelope");
    }
    if (message.frame !== this.currentNonce) return this.drop("wrong_nonce");
    if (this.disposed || this.navigated) return this.drop("detached");
    switch (message.type) {
      case "ready":
        this.events.onReady?.();
        void this.start();
        return true;
      case "size":
        return this.onSize(message);
      case "comm.send":
        return this.onCommSend(message);
      case "need_module":
        return this.onNeedModule(message);
      case "link":
        return this.onLink(message);
      case "error":
        return this.onFrameError(message);
      default:
        return this.drop("unknown_type");
    }
  }

  private drop(reason: DropReason): false {
    this.drops[reason] += 1;
    return false;
  }

  private mintNonce(): string {
    const bytes = this.randomBytes(new Uint8Array(16));
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  private live(generation: number): boolean {
    return generation === this.generation && !this.disposed && !this.navigated;
  }

  /** Writes to the frame. The target origin is `*` because the frame's origin
   *  is opaque and cannot be named; what keeps this safe is that the window is
   *  the one this host created, and that after a navigation (a second load)
   *  nothing is posted at all. */
  private post(message: ParentMessage): void {
    if (this.disposed || this.navigated) return;
    const frameWindow = this.getWindow();
    if (!frameWindow) return;
    frameWindow.postMessage({ ...message, alk: FRAME_PROTOCOL, frame: this.currentNonce }, "*");
  }

  private fail(message: string): void {
    this.events.onError?.(message);
  }

  private async start(): Promise<void> {
    if (this.started || this.disposed || this.navigated) return;
    this.started = true;
    const generation = this.generation;
    const ref = FRAME_MODULE_BY_MIME[this.mime];
    if (!ref) {
      this.fail(`Outputs of type ${this.mime} can't be shown here.`);
      return;
    }
    let code: string;
    try {
      code = await this.services.loadModule(ref.name, ref.version);
    } catch {
      if (this.live(generation)) this.fail("The renderer for this output couldn't be loaded.");
      return;
    }
    if (!this.live(generation)) return;
    this.requested.add(`${ref.name}@${ref.version}`);
    // The widget manager is a plain bundle; the adapter registers it.
    const shipped = ref.name === WIDGETS_MODULE ? withWidgetsAdapter(code) : code;
    this.post({ type: "module", name: ref.name, version: ref.version, code: shipped });
    let opens: CommOpen[];
    const comms = this.services.comms;
    const modelId = isRecord(this.data) && isId(this.data.model_id) ? this.data.model_id : null;
    let attached: AttachedFrame | null = null;
    let pending: CommInbound[] = [];
    if (this.isWidget && comms?.attach && modelId !== null && !this.readonly) {
      // The engine's hub keeps this frame: it replays the models the frame
      // shows and routes it only their traffic.
      try {
        attached = await comms.attach({ frame_id: this.frameId, output_id: this.outputId, model_id: modelId }, (inbound) =>
          this.onFrameMessage(generation, inbound),
        );
      } catch {
        if (this.live(generation)) this.fail("The widget could not reach the kernel.");
        return;
      }
      if (!this.live(generation)) {
        attached?.detach();
        return;
      }
      // Refused (a reader): shown read-only from the broadcast instead.
      if (attached === null) this.refused = true;
    }
    if (attached !== null) {
      const frame = attached;
      this.hubbed = true;
      this.unsubscribe = () => frame.detach();
      opens = frame.opens;
      pending = frame.pending ?? [];
    } else {
      opens = this.initialOpens();
    }
    for (const open of opens) this.allowed.add(open.comm_id);
    this.post({
      type: "init",
      theme: this.theme,
      output_id: this.outputId,
      mime: this.mime,
      data: this.data,
      opens,
      readonly: this.readOnly,
    });
    // Subscribed in the same turn as the replay was read, so no kernel message
    // can fall between the snapshot and the stream.
    for (const inbound of pending) this.onFrameMessage(generation, inbound);
    if (this.isWidget && comms && !this.hubbed) {
      this.unsubscribe = comms.subscribe((inbound) => this.onKernel(comms, generation, inbound));
    }
  }

  private initialOpens(): CommOpen[] {
    const comms = this.services.comms;
    if (!this.isWidget || !comms || !isRecord(this.data) || !isId(this.data.model_id)) return [];
    return comms.opensFor(this.data.model_id);
  }

  private stopComms(): void {
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  /** Kernel-to-frontend traffic: only what concerns the models this frame was
   *  given reaches it. */
  private onKernel(comms: CommBridge, generation: number, inbound: CommInbound): void {
    if (!this.live(generation)) return;
    switch (inbound.type) {
      case "comm.open":
        // A new model reaches a frame only once something it shows refers to
        // it; the replay then comes from the bridge's cache.
        if (this.allowed.has(inbound.comm_id)) this.post(inbound);
        return;
      case "comm.msg":
        if (!this.allowed.has(inbound.comm_id)) return;
        this.extendClosure(comms, inbound.content);
        this.post({
          type: "comm.msg",
          comm_id: inbound.comm_id,
          content: inbound.content,
          buffers: inbound.buffers ?? [],
          parent_msg_id: inbound.parent_msg_id ?? null,
        });
        return;
      case "comm.close":
        if (!this.allowed.delete(inbound.comm_id)) return;
        this.post({ type: "comm.close", comm_id: inbound.comm_id });
        return;
      case "comm.status":
        if (!this.pending.delete(inbound.msg_id)) return;
        this.post({ type: "comm.status", msg_id: inbound.msg_id, execution_state: "idle" });
        return;
    }
  }

  /** Traffic the engine routed to this frame: already only the models it
   *  shows, so it is passed on as it is, keeping track of what it was given. */
  private onFrameMessage(generation: number, inbound: CommInbound): void {
    if (!this.live(generation)) return;
    switch (inbound.type) {
      case "comm.open":
        this.allowed.add(inbound.comm_id);
        this.post(inbound);
        return;
      case "comm.msg":
        this.post({
          type: "comm.msg",
          comm_id: inbound.comm_id,
          content: inbound.content,
          buffers: inbound.buffers ?? [],
          parent_msg_id: inbound.parent_msg_id ?? null,
        });
        return;
      case "comm.close":
        this.allowed.delete(inbound.comm_id);
        this.post({ type: "comm.close", comm_id: inbound.comm_id });
        return;
      case "comm.status":
        if (!this.pending.delete(inbound.msg_id)) return;
        this.post({ type: "comm.status", msg_id: inbound.msg_id, execution_state: "idle" });
        return;
    }
  }

  /** An update that refers to a model this frame does not have yet gives it
   *  that model's closure, opened before the update that needs it. */
  private extendClosure(comms: CommBridge, content: unknown): void {
    for (const modelId of modelReferences(content)) {
      if (this.allowed.has(modelId)) continue;
      for (const open of comms.opensFor(modelId)) {
        if (this.allowed.has(open.comm_id)) continue;
        this.allowed.add(open.comm_id);
        this.post({ type: "comm.open", ...open });
      }
    }
  }

  private onSize(message: Record<string, unknown>): boolean {
    const height = message.height;
    if (typeof height !== "number" || !Number.isFinite(height) || height < 0) return this.drop("bad_fields");
    this.events.onSize?.(Math.min(MAX_FRAME_HEIGHT, Math.ceil(height)));
    return true;
  }

  private onCommSend(message: Record<string, unknown>): boolean {
    if (!this.isWidget) return this.drop("not_widget");
    const comms = this.services.comms;
    if (this.readOnly || !comms) return this.drop("readonly");
    const { comm_id: commId, msg_id: msgId, content } = message;
    const buffers = toBuffers(message.buffers);
    if (!isId(commId) || !isId(msgId) || !isRecord(content) || buffers === null) return this.drop("bad_fields");
    if (!this.allowed.has(commId)) return this.drop("unknown_comm");
    const size = sendSize(content, buffers);
    if (size === null) return this.drop("bad_fields");
    if (size > MAX_COMM_SEND_BYTES) return this.drop("too_large");
    const now = this.now();
    this.sendTimes = this.sendTimes.filter((t) => now - t < 1000);
    if (this.sendTimes.length >= COMM_SENDS_PER_SECOND) return this.drop("rate_limited");
    this.sendTimes.push(now);
    this.pending.add(msgId);
    if (this.pending.size > MAX_PENDING_SENDS) {
      const oldest = this.pending.values().next().value;
      if (oldest !== undefined) this.pending.delete(oldest);
    }
    comms.send({ comm_id: commId, msg_id: msgId, content, buffers, ...(this.hubbed ? { frame_id: this.frameId } : {}) });
    return true;
  }

  private onNeedModule(message: Record<string, unknown>): boolean {
    if (!this.isWidget) return this.drop("not_widget");
    const { name, version } = message;
    if (!isId(name) || typeof version !== "string" || version.length > MAX_ID_CHARS) return this.drop("bad_fields");
    const key = `${name}@${version}`;
    if (this.requested.has(key)) return true;
    if (this.requested.size >= MAX_MODULE_REQUESTS) return this.drop("too_many_modules");
    this.requested.add(key);
    const generation = this.generation;
    this.services.loadModule(name, version).then(
      (code) => {
        if (this.live(generation)) this.post({ type: "module", name, version, code });
      },
      () => {
        if (this.live(generation)) this.fail(`The module ${name} couldn't be loaded.`);
      },
    );
    return true;
  }

  private onLink(message: Record<string, unknown>): boolean {
    const href = message.href;
    if (typeof href !== "string" || href.length > MAX_HREF_CHARS) return this.drop("bad_fields");
    let url: URL;
    try {
      url = new URL(href);
    } catch {
      return this.drop("bad_link");
    }
    if (!LINK_PROTOCOLS.has(url.protocol)) return this.drop("bad_link");
    if (!this.services.confirmLink) return this.drop("no_link_handler");
    // Never opened from here: the page asks the reader first.
    this.services.confirmLink(url.href);
    return true;
  }

  private onFrameError(message: Record<string, unknown>): boolean {
    const text = message.message;
    if (typeof text !== "string") return this.drop("bad_fields");
    if (this.errors >= MAX_ERRORS) return this.drop("too_many_errors");
    this.errors += 1;
    this.fail(text.slice(0, MAX_ERROR_CHARS));
    return true;
  }
}
