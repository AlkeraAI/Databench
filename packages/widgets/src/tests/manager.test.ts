import { afterEach, describe, expect, it } from "vitest";

import { ASSET_REF_PREFIX, WIDGET_STATE_MIME, WIDGET_VIEW_MIME, type CommOpenMessage, type JSONObject } from "../protocol";
import { harness, layout, open, settle, until, type Harness } from "./harness";

afterEach(() => {
  document.body.innerHTML = "";
});

function init(h: Harness, opens: CommOpenMessage[], modelId: string, readonly = false): void {
  h.manager.handle({ type: "init", theme: "light", output_id: "o1", mime: WIDGET_VIEW_MIME, data: { model_id: modelId, version_major: 2 }, opens, readonly });
}

function slider(value = 3): CommOpenMessage[] {
  const lay = layout();
  const style = open("SliderStyleModel", "@jupyter-widgets/controls", "StyleView", {});
  const model = open("IntSliderModel", "@jupyter-widgets/controls", "IntSliderView", {
    value,
    min: 0,
    max: 10,
    step: 1,
    layout: `IPY_MODEL_${lay.comm_id}`,
    style: `IPY_MODEL_${style.comm_id}`,
  });
  return [lay, style, model];
}

async function model(h: Harness, id: string) {
  return (await h.manager.get_model(id))!;
}

