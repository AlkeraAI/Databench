import { describe, expect, it } from "vitest";

import type { CommBridge, CommInbound, CommOpen, FrameServices } from "../outputs/types";
import { FrameHost, modelReferences, type DropReason, type FrameHostEvents } from "./host";
import { COMM_SENDS_PER_SECOND, MAX_COMM_SEND_BYTES, MAX_FRAME_HEIGHT, WIDGET_VIEW_MIME } from "./protocol";

/** A frame's window that records what the host posts to it. */
class RecordingWindow {
  readonly posted: Array<{ message: Record<string, unknown>; target: string }> = [];
  postMessage(message: unknown, target: string): void {
    this.posted.push({ message: message as Record<string, unknown>, target });
  }
  types(): unknown[] {
    return this.posted.map((p) => p.message.type);
  }
  last(type: string): Record<string, unknown> | undefined {
    return [...this.posted].reverse().find((p) => p.message.type === type)?.message;
  }
}

/** A comm bridge over an in-memory widget hub. */
class FakeBridge implements CommBridge {
  readonly sent: Array<{ comm_id: string; msg_id: string; content: Record<string, unknown>; buffers: ArrayBuffer[] }> = [];
  private listeners: Array<(m: CommInbound) => void> = [];
  constructor(private readonly closures: Record<string, CommOpen[]>) {}
  opensFor(modelId: string): CommOpen[] {
    return this.closures[modelId] ?? [];
  }
  subscribe(listener: (m: CommInbound) => void): () => void {
    this.listeners.push(listener);
    return () => {
      this.listeners = this.listeners.filter((l) => l !== listener);
    };
  }
  send(message: { comm_id: string; msg_id: string; content: Record<string, unknown>; buffers: ArrayBuffer[] }): void {
    this.sent.push(message);
  }
  emit(message: CommInbound): void {
    for (const listener of this.listeners) listener(message);
  }
  get subscribers(): number {
    return this.listeners.length;
  }
}

const open = (comm_id: string, state: Record<string, unknown> = {}): CommOpen => ({
  comm_id,
  target_name: "jupyter.widget",
  data: { state },
});

interface Rig {
  host: FrameHost;
  frame: RecordingWindow;
  bridge: FakeBridge;
  links: string[];
  errors: string[];
  sizes: number[];
  clock: { t: number };
  loads: string[];
  /** Sends a message as the frame would, with the right envelope unless overridden. */
  say(data: Record<string, unknown>, overrides?: { source?: unknown; origin?: string; frame?: string }): boolean;
}

const SLIDER = open("slider", { value: 1, layout: "IPY_MODEL_layout" });
const LAYOUT = open("layout", { width: "auto" });

