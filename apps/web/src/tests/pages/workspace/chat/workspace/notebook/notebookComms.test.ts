// Widget comms on the page: models replayed with their latest state, only the
// models a view reaches, the kernel's idle relayed as a status; frames
// attached at the engine's hub under the id the server gives them, their
// messages sent under that id with buffers as base64; and a reader, whom the
// attach route refuses, shown the widget read-only instead of a broken output.

import { afterEach, describe, expect, it, vi } from "vitest";

import { FrameHost, WIDGET_VIEW_MIME, type CommInbound, type FrameAttach } from "@alkera/notebook-ui";

import type { FrameAttachRequest, FrameAttached } from "@/api/notebooks";
import {
  NotebookComms,
  base64Of,
  bufferOf,
  openOf,
  referencedModels,
  routeTransport,
} from "@/pages/workspace/chat/workspace/notebook/notebookComms";

const open = (id: string, state: Record<string, unknown>) => ({ type: "comm.open", comm_id: id, content: { comm_id: id, target_name: "jupyter.widget", data: { state } } });

describe("notebook comms", () => {
  it("replays the displayed model and every model it reaches, with their latest state", () => {
    const comms = new NotebookComms(() => {});
    comms.handle(open("layout", { width: "1px" }));
    comms.handle(open("slider", { value: 1, layout: "IPY_MODEL_layout" }));
    comms.handle(open("box", { children: ["IPY_MODEL_slider"] }));
    comms.handle(open("other", { value: 9 }));
    comms.handle({ type: "comm.msg", comm_id: "slider", content: { comm_id: "slider", data: { method: "update", state: { value: 5 } } } });
    const opens = comms.opensFor("box");
    expect(opens.map((o) => o.comm_id)).toEqual(["layout", "slider", "box"]);
    expect(opens.find((o) => o.comm_id === "slider")!.data.state).toEqual({ value: 5, layout: "IPY_MODEL_layout" });
  });

  it("fans messages out, closes models and relays the kernel's idle as a status", () => {
    const comms = new NotebookComms(() => {});
    const seen: CommInbound[] = [];
    comms.subscribe((m) => seen.push(m));
    comms.handle(open("w", { value: 1 }));
    comms.handle({ type: "comm.msg", comm_id: "w", content: { data: { method: "custom" } }, parent_msg_id: "m1" });
    comms.handle({ type: "comm.idle", msg_id: "m1" });
    comms.handle({ type: "comm.close", comm_id: "w" });
    expect(seen.map((m) => m.type)).toEqual(["comm.open", "comm.msg", "comm.status", "comm.close"]);
    expect(seen[2]).toEqual({ type: "comm.status", msg_id: "m1", execution_state: "idle" });
    expect(comms.opensFor("w")).toEqual([]);
  });

  it("does not take events that are not comm traffic", () => {
    expect(new NotebookComms(() => {}).handle({ type: "cell.status" })).toBe(false);
  });

  it("decodes base64 buffers back to the bytes", () => {
    const bytes = Uint8Array.from([0, 255, 7]).buffer;
    expect(base64Of(bytes)).toBe("AP8H");
    expect(new Uint8Array(bufferOf(base64Of(bytes))!)).toEqual(new Uint8Array(bytes));
    expect(bufferOf("not base64!")).toBeNull();
  });

  it("sends nothing for a frame that is not attached: the comm route takes only attached frames", () => {
    const sent: unknown[] = [];
    const comms = new NotebookComms((m) => sent.push(m));
    comms.send({ comm_id: "w", msg_id: "m2", content: { data: {} }, buffers: [] });
    comms.send({ comm_id: "w", msg_id: "m3", content: { data: {} }, buffers: [], frame_id: "never-attached" });
    expect(sent).toEqual([]);
  });

  it("finds references at any depth", () => {
    expect([...referencedModels({ a: ["IPY_MODEL_x", { b: "IPY_MODEL_y" }], c: "plain" })].sort()).toEqual(["x", "y"]);
  });
});

