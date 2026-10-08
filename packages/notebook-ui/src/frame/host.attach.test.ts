// A frame host whose bridge attaches frames to the engine's widget hub: the
// replays come from the attach, the frame receives exactly the traffic routed
// to it (no filtering of a broadcast), sends name the frame, and a frame that
// goes (disposed, or recycled into a new one) is detached. A read-only frame,
// or one the hub refuses (a reader), is never attached: it shows the widget
// from the broadcast and sends nothing.

import { describe, expect, it } from "vitest";

import type { AttachedFrame, CommBridge, CommInbound, CommOpen, FrameAttach, FrameServices } from "../outputs/types";
import { FrameHost } from "./host";
import { WIDGET_VIEW_MIME } from "./protocol";

class RecordingWindow {
  readonly posted: Record<string, unknown>[] = [];
  postMessage(message: unknown): void {
    this.posted.push(message as Record<string, unknown>);
  }
  of(type: string) {
    return this.posted.filter((m) => m.type === type);
  }
}

type Answer = "attach" | "fail" | "refuse";

class HubBridge implements CommBridge {
  readonly attached: FrameAttach[] = [];
  readonly detached: string[] = [];
  readonly sent: Record<string, unknown>[] = [];
  readonly routes = new Map<string, (m: CommInbound) => void>();
  /** The broadcast: what a frame that is not attached listens to. */
  readonly broadcast = new Set<(m: CommInbound) => void>();
  constructor(
    private readonly replays: CommOpen[],
    private readonly answer: Answer = "attach",
    private readonly cached: CommOpen[] = [],
    private readonly pending: CommInbound[] = [],
  ) {}
  opensFor(): CommOpen[] {
    return this.cached;
  }
  subscribe(listener: (m: CommInbound) => void): () => void {
    this.broadcast.add(listener);
    return () => void this.broadcast.delete(listener);
  }
  send(message: Record<string, unknown>): void {
    this.sent.push(message);
  }
  async attach(frame: FrameAttach, listener: (m: CommInbound) => void): Promise<AttachedFrame | null> {
    if (this.answer === "fail") throw new Error("no kernel");
    if (this.answer === "refuse") return null;
    this.attached.push(frame);
    this.routes.set(frame.frame_id, listener);
    return {
      opens: this.replays,
      ...(this.pending.length > 0 ? { pending: this.pending } : {}),
      detach: () => void this.detached.push(frame.frame_id),
    };
  }
}

const SLIDER: CommOpen = { comm_id: "slider", target_name: "jupyter.widget", data: { state: { value: 1 } } };
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

async function start(bridge: HubBridge, readonly = false) {
  const frame = new RecordingWindow();
  const errors: string[] = [];
  const services: FrameServices = { bootstrapUrl: "https://c.example/c/nb-output/abcdef12", loadModule: async (n) => `/* ${n} */`, comms: bridge };
  let seed = 0;
  const host = new FrameHost({
    services,
    outputId: "o1",
    mime: WIDGET_VIEW_MIME,
    data: { model_id: "slider" },
    theme: "light",
    readonly,
    events: { onError: (m) => errors.push(m) },
    now: () => 0,
    randomBytes: (b) => b.fill((seed += 1)),
  });
  host.attach(() => frame);
  const say = (data: Record<string, unknown>) => host.handleMessage({ source: frame, origin: "null", data: { alk: 1, frame: host.nonce, ...data } });
  say({ type: "ready" });
  await flush();
  return { host, frame, errors, say };
}

describe("a frame attached to the engine's widget hub", () => {
  it("is attached with its output and model and inits with the hub's replays", async () => {
    const bridge = new HubBridge([SLIDER], "attach", [{ ...SLIDER, comm_id: "from-broadcast" }]);
    const { frame } = await start(bridge);
    expect(bridge.attached).toEqual([{ frame_id: expect.stringMatching(/^f[0-9a-f]{24}$/), output_id: "o1", model_id: "slider" }]);
    expect(frame.of("init")[0]!.opens).toEqual([SLIDER]);
    expect(bridge.broadcast.size).toBe(0);
  });

  it.each([
    ["a read-only frame", "attach" as const, true],
    ["a frame the hub refuses", "refuse" as const, false],
  ])("shows %s read-only from the broadcast, attached nowhere", async (_name, answer, readonly) => {
    const bridge = new HubBridge([], answer, [SLIDER]);
    const { frame, errors, say } = await start(bridge, readonly);
    expect(bridge.attached).toEqual([]);
    expect(errors).toEqual([]);
    expect(frame.of("init")[0]).toMatchObject({ opens: [SLIDER], readonly: true });
    for (const listener of bridge.broadcast) listener({ type: "comm.msg", comm_id: "slider", content: { data: { method: "update", state: { value: 2 } } } });
    expect(frame.of("comm.msg")).toHaveLength(1);
    expect(say({ type: "comm.send", comm_id: "slider", msg_id: "m1", content: { data: {} }, buffers: [] })).toBe(false);
    expect(bridge.sent).toEqual([]);
  });

  it("passes on the traffic routed to it and names itself when it sends", async () => {
    const bridge = new HubBridge([SLIDER]);
    const { frame, say } = await start(bridge);
    const route = bridge.routes.get(bridge.attached[0]!.frame_id)!;
    route({ type: "comm.open", comm_id: "layout", target_name: "jupyter.widget", data: { state: {} } });
    route({ type: "comm.msg", comm_id: "layout", content: { data: { method: "update", state: { width: "1px" } } } });
    expect(frame.of("comm.open").map((m) => m.comm_id)).toEqual(["layout"]);
    expect(frame.of("comm.msg")).toHaveLength(1);
    expect(say({ type: "comm.send", comm_id: "layout", msg_id: "m1", content: { data: {} }, buffers: [] })).toBe(true);
    expect(bridge.sent[0]).toMatchObject({ comm_id: "layout", msg_id: "m1", frame_id: bridge.attached[0]!.frame_id });
    route({ type: "comm.status", msg_id: "m1", execution_state: "idle" });
    route({ type: "comm.status", msg_id: "never-sent", execution_state: "idle" });
    expect(frame.of("comm.status").map((m) => m.msg_id)).toEqual(["m1"]);
  });

  it("is detached when it goes, and a recycled frame attaches under a new id", async () => {
    const bridge = new HubBridge([SLIDER]);
    const { host, say } = await start(bridge);
    const first = bridge.attached[0]!.frame_id;
    host.recycle();
    expect(bridge.detached).toEqual([first]);
    say({ type: "ready" });
    await flush();
    expect(bridge.attached).toHaveLength(2);
    expect(bridge.attached[1]!.frame_id).not.toBe(first);
    host.dispose();
    expect(bridge.detached).toEqual([first, bridge.attached[1]!.frame_id]);
  });

  it("delivers what the hub routed before its answer, after the replays", async () => {
    const early: CommInbound = { type: "comm.msg", comm_id: "slider", content: { data: { method: "update", state: { value: 2 } } } };
    const { frame } = await start(new HubBridge([SLIDER], "attach", [], [early]));
    const types = frame.posted.map((m) => m.type).filter((t) => t === "init" || t === "comm.msg");
    expect(types).toEqual(["init", "comm.msg"]);
    expect(frame.of("comm.msg")[0]).toMatchObject({ comm_id: "slider", content: early.content });
  });

  it("says so when the hub cannot take the frame", async () => {
    const { errors, frame } = await start(new HubBridge([], "fail"));
    expect(errors).toEqual(["The widget could not reach the kernel."]);
    expect(frame.of("init")).toEqual([]);
  });
});