function rig(options: {
  mime?: string;
  data?: unknown;
  readonly?: boolean;
  comms?: boolean;
  confirmLink?: boolean;
  closures?: Record<string, CommOpen[]>;
  failModules?: boolean;
  refuse?: string[];
  events?: FrameHostEvents;
} = {}): Rig {
  const frame = new RecordingWindow();
  const bridge = new FakeBridge(options.closures ?? { slider: [LAYOUT, SLIDER] });
  const links: string[] = [];
  const errors: string[] = [];
  const sizes: number[] = [];
  const loads: string[] = [];
  const clock = { t: 0 };
  let seed = 0;
  const services: FrameServices = {
    bootstrapUrl: "https://content.example/c/nb-output/abcdef12",
    loadModule: async (name, version) => {
      loads.push(`${name}@${version}`);
      if (options.failModules || options.refuse?.includes(name)) throw new Error("not vouched for");
      return `/* ${name} */`;
    },
    ...(options.comms === false ? {} : { comms: bridge }),
    ...(options.confirmLink === false ? {} : { confirmLink: (href: string) => links.push(href) }),
  };
  const events: FrameHostEvents = {
    ...options.events,
    onError: (m) => errors.push(m),
    onSize: (h) => sizes.push(h),
  };
  const host = new FrameHost({
    services,
    outputId: "out-1",
    mime: options.mime ?? WIDGET_VIEW_MIME,
    data: options.data ?? { model_id: "slider", version_major: 2 },
    theme: "light",
    readonly: options.readonly ?? false,
    events,
    now: () => clock.t,
    randomBytes: (buffer) => buffer.fill((seed += 1)),
  });
  host.attach(() => frame);
  const say: Rig["say"] = (data, overrides = {}) =>
    host.handleMessage({
      source: "source" in overrides ? overrides.source : frame,
      origin: overrides.origin ?? "null",
      data: { alk: 1, frame: overrides.frame ?? host.nonce, ...data },
    });
  return { host, frame, bridge, links, errors, sizes, clock, loads, say };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

async function started(options: Parameters<typeof rig>[0] = {}): Promise<Rig> {
  const r = rig(options);
  r.say({ type: "ready" });
  await flush();
  return r;
}

function dropped(host: FrameHost): Partial<Record<DropReason, number>> {
  return Object.fromEntries(Object.entries(host.drops).filter(([, n]) => n > 0));
}

const send = (comm_id = "slider", extra: Record<string, unknown> = {}) => ({
  type: "comm.send",
  comm_id,
  msg_id: `m-${Math.random()}`,
  content: { method: "update", state: { value: 2 } },
  buffers: [],
  ...extra,
});

describe("FrameHost envelope checks", () => {
  it("refuses a message from any window but its frame", () => {
    const r = rig();
    expect(r.say({ type: "size", height: 10 }, { source: {} })).toBe(false);
    expect(r.say({ type: "size", height: 10 }, { source: null })).toBe(false);
    expect(dropped(r.host)).toEqual({ wrong_source: 2 });
    expect(r.sizes).toEqual([]);
  });

  it("refuses a message from a real origin, even the content origin", () => {
    const r = rig();
    expect(r.say({ type: "size", height: 10 }, { origin: "https://content.example" })).toBe(false);
    expect(r.say({ type: "size", height: 10 }, { origin: "" })).toBe(false);
    expect(dropped(r.host)).toEqual({ wrong_origin: 2 });
  });

  it("refuses another frame's nonce", () => {
    const r = rig();
    expect(r.say({ type: "size", height: 10 }, { frame: "someone-else" })).toBe(false);
    expect(dropped(r.host)).toEqual({ wrong_nonce: 1 });
    expect(r.sizes).toEqual([]);
  });

  it.each([
    ["not an object", "hello"],
    ["no alk stamp", { frame: "x", type: "ready" }],
    ["a future protocol", { alk: 2, type: "ready" }],
    ["no type", { alk: 1 }],
  ])("refuses an envelope with %s", (_label, data) => {
    const r = rig();
    const accepted = r.host.handleMessage({
      source: r.frame,
      origin: "null",
      data: typeof data === "object" && data !== null && "alk" in data ? { frame: r.host.nonce, ...data } : data,
    });
    expect(accepted).toBe(false);
    expect(dropped(r.host)).toEqual({ bad_envelope: 1 });
  });

  it("refuses a type the contract does not have", () => {
    const r = rig();
    expect(r.say({ type: "eval", code: "1" })).toBe(false);
    expect(dropped(r.host)).toEqual({ unknown_type: 1 });
  });

  it("mints a fresh random nonce and keeps it out of the server's view", () => {
    const r = rig();
    expect(r.host.nonce).toMatch(/^[0-9a-f]{32}$/);
    expect(r.host.src).toBe(`https://content.example/c/nb-output/abcdef12#n=${r.host.nonce}`);
  });
});

describe("FrameHost start", () => {
  it("answers ready with the renderer module, then init with the closure of the displayed model", async () => {
    const r = await started();
    expect(r.loads).toEqual(["@alkera/widgets@1"]);
    expect(r.frame.types()).toEqual(["module", "init"]);
    expect(r.frame.posted.every((p) => p.target === "*" && p.message.alk === 1 && p.message.frame === r.host.nonce)).toBe(true);
    const init = r.frame.last("init");
    expect(init).toMatchObject({ output_id: "out-1", mime: WIDGET_VIEW_MIME, theme: "light", readonly: false });
    expect(init?.opens).toEqual([LAYOUT, SLIDER]);
  });

  it("starts on the frame's load when the frame cannot announce itself, and only once", async () => {
    const r = rig();
    r.host.handleLoad();
    r.say({ type: "ready" });
    await flush();
    expect(r.frame.types()).toEqual(["module", "init"]);
  });

  it.each([
    ["text/html", "nb-html@1"],
    ["image/svg+xml", "nb-svg@1"],
    ["application/vnd.vegalite.v5+json", "nb-vega@1"],
    ["application/vnd.vegalite.v6+json", "nb-vega@1"],
    ["application/vnd.plotly.v1+json", "nb-plotly@1"],
  ])("sends %s outputs the %s module and no comm replays", async (mime, module) => {
    const r = await started({ mime, data: "<b>hi</b>" });
    expect(r.loads).toEqual([module]);
    expect(r.frame.last("init")?.opens).toEqual([]);
    expect(r.bridge.subscribers).toBe(0);
  });

  it("surfaces a renderer that cannot be loaded instead of posting nothing silently", async () => {
    const r = await started({ failModules: true });
    expect(r.frame.posted).toEqual([]);
    expect(r.errors).toEqual(["The renderer for this output couldn't be loaded."]);
  });

  it("renders a widget read-only when there is no comm bridge", async () => {
    const r = await started({ comms: false });
    expect(r.frame.last("init")).toMatchObject({ readonly: true, opens: [] });
    expect(r.host.canSend).toBe(false);
  });

  it("follows theme changes once started", async () => {
    const r = rig();
    r.host.setTheme("dark");
    expect(r.frame.posted).toEqual([]);
    r.say({ type: "ready" });
    await flush();
    expect(r.frame.last("init")?.theme).toBe("dark");
    r.host.setTheme("light");
    expect(r.frame.last("theme")).toMatchObject({ theme: "light" });
  });
});

describe("FrameHost size", () => {
  it.each([
    [120.2, 121],
    [MAX_FRAME_HEIGHT + 5_000, MAX_FRAME_HEIGHT],
    [0, 0],
  ])("reports %d as %d", (height, expected) => {
    const r = rig();
    expect(r.say({ type: "size", height })).toBe(true);
    expect(r.sizes).toEqual([expected]);
  });

  it.each([[-1], [Number.NaN], [Number.POSITIVE_INFINITY], ["100"]])("refuses height %s", (height) => {
    const r = rig();
    expect(r.say({ type: "size", height })).toBe(false);
    expect(dropped(r.host)).toEqual({ bad_fields: 1 });
  });
});

describe("FrameHost comm.send", () => {
  it("relays an allowed send to the bridge with the frame's msg_id, and relays its idle back", async () => {
    const r = await started();
    const message = send("slider", { msg_id: "m-1", buffers: [new Uint8Array([1, 2, 3])] });
    expect(r.say(message)).toBe(true);
    expect(r.bridge.sent).toHaveLength(1);
    expect(r.bridge.sent[0]).toMatchObject({ comm_id: "slider", msg_id: "m-1", content: message.content });
    expect(new Uint8Array(r.bridge.sent[0].buffers[0])).toEqual(new Uint8Array([1, 2, 3]));

    r.bridge.emit({ type: "comm.status", msg_id: "m-1", execution_state: "idle" });
    expect(r.frame.last("comm.status")).toMatchObject({ msg_id: "m-1", execution_state: "idle" });
  });

  it("relays only the idle replies for its own sends, once", async () => {
    const r = await started();
    r.bridge.emit({ type: "comm.status", msg_id: "someone-elses", execution_state: "idle" });
    r.say(send("slider", { msg_id: "m-2" }));
    r.bridge.emit({ type: "comm.status", msg_id: "m-2", execution_state: "idle" });
    r.bridge.emit({ type: "comm.status", msg_id: "m-2", execution_state: "idle" });
    expect(r.frame.posted.filter((p) => p.message.type === "comm.status")).toHaveLength(1);
  });

  it("refuses a comm id this frame was never given", async () => {
    const r = await started();
    expect(r.say(send("another-widget"))).toBe(false);
    expect(dropped(r.host)).toEqual({ unknown_comm: 1 });
    expect(r.bridge.sent).toEqual([]);
  });

  it("refuses every send from a read-only frame", async () => {
    const r = await started({ readonly: true });
    expect(r.frame.last("init")?.readonly).toBe(true);
    expect(r.say(send("slider"))).toBe(false);
    expect(dropped(r.host)).toEqual({ readonly: 1 });
    expect(r.bridge.sent).toEqual([]);
  });

  it.each([
    ["text/html"],
    ["image/svg+xml"],
    ["application/vnd.vegalite.v5+json"],
    ["application/vnd.plotly.v1+json"],
  ])("refuses sends from a %s output frame, whatever comm id it names", async (mime) => {
    const r = await started({ mime, data: "x" });
    expect(r.say(send("slider"))).toBe(false);
    expect(dropped(r.host)).toEqual({ not_widget: 1 });
    expect(r.bridge.sent).toEqual([]);
  });

  it("refuses a send over 1 MiB and accepts one at the cap", async () => {
    const r = await started();
    const envelope = JSON.stringify({ method: "custom" }).length;
    const exact = new Uint8Array(MAX_COMM_SEND_BYTES - envelope);
    expect(r.say(send("slider", { content: { method: "custom" }, buffers: [exact] }))).toBe(true);
    const over = new Uint8Array(MAX_COMM_SEND_BYTES - envelope + 1);
    expect(r.say(send("slider", { content: { method: "custom" }, buffers: [over] }))).toBe(false);
    const text = "é".repeat(MAX_COMM_SEND_BYTES / 2);
    expect(r.say(send("slider", { content: { text } }))).toBe(false);
    expect(dropped(r.host)).toEqual({ too_large: 2 });
    expect(r.bridge.sent).toHaveLength(1);
  });

  it("allows 60 sends in a second, refuses the 61st, and allows more once the second has passed", async () => {
    const r = await started();
    for (let i = 0; i < COMM_SENDS_PER_SECOND; i += 1) {
      r.clock.t = i * 10;
      expect(r.say(send())).toBe(true);
    }
    r.clock.t = 999;
    expect(r.say(send())).toBe(false);
    expect(dropped(r.host)).toEqual({ rate_limited: 1 });
    r.clock.t = 1_001;
    expect(r.say(send())).toBe(true);
    expect(r.bridge.sent).toHaveLength(COMM_SENDS_PER_SECOND + 1);
  });

  it.each([
    ["a non-object content", { content: [1, 2] }],
    ["a missing msg_id", { msg_id: undefined }],
    ["a numeric comm id", { comm_id: 7 }],
    ["a buffer that is not binary", { buffers: ["abc"] }],
  ])("refuses %s", async (_label, extra) => {
    const r = await started();
    expect(r.say(send("slider", extra))).toBe(false);
    expect(dropped(r.host)).toEqual({ bad_fields: 1 });
  });

  it("refuses content that cannot be serialized", async () => {
    const r = await started();
    const content: Record<string, unknown> = {};
    content.self = content;
    expect(r.say(send("slider", { content }))).toBe(false);
    expect(dropped(r.host)).toEqual({ bad_fields: 1 });
  });
});

describe("FrameHost kernel relay", () => {
  it("relays traffic for its models only, with the parent msg id", async () => {
    const r = await started();
    r.frame.posted.length = 0;
    r.bridge.emit({ type: "comm.msg", comm_id: "elsewhere", content: { method: "update" } });
    r.bridge.emit({ type: "comm.msg", comm_id: "slider", content: { method: "update", state: { value: 9 } }, parent_msg_id: "m-7" });
    expect(r.frame.posted.map((p) => p.message)).toEqual([
      expect.objectContaining({ type: "comm.msg", comm_id: "slider", parent_msg_id: "m-7", buffers: [] }),
    ]);
  });

  it("grows the closure when an update refers to a new model, replaying it before the update", async () => {
    const LABEL = open("label", { value: "hi", style: "IPY_MODEL_style" });
    const STYLE = open("style", {});
    const r = await started({ closures: { slider: [LAYOUT, SLIDER], label: [STYLE, LABEL] } });
    r.frame.posted.length = 0;
    r.bridge.emit({ type: "comm.msg", comm_id: "slider", content: { method: "update", state: { description: "IPY_MODEL_label" } } });
    expect(r.frame.posted.map((p) => [p.message.type, p.message.comm_id])).toEqual([
      ["comm.open", "style"],
      ["comm.open", "label"],
      ["comm.msg", "slider"],
    ]);
    // The new model's traffic now flows, and the frame may send on it.
    r.bridge.emit({ type: "comm.msg", comm_id: "label", content: { method: "update" } });
    expect(r.frame.last("comm.msg")?.comm_id).toBe("label");
    expect(r.say(send("label"))).toBe(true);
  });

  it("does not hand a frame a model just because the kernel opened it", async () => {
    const r = await started();
    r.frame.posted.length = 0;
    r.bridge.emit({ type: "comm.open", ...open("unrelated") });
    expect(r.frame.posted).toEqual([]);
    expect(r.say(send("unrelated"))).toBe(false);
  });

  it("forgets a closed comm", async () => {
    const r = await started();
    r.bridge.emit({ type: "comm.close", comm_id: "slider" });
    expect(r.frame.last("comm.close")).toMatchObject({ comm_id: "slider" });
    expect(r.say(send("slider"))).toBe(false);
    expect(dropped(r.host)).toEqual({ unknown_comm: 1 });
  });
});

describe("FrameHost recycle and navigation", () => {
  it("recycles with a new nonce and refuses the old frame's messages", async () => {
    const recycled: string[] = [];
    const r = await started({ events: { onRecycle: (nonce) => recycled.push(nonce) } });
    const old = r.host.nonce;
    r.host.recycle();
    expect(r.host.nonce).not.toBe(old);
    expect(recycled).toEqual([r.host.nonce]);
    expect(r.host.src.endsWith(`#n=${r.host.nonce}`)).toBe(true);
    expect(r.say(send("slider"), { frame: old })).toBe(false);
    expect(dropped(r.host)).toEqual({ wrong_nonce: 1 });
    // The old subscription is gone; the new frame starts over.
    expect(r.bridge.subscribers).toBe(0);
    r.frame.posted.length = 0;
    r.say({ type: "ready" });
    await flush();
    expect(r.frame.types()).toEqual(["module", "init"]);
    expect(r.frame.posted.every((p) => p.message.frame === r.host.nonce)).toBe(true);
    expect(r.bridge.subscribers).toBe(1);
  });

  it("stops talking to a frame that navigated away", async () => {
    const navigated: boolean[] = [];
    const r = rig({ events: { onNavigated: () => navigated.push(true) } });
    r.host.handleLoad();
    await flush();
    r.frame.posted.length = 0;
    const before = r.host.nonce;
    r.host.handleLoad();
    expect(navigated).toEqual([true]);
    // The old nonce is discarded on the second load.
    expect(r.host.nonce).not.toBe(before);
    r.bridge.emit({ type: "comm.msg", comm_id: "slider", content: {} });
    r.host.setTheme("dark");
    expect(r.frame.posted).toEqual([]);
    expect(r.say({ type: "size", height: 1 })).toBe(false);
    expect(dropped(r.host)).toEqual({ detached: 1 });
  });

  it("talks again only after a recycle and a fresh ready under a new nonce", async () => {
    const r = rig();
    r.host.handleLoad();
    await flush();
    r.host.handleLoad();
    r.host.recycle();
    r.frame.posted.length = 0;
    r.host.handleLoad();
    await flush();
    expect(r.frame.posted.length).toBeGreaterThan(0);
    expect(new Set(r.frame.posted.map((p) => p.message.frame))).toEqual(new Set([r.host.nonce]));
  });

  it("ignores a module that arrives after the frame was recycled", async () => {
    let release: (code: string) => void = () => {};
    const frame = new RecordingWindow();
    const host = new FrameHost({
      services: { bootstrapUrl: "https://c.example/c/nb-output/abcdef12", loadModule: () => new Promise((resolve) => (release = resolve)) },
      outputId: "o",
      mime: "text/html",
      data: "x",
      theme: "light",
      readonly: false,
    });
    host.attach(() => frame);
    host.handleLoad();
    host.recycle();
    release("code");
    await flush();
    expect(frame.posted).toEqual([]);
  });
});

describe("FrameHost modules, links and errors", () => {
  it("ships the widget manager with the adapter that registers it, and other renderers as they are", async () => {
    const widget = await started();
    const shipped = String(widget.frame.last("module")?.code);
    // The prelude comes first, so it holds the register before the bundle runs.
    expect(shipped.endsWith("/* @alkera/widgets */")).toBe(true);
    expect(shipped).toContain('name !== "alkera-widgets"');
    expect(shipped).toContain('bootstrap("@alkera/widgets"');
    const html = await started({ mime: "text/html", data: "x" });
    expect(html.frame.last("module")?.code).toBe("/* nb-html */");
  });

  it("answers a widget asset request by its content reference", async () => {
    const r = await started();
    const asset = `alkera-asset:sha256:${"0f".repeat(32)}`;
    expect(r.say({ type: "need_module", name: asset, version: "asset" })).toBe(true);
    await flush();
    expect(r.loads.at(-1)).toBe(`${asset}@asset`);
    expect(r.frame.last("module")).toMatchObject({ name: asset, version: "asset" });
  });

  it("answers need_module from a widget frame through loadModule, once per module", async () => {
    const r = await started();
    expect(r.say({ type: "need_module", name: "bqplot", version: "^0.5" })).toBe(true);
    expect(r.say({ type: "need_module", name: "bqplot", version: "^0.5" })).toBe(true);
    await flush();
    expect(r.loads).toEqual(["@alkera/widgets@1", "bqplot@^0.5"]);
    expect(r.frame.last("module")).toMatchObject({ name: "bqplot", version: "^0.5", code: "/* bqplot */" });
  });

  it("refuses need_module from a plain HTML output", async () => {
    const r = await started({ mime: "text/html", data: "x" });
    expect(r.say({ type: "need_module", name: "@alkera/widgets", version: "1" })).toBe(false);
    expect(dropped(r.host)).toEqual({ not_widget: 1 });
  });

  it("reports a module the page cannot vouch for", async () => {
    const r = await started({ refuse: ["evil"] });
    r.say({ type: "need_module", name: "evil", version: "1" });
    await flush();
    expect(r.errors).toEqual(["The module evil couldn't be loaded."]);
  });

  it("hands a link to the confirmation and never opens it itself", () => {
    const opened: unknown[] = [];
    const originalOpen = window.open;
    window.open = ((...args: unknown[]) => {
      opened.push(args);
      return null;
    }) as typeof window.open;
    try {
      const r = rig({ mime: "text/html", data: "x" });
      expect(r.say({ type: "link", href: "https://example.com/a b" })).toBe(true);
      expect(r.links).toEqual(["https://example.com/a%20b"]);
      expect(opened).toEqual([]);
    } finally {
      window.open = originalOpen;
    }
  });

  it.each([["javascript:alert(1)"], ["data:text/html,hi"], ["relative/path"], ["blob:https://x/1"]])(
    "refuses the link %s",
    (href) => {
      const r = rig({ mime: "text/html", data: "x" });
      expect(r.say({ type: "link", href })).toBe(false);
      expect(dropped(r.host)).toEqual({ bad_link: 1 });
      expect(r.links).toEqual([]);
    },
  );

  it("refuses links when the page cannot ask the reader", () => {
    const r = rig({ mime: "text/html", data: "x", confirmLink: false });
    expect(r.say({ type: "link", href: "https://example.com" })).toBe(false);
    expect(dropped(r.host)).toEqual({ no_link_handler: 1 });
  });

  it("surfaces a frame's error, truncated, and stops after a flood", () => {
    const r = rig();
    expect(r.say({ type: "error", message: "x".repeat(2_000) })).toBe(true);
    expect(r.errors[0]).toHaveLength(500);
    for (let i = 0; i < 60; i += 1) r.say({ type: "error", message: `e${i}` });
    expect(r.errors).toHaveLength(50);
    expect(dropped(r.host)).toEqual({ too_many_errors: 11 });
  });
});

describe("modelReferences", () => {
  it("finds references at any depth and ignores look-alikes", () => {
    expect(
      modelReferences({ a: "IPY_MODEL_x", b: ["IPY_MODEL_y", { c: "IPY_MODEL_x" }], d: "IPY_MODEL_", e: "xIPY_MODEL_z" }).sort(),
    ).toEqual(["x", "y"]);
  });
});
