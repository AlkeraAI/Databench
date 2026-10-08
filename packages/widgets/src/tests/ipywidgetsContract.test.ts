// The comm-open replays a real ipywidgets slider produces, recorded from a
// real kernel through the engine's widget hub
// (packages/alkera-notebook/tests/cases/test_nbeng_ipywidgets_contract.py
// asserts the hub still sends exactly this), rendered by the real manager.
// Nothing here supplies the protocol version: the replays carry it or the
// manager refuses them.
import { afterEach, describe, expect, it } from "vitest";

import { WIDGET_VIEW_MIME, type CommOpenMessage } from "../protocol";
import recorded from "./fixtures/ipywidgets-slider-opens.json";
import { harness, until, type Harness } from "./harness";

const opens = recorded.opens as unknown as CommOpenMessage[];

afterEach(() => {
  document.body.innerHTML = "";
});

function init(h: Harness, replays: CommOpenMessage[]): void {
  h.manager.handle({ type: "init", theme: "light", output_id: "o1", mime: WIDGET_VIEW_MIME, data: { model_id: recorded.model_id, version_major: 2 }, opens: replays, readonly: false });
}

describe("a real ipywidgets slider's replays", () => {
  it("draw the slider at the value the kernel set", async () => {
    const h = harness();
    init(h, opens);
    await until(() => h.root.querySelector(".widget-readout") !== null);
    expect(h.root.querySelector(".widget-readout")!.textContent).toBe("3");
    expect(h.root.querySelector(".widget-slider")).not.toBeNull();
    expect(h.errors()).toEqual([]);
  });

  it("send a change back on the slider's comm", async () => {
    const h = harness();
    init(h, opens);
    await until(() => h.root.querySelector(".widget-readout") !== null);
    const model = (await h.manager.get_model(recorded.model_id))!;
    model.set("value", 8);
    model.save_changes();
    await until(() => h.sends().length > 0);
    const [send] = h.sends();
    expect(send.comm_id).toBe(recorded.model_id);
    expect(send.content).toMatchObject({ data: { method: "update", state: { value: 8 } } });
  });

  it("are refused, and said so, when the protocol version is missing", async () => {
    const h = harness();
    init(h, opens.map((o) => ({ ...o, metadata: {} })));
    await until(() => h.errors().length === opens.length);
    expect(h.errors().every((e) => e.includes("Wrong widget protocol version"))).toBe(true);
    expect(h.root.querySelector(".widget-slider")).toBeNull();
  });
});