describe("rendering from comm-open replays", () => {
  it("draws a control whose state comes from the replays", async () => {
    const h = harness();
    const opens = slider(7);
    init(h, opens, opens[2].comm_id);
    await until(() => h.root.querySelector(".widget-readout") !== null);
    expect(h.root.querySelector(".widget-readout")!.textContent).toBe("7");
    expect(h.errors()).toEqual([]);
  });

  it("follows a kernel update", async () => {
    const h = harness();
    const opens = slider(1);
    const id = opens[2].comm_id;
    init(h, opens, id);
    await until(() => h.root.querySelector(".widget-readout") !== null);
    h.manager.handle({ type: "comm.msg", comm_id: id, content: { comm_id: id, data: { method: "update", state: { value: 9 }, buffer_paths: [] } }, parent_msg_id: null });
    await until(() => h.root.querySelector(".widget-readout")!.textContent === "9");
  });

  it("renders a kernel-less snapshot through set_state", async () => {
    const h = harness();
    h.manager.handle({
      type: "init",
      theme: "dark",
      output_id: "o1",
      mime: WIDGET_STATE_MIME,
      data: {
        model_id: "m1",
        version_major: 2,
        version_minor: 0,
        state: {
          m1: { model_name: "HTMLModel", model_module: "@jupyter-widgets/controls", model_module_version: "2.0.0", state: { value: "<b>saved</b>", _view_name: "HTMLView", _view_module: "@jupyter-widgets/controls", _view_module_version: "2.0.0" } },
        },
      },
      opens: [],
      readonly: true,
    });
    await until(() => h.root.textContent!.includes("saved"));
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});

describe("frontend changes and the idle relay", () => {
  it("sends a change with a msg_id and the Jupyter comm content", async () => {
    const h = harness();
    const opens = slider(1);
    const id = opens[2].comm_id;
    init(h, opens, id);
    const m = await model(h, id);
    m.set("value", 4);
    m.save_changes();
    expect(h.sends()).toHaveLength(1);
    const [send] = h.sends();
    expect(send.comm_id).toBe(id);
    expect(send.msg_id).toMatch(/^frame-/);
    expect(send.content).toMatchObject({ comm_id: id, data: { method: "update", state: { value: 4 } } });
  });

  it("holds later changes until the kernel's idle for the first arrives, then sends the latest", async () => {
    const h = harness();
    const opens = slider(0);
    const id = opens[2].comm_id;
    init(h, opens, id);
    const m = await model(h, id);
    for (const v of [1, 2, 3, 4, 5]) {
      m.set("value", v);
      m.save_changes();
    }
    expect(h.sends()).toHaveLength(1);
    h.manager.handle({ type: "comm.status", msg_id: h.sends()[0].msg_id, execution_state: "idle" });
    await settle();
    const sends = h.sends();
    expect(sends.length).toBe(2);
    expect((sends[1].content.data as JSONObject).state).toMatchObject({ value: 5 });
  });

  it("without the idle relay, later changes never leave", async () => {
    const h = harness();
    const opens = slider(0);
    const id = opens[2].comm_id;
    init(h, opens, id);
    const m = await model(h, id);
    for (const v of [1, 2, 3]) {
      m.set("value", v);
      m.save_changes();
    }
    await settle();
    expect(h.sends()).toHaveLength(1);
  });
});

describe("read-only frames", () => {
  it("never posts comm.send and disables inputs", async () => {
    const h = harness();
    const opens = slider(2);
    const id = opens[2].comm_id;
    init(h, opens, id, true);
    await until(() => h.root.querySelector(".widget-readout") !== null);
    const m = await model(h, id);
    for (const v of [3, 4]) {
      m.set("value", v);
      m.save_changes();
      await settle(5);
    }
    expect(h.sends()).toEqual([]);
    expect(h.root.classList.contains("alk-readonly")).toBe(true);
    const text = open("TextModel", "@jupyter-widgets/controls", "TextView", { value: "x", layout: `IPY_MODEL_${opens[0].comm_id}` });
    h.manager.handle(text);
    await settle();
    await h.manager.display(text.comm_id);
    await settle();
    const inputs = Array.from(h.root.querySelectorAll("input"));
    expect(inputs.length).toBeGreaterThan(0);
    expect(inputs.every((i) => i.disabled)).toBe(true);
  });
});

const FAKE_LIB = `
define(["@jupyter-widgets/base", "fakedep"], function (base, dep) {
  class FakeModel extends base.DOMWidgetModel {}
  class FakeView extends base.DOMWidgetView {
    render() { this.el.className = "fake-view"; this.el.textContent = dep.greeting + " " + this.model.get("value"); }
  }
  return { FakeModel: FakeModel, FakeView: FakeView };
});`;
const FAKE_DEP = `define([], function () { return { greeting: "hello" }; });`;

describe("modules by need_module", () => {
  it("asks for an unknown module and its AMD dependencies, then renders", async () => {
    const h = harness();
    const lay = layout();
    const fake = open("FakeModel", "fakelib", "FakeView", { value: "world", layout: `IPY_MODEL_${lay.comm_id}` });
    init(h, [lay, fake], fake.comm_id);
    await until(() => h.sent.some((m) => m.type === "need_module"));
    expect(h.sent.filter((m) => m.type === "need_module")).toEqual([{ type: "need_module", name: "fakelib", version: "1.0.0" }]);
    h.manager.handle({ type: "module", name: "fakelib", version: "1.0.0", code: FAKE_LIB });
    await until(() => h.sent.filter((m) => m.type === "need_module").length === 2);
    expect(h.sent.filter((m) => m.type === "need_module")[1]).toEqual({ type: "need_module", name: "fakedep", version: "*" });
    h.manager.handle({ type: "module", name: "fakedep", version: "*", code: FAKE_DEP });
    await until(() => h.root.querySelector(".fake-view") !== null);
    expect(h.root.querySelector(".fake-view")!.textContent).toBe("hello world");
  });

  it.each([
    { name: "@jupyter-widgets/base", why: "a platform module" },
    { name: "never-asked", why: "a module nobody requested" },
  ])("refuses $why", async ({ name }) => {
    const h = harness();
    h.manager.handle({ type: "module", name, version: "1.0.0", code: `define([], function(){ throw new Error("ran"); })` });
    await settle();
    expect(h.errors()).toEqual([`refused module ${name}: not requested by this frame`]);
  });

  it("applies the ipydatagrid patch to an environment asset by module name", async () => {
    const h = harness();
    const pending = h.manager.requestModule("ipydatagrid", "1.4.0", "amd");
    const code = `define([], function () {
      class VegaExprModel { constructor(v) { this.v = v; } get(k) { return this.v; }
        updateFunction() { this._function = new Function("cell", "default_value", "functions", "return 'eval'"); } }
      return { VegaExprModel: VegaExprModel };
    });`;
    h.manager.handle({ type: "module", name: "ipydatagrid", version: "1.4.0", code });
    const mod = (await pending) as { VegaExprModel: new (v: string) => { updateFunction(): void; _function: (...a: unknown[]) => unknown } };
    const expr = new mod.VegaExprModel("cell.value > 2 ? 'red' : default_value");
    expr.updateFunction();
    expect(expr._function({ value: 3 }, "blue", {})).toBe("red");
    expect(expr._function({ value: 1 }, "blue", {})).toBe("blue");
  });

  it("leaves another library's module unpatched", async () => {
    const h = harness();
    const pending = h.manager.requestModule("otherlib", "1.4.0", "amd");
    const code = `define([], function () {
      class VegaExprModel { updateFunction() { this._function = function () { return "original"; }; } }
      return { VegaExprModel: VegaExprModel };
    });`;
    h.manager.handle({ type: "module", name: "otherlib", version: "1.4.0", code });
    const mod = (await pending) as { VegaExprModel: new () => { updateFunction(): void; _function: () => unknown } };
    const expr = new mod.VegaExprModel();
    expr.updateFunction();
    expect(expr._function()).toBe("original");
  });

  it("verifies an asset reference against its hash", async () => {
    const h = harness();
    const text = "export default { render() {} };";
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    const hex = Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
    const good = h.manager.resolveAsset(`${ASSET_REF_PREFIX}${hex}`);
    expect(h.sent.at(-1)).toEqual({ type: "need_module", name: `${ASSET_REF_PREFIX}${hex}`, version: "asset" });
    h.manager.handle({ type: "module", name: `${ASSET_REF_PREFIX}${hex}`, version: "asset", code: text });
    await expect(good).resolves.toBe(text);

    const forged = `${ASSET_REF_PREFIX}${"0".repeat(64)}`;
    const bad = h.manager.resolveAsset(forged);
    h.manager.handle({ type: "module", name: forged, version: "asset", code: text });
    await expect(bad).rejects.toThrow("content does not match its hash");
  });

  it("fails a module the parent never supplies", async () => {
    const h = harness();
    await expect(h.manager.requestModule("ghost", "1.0.0", "amd")).rejects.toThrow("ghost was not supplied");
  });
});

describe("Output, Image and links", () => {
  it("draws the outputs the kernel routed into an Output widget", async () => {
    const h = harness();
    const lay = layout();
    const out = open("OutputModel", "@jupyter-widgets/output", "OutputView", {
      layout: `IPY_MODEL_${lay.comm_id}`,
      outputs: [
        { output_type: "stream", name: "stdout", text: "clicked\n" },
        { output_type: "error", ename: "ValueError", evalue: "bad", traceback: ["\u001b[31mValueError\u001b[0m: bad"] },
        { output_type: "display_data", data: { "image/png": "iVBORw0KGgo=", "text/plain": "<Figure>" } },
      ],
    });
    init(h, [lay, out], out.comm_id);
    await until(() => h.root.querySelector(".alk-output") !== null);
    expect(h.root.querySelector(".alk-stream-stdout")!.textContent).toBe("clicked\n");
    expect(h.root.querySelector(".alk-error")!.textContent).toBe("ValueError: bad");
    expect(h.root.querySelector("img")!.getAttribute("src")).toBe("data:image/png;base64,iVBORw0KGgo=");
  });

  it("shows an Image from its binary buffer as a data URL", async () => {
    const h = harness();
    const lay = layout();
    const img = open("ImageModel", "@jupyter-widgets/controls", "ImageView", { format: "png", width: "", height: "", layout: `IPY_MODEL_${lay.comm_id}` });
    (img.data as { buffer_paths: unknown }).buffer_paths = [["value"]];
    img.buffers = [new Uint8Array([137, 80, 78, 71]).buffer];
    init(h, [lay, img], img.comm_id);
    await until(() => h.root.querySelector("img") !== null);
    expect(h.root.querySelector("img")!.getAttribute("src")).toBe("data:image/png;base64,iVBORw==");
  });

  it("asks the parent before following a link and never navigates itself", async () => {
    const h = harness();
    const a = document.createElement("a");
    a.href = "https://example.com/docs";
    h.root.appendChild(a);
    const event = new MouseEvent("click", { bubbles: true, cancelable: true });
    a.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(h.sent).toContainEqual({ type: "link", href: "https://example.com/docs" });
    const js = document.createElement("a");
    js.setAttribute("href", "javascript:alert(1)");
    h.root.appendChild(js);
    const second = new MouseEvent("click", { bubbles: true, cancelable: true });
    js.dispatchEvent(second);
    expect(second.defaultPrevented).toBe(true);
    expect(h.sent.filter((m) => m.type === "link")).toHaveLength(1);
  });

  it("closes a model on comm.close", async () => {
    const h = harness();
    const opens = slider(1);
    const id = opens[2].comm_id;
    init(h, opens, id);
    const m = await model(h, id);
    let closed = false;
    m.on("comm:close", () => {
      closed = true;
    });
    h.manager.handle({ type: "comm.close", comm_id: id });
    await settle();
    expect(closed).toBe(true);
    expect(await h.manager.get_model(id).catch(() => undefined)).toBeUndefined();
  });
});

describe("modules register through window.__alkRegister", () => {
  it("a supplied AMD module is registered under its name and version", async () => {
    const { onRegister } = await import("../registry");
    const seen: string[] = [];
    const stop = onRegister((r) => seen.push(`${r.name}@${r.version}`));
    const h = harness();
    const pending = h.manager.requestModule("amdlib", "2.1.0", "amd");
    h.manager.handle({ type: "module", name: "amdlib", version: "2.1.0", code: `define([], function () { return { K: 1 }; });` });
    await expect(pending).resolves.toEqual({ K: 1 });
    stop();
    expect(seen).toEqual(["amdlib@2.1.0"]);
  });

  it("a script that registers itself instead of calling define resolves its wait", async () => {
    const h = harness();
    const pending = h.manager.requestModule("selfreg", "1.0.0", "amd");
    h.manager.handle({ type: "module", name: "selfreg", version: "1.0.0", code: `window.__alkRegister("selfreg", "1.0.0", { Thing: 5 });` });
    await expect(pending).resolves.toEqual({ Thing: 5 });
    expect(h.errors()).toEqual([]);
  });

  it("registrations nobody asked for, or of a platform name, load nothing", async () => {
    const h = harness();
    const pending = h.manager.requestModule("hostlib", "1.0.0", "amd");
    const code = `window.__alkRegister("sneaky", "1", { Evil: 1 });
      window.__alkRegister("@jupyter-widgets/controls", "2.0.0", { IntSliderModel: 1 });
      define([], function () { return { Fine: 1 }; });`;
    h.manager.handle({ type: "module", name: "hostlib", version: "1.0.0", code });
    await expect(pending).resolves.toEqual({ Fine: 1 });
    // The platform's controls are still the bundle's own: a slider renders.
    const opens = slider(4);
    init(h, opens, opens[2].comm_id);
    await until(() => h.root.querySelector(".widget-slider") !== null);
    // "sneaky" is still unknown to this frame: a model from it is asked of the parent.
    const evil = open("Evil", "sneaky", null, {});
    h.manager.handle(evil);
    await until(() => h.sent.some((m) => m.type === "need_module" && m.name === "sneaky"));
  });
});