describe("frames kept by the engine's widget hub", () => {
  const SLIDER_OPEN = { type: "comm.open", comm_id: "slider", target_name: "jupyter.widget", data: { state: { value: 3 } } };

  function hub(answer: (request: FrameAttachRequest, n: number) => FrameAttached | null = (_r, n) => ({ frame_id: `frm_${n}`, opens: [{ frame_id: `frm_${n}`, message: SLIDER_OPEN }] })) {
    const requests: FrameAttachRequest[] = [];
    const detached: string[] = [];
    const sent: unknown[] = [];
    const comms = new NotebookComms((m) => sent.push(m), {
      attach: async (request) => {
        requests.push(request);
        return answer(request, requests.length);
      },
      detach: (id) => void detached.push(id),
    });
    return { comms, requests, detached, sent };
  }

  const frame = (id: string, output = "c1/0"): FrameAttach => ({ frame_id: id, output_id: output, model_id: "slider" });

  it("asks for the frame by output and model, and takes its replays", async () => {
    const { comms, requests } = hub();
    const a = await comms.bridge().attach!(frame("local-a"), () => {});
    expect(requests).toEqual([{ output_id: "c1/0", model_ids: ["slider"] }]);
    expect(a?.opens).toEqual([{ comm_id: "slider", target_name: "jupyter.widget", data: { state: { value: 3 } } }]);
  });

  it("routes a frame the traffic addressed to the id the server gave it, not its own", async () => {
    const { comms } = hub();
    const bridge = comms.bridge();
    const mine: CommInbound[] = [];
    const theirs: CommInbound[] = [];
    await bridge.attach!(frame("local-a"), (m) => mine.push(m));
    await bridge.attach!(frame("local-b"), (m) => theirs.push(m));
    comms.handle({ type: "frame.message", frame_id: "frm_1", message: { type: "comm.msg", comm_id: "slider", content: { data: { method: "update" } } }, buffers: ["AP8H"] });
    comms.handle({ type: "frame.message", frame_id: "frm_1", message: { type: "comm.status", msg_id: "m1" } });
    comms.handle({ type: "frame.message", frame_id: "local-a", message: { type: "comm.close", comm_id: "slider" } });
    expect(mine.map((m) => m.type)).toEqual(["comm.msg", "comm.status"]);
    expect(new Uint8Array((mine[0] as { buffers: ArrayBuffer[] }).buffers[0]!)).toEqual(Uint8Array.from([0, 255, 7]));
    expect(theirs).toEqual([]);
  });

  it("sends a frame's messages under the server's frame id, buffers as base64", async () => {
    const { comms, sent } = hub();
    const bridge = comms.bridge();
    await bridge.attach!(frame("local-a"), () => {});
    bridge.send({ comm_id: "slider", msg_id: "m9", content: { data: {} }, buffers: [Uint8Array.from([0, 255, 7]).buffer], frame_id: "local-a" });
    expect(sent).toEqual([{ frame_id: "frm_1", comm_id: "slider", msg_id: "m9", content: { data: {} }, buffers: ["AP8H"] }]);
  });

  it("detaches under the server's id once, then neither routes to the frame nor sends from it", async () => {
    const { comms, detached, sent } = hub();
    const bridge = comms.bridge();
    const seen: CommInbound[] = [];
    const a = await bridge.attach!(frame("local-a"), (m) => seen.push(m));
    a!.detach();
    a!.detach();
    comms.handle({ type: "frame.message", frame_id: "frm_1", message: { type: "comm.status", msg_id: "m1" } });
    bridge.send({ comm_id: "slider", msg_id: "m2", content: {}, buffers: [], frame_id: "local-a" });
    expect(detached).toEqual(["frm_1"]);
    expect(seen).toEqual([]);
    expect(sent).toEqual([]);
  });

  it("hands the frame what the hub routed it before the attach answered", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const comms = new NotebookComms(() => {}, {
      attach: async () => {
        await gate;
        return { frame_id: "frm_9", opens: [] };
      },
      detach: () => {},
    });
    const pending = comms.bridge().attach!(frame("local-a"), () => {});
    comms.handle({ type: "frame.message", frame_id: "frm_9", message: { type: "comm.status", msg_id: "m0" } });
    comms.handle({ type: "frame.message", frame_id: "frm_other", message: { type: "comm.status", msg_id: "mx" } });
    release();
    const a = await pending;
    expect(a?.pending).toEqual([{ type: "comm.status", msg_id: "m0", execution_state: "idle" }]);
  });

  it("reads a replay sent as a frame message or as the open itself", () => {
    expect(openOf({ frame_id: "f", message: SLIDER_OPEN })).toMatchObject({ comm_id: "slider", data: { state: { value: 3 } } });
    expect(openOf({ comm_id: "w1", data: { state: { value: 1 } } })).toEqual({ comm_id: "w1", target_name: "jupyter.widget", data: { state: { value: 1 } } });
    expect(openOf({ message: { type: "comm.close", comm_id: "w1" } })).toBeNull();
  });

  it("offers no attach without a hub transport", () => {
    expect(new NotebookComms(() => {}).bridge().attach).toBeUndefined();
  });
});

