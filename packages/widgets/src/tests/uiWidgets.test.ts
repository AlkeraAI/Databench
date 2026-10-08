import { afterEach, describe, expect, it } from "vitest";

import { WIDGET_VIEW_MIME, type JSONObject } from "../protocol";
import { harness, layout, open, until, type Harness } from "./harness";

afterEach(() => {
  document.body.innerHTML = "";
});

async function show(view: string, state: JSONObject, readonly = false): Promise<{ h: Harness; el: HTMLElement; id: string }> {
  const h = harness();
  const lay = layout();
  const model = open("ElementModel", "@alkera/ui-widgets", view, { layout: `IPY_MODEL_${lay.comm_id}`, ...state });
  h.manager.handle({ type: "init", theme: "light", output_id: "o", mime: WIDGET_VIEW_MIME, data: { model_id: model.comm_id }, opens: [lay, model], readonly });
  await until(() => h.root.querySelector(".alk-ui") !== null);
  return { h, el: h.root.querySelector(".alk-ui") as HTMLElement, id: model.comm_id };
}

function lastValue(h: Harness): unknown {
  const send = h.sends().at(-1);
  return ((send?.content.data as JSONObject | undefined)?.state as JSONObject | undefined)?.value;
}

function fire(el: Element, type: string): void {
  el.dispatchEvent(new Event(type, { bubbles: true }));
}

describe("@alkera/ui-widgets views", () => {
  it("slider sends the moved value and shows it", async () => {
    const { h, el } = await show("SliderView", { value: 2, min: 0, max: 10, step: 1, label: "Rows" });
    const input = el.querySelector("input[type=range]") as HTMLInputElement;
    expect(input.value).toBe("2");
    expect(el.querySelector(".alk-ui-label")!.textContent).toBe("Rows");
    input.value = "6";
    fire(input, "input");
    expect(lastValue(h)).toBe(6);
    expect(el.querySelector(".alk-ui-readout")!.textContent).toBe("6");
  });

  it("password text is a password input", async () => {
    const { h, el } = await show("TextView", { value: "", kind: "password" });
    const input = el.querySelector("input") as HTMLInputElement;
    expect(input.type).toBe("password");
    input.value = "s3cret";
    fire(input, "input");
    expect(lastValue(h)).toBe("s3cret");
  });

  it.each([
    { view: "CheckboxView", selector: "input[type=checkbox]", act: (i: HTMLInputElement) => ((i.checked = true), fire(i, "change")), value: true },
    { view: "SwitchView", selector: "input[role=switch]", act: (i: HTMLInputElement) => ((i.checked = true), fire(i, "change")), value: true },
    { view: "NumberView", selector: "input[type=number]", act: (i: HTMLInputElement) => ((i.value = "4.5"), fire(i, "change")), value: 4.5 },
    { view: "DateView", selector: "input[type=date]", act: (i: HTMLInputElement) => ((i.value = "2026-10-05"), fire(i, "change")), value: "2026-10-05" },
    { view: "TextAreaView", selector: "textarea", act: (i: HTMLInputElement) => ((i.value = "a\nb"), fire(i, "input")), value: "a\nb" },
  ])("$view sends its value", async ({ view, selector, act, value }) => {
    const { h, el } = await show(view, { value: null });
    act(el.querySelector(selector) as HTMLInputElement);
    expect(lastValue(h)).toEqual(value);
  });

  it("dropdown sends the chosen label", async () => {
    const { h, el } = await show("DropdownView", { value: "b", options: ["a", "b", "c"] });
    const select = el.querySelector("select") as HTMLSelectElement;
    expect(select.value).toBe("b");
    select.value = "c";
    fire(select, "change");
    expect(lastValue(h)).toBe("c");
  });

  it("multiselect sends every checked label", async () => {
    const { h, el } = await show("MultiselectView", { value: ["a"], options: ["a", "b", "c"] });
    const boxes = Array.from(el.querySelectorAll("input")) as HTMLInputElement[];
    expect(boxes.map((b) => b.checked)).toEqual([true, false, false]);
    boxes[2].checked = true;
    fire(boxes[2], "change");
    expect(lastValue(h)).toEqual(["a", "c"]);
  });

  it("radio sends one label", async () => {
    const { h, el } = await show("RadioView", { value: "x", options: ["x", "y"] });
    const radios = Array.from(el.querySelectorAll("input")) as HTMLInputElement[];
    radios[1].checked = true;
    fire(radios[1], "change");
    expect(lastValue(h)).toBe("y");
  });

  it.each(["ButtonView", "RunButtonView"])("%s counts clicks", async (view) => {
    const { h, el } = await show(view, { value: 4, label: "Go" });
    const button = el.querySelector("button") as HTMLButtonElement;
    expect(button.textContent).toBe("Go");
    button.click();
    expect(lastValue(h)).toBe(5);
  });

  it("follows the kernel's value", async () => {
    const { h, el, id } = await show("SliderView", { value: 1, min: 0, max: 10 });
    h.manager.handle({ type: "comm.msg", comm_id: id, content: { comm_id: id, data: { method: "update", state: { value: 8 }, buffer_paths: [] } } });
    await until(() => (el.querySelector("input") as HTMLInputElement).value === "8");
  });

  it("honours disabled and read-only", async () => {
    const { el } = await show("SliderView", { value: 1, disabled: true });
    expect((el.querySelector("input") as HTMLInputElement).disabled).toBe(true);
    const ro = await show("TextView", { value: "x" }, true);
    expect((ro.el.querySelector("input") as HTMLInputElement).disabled).toBe(true);
    const input = ro.el.querySelector("input") as HTMLInputElement;
    input.value = "y";
    fire(input, "input");
    expect(ro.h.sends()).toEqual([]);
  });
});