describe("the notebook routes as a frame transport", () => {
  afterEach(() => vi.unstubAllGlobals());

  function stub(status: number, body: unknown) {
    const calls: { url: string; method: string; body: unknown }[] = [];
    const fetchImpl = vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url: String(url), method: init.method ?? "GET", body: init.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    }) as unknown as typeof fetch;
    return { calls, fetchImpl };
  }

  it("posts exactly the attach request, narrowed to this tab's socket", async () => {
    const { calls, fetchImpl } = stub(200, { frame_id: "frm_1", opens: [] });
    const answer = await routeTransport("d1", "n1", () => "p:7", fetchImpl).attach({ output_id: "c1/0", model_ids: ["slider"] });
    expect(answer).toEqual({ frame_id: "frm_1", opens: [] });
    expect(calls).toEqual([{ url: expect.stringMatching(/\/api\/v1\/notebooks\/d1\/n1\/frames$/), method: "POST", body: { output_id: "c1/0", model_ids: ["slider"], peer_id: "p:7" } }]);
  });

  it("leaves the peer out before the socket has one", async () => {
    const { calls, fetchImpl } = stub(200, { frame_id: "frm_1", opens: [] });
    await routeTransport("d1", "n1", () => null, fetchImpl).attach({ output_id: "c1/0", model_ids: ["slider"] });
    expect(calls[0]!.body).toEqual({ output_id: "c1/0", model_ids: ["slider"] });
  });

  it("answers a reader's refusal as no frame, and any other failure as an error", async () => {
    const refused = stub(403, { error: { code: "notebook.run_refused", message: "Running needs Can edit" } });
    await expect(routeTransport("d1", "n1", () => null, refused.fetchImpl).attach({ output_id: "o" })).resolves.toBeNull();
    const broken = stub(503, { error: { code: "notebook.kernel_unavailable", message: "no kernel" } });
    await expect(routeTransport("d1", "n1", () => null, broken.fetchImpl).attach({ output_id: "o" })).rejects.toMatchObject({ status: 503, code: "notebook.kernel_unavailable" });
  });

  it("shows a reader's widget read-only from the broadcast when the attach is refused", async () => {
    const { calls, fetchImpl } = stub(403, { error: { code: "notebook.run_refused", message: "Running needs Can edit" } });
    const sent: unknown[] = [];
    const comms = new NotebookComms((m) => sent.push(m), routeTransport("d1", "n1", () => "p:1", fetchImpl));
    comms.handle(open("slider", { value: 4 }));
    const posted: Record<string, unknown>[] = [];
    const errors: string[] = [];
    const frameWindow = { postMessage: (m: unknown) => void posted.push(m as Record<string, unknown>) };
    const host = new FrameHost({
      services: { bootstrapUrl: "https://c.example/c/nb-output/abcdef12", loadModule: async (n) => `/* ${n} */`, comms: comms.bridge() },
      outputId: "c1/0",
      mime: WIDGET_VIEW_MIME,
      data: { model_id: "slider" },
      theme: "light",
      readonly: false,
      events: { onError: (m) => errors.push(m) },
      now: () => 0,
      randomBytes: (b) => b.fill(7),
    });
    host.attach(() => frameWindow);
    const say = (data: Record<string, unknown>) => host.handleMessage({ source: frameWindow, origin: "null", data: { alk: 1, frame: host.nonce, ...data } });
    say({ type: "ready" });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(calls.map((c) => c.url)).toEqual([expect.stringMatching(/\/frames$/)]);
    expect(errors).toEqual([]);
    const init = posted.find((m) => m.type === "init");
    expect(init).toMatchObject({ readonly: true, opens: [{ comm_id: "slider", data: { state: { value: 4 } } }] });
    comms.handle({ type: "comm.msg", comm_id: "slider", content: { comm_id: "slider", data: { method: "update", state: { value: 5 } } } });
    expect(posted.filter((m) => m.type === "comm.msg")).toHaveLength(1);
    expect(say({ type: "comm.send", comm_id: "slider", msg_id: "m1", content: { data: {} }, buffers: [] })).toBe(false);
    expect(sent).toEqual([]);
  });
});
