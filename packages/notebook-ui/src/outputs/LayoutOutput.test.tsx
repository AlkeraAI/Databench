import { render, screen } from "@testing-library/react";

import { registerDefaultOutputRenderers } from "./defaults";
import { parseLayout } from "./LayoutOutput";
import { OutputView } from "./OutputView";
import { OutputRegistry } from "./registry";
import type { OutputAreaContext } from "./types";

const LAYOUT = "application/vnd.alkera.layout+json";
const base: OutputAreaContext = { theme: "light", readonly: false, cellId: "c1" };

function draw(payload: unknown) {
  const registry = new OutputRegistry();
  registerDefaultOutputRenderers(registry);
  return render(<OutputView output={{ output_id: "o", type: "display", data: { [LAYOUT]: payload, "text/plain": "repr" } }} context={base} registry={registry} />);
}

describe("layout renderer", () => {
  it("draws an hstack's children through the registry, each with its own renderer", () => {
    const { container } = draw({
      type: "hstack",
      gap: 24,
      align: "center",
      items: [{ "text/markdown": "**left**", "text/plain": "left" }, { "application/json": { k: 1 } }],
    });
    const stack = container.querySelector(".nb-output-stack--hstack") as HTMLElement;
    expect(stack.style.gap).toBe("24px");
    expect(stack.style.alignItems).toBe("center");
    const renderers = [...stack.querySelectorAll(":scope > .nb-output-stack-item > .nb-output-mime")].map((el) => el.getAttribute("data-renderer"));
    expect(renderers).toEqual(["alkera.markdown", "alkera.json"]);
    expect(screen.getByText("left").tagName).toBe("STRONG");
  });

  it("draws a vstack with nested layouts", () => {
    const { container } = draw({
      type: "vstack",
      items: [{ [LAYOUT]: { type: "callout", kind: "warn", body: { "text/plain": "inner" } } }],
    });
    expect(container.querySelector(".nb-output-stack--vstack .nb-output-callout--warn")).toHaveTextContent("inner");
  });

  it.each(["info", "warn", "danger", "success", "neutral"])("draws a %s callout around its body", (kind) => {
    const { container } = draw({ type: "callout", kind, body: { "text/plain": "body text" } });
    const callout = container.querySelector(`.nb-output-callout--${kind}`);
    expect(callout).toHaveTextContent("body text");
  });

  it("clamps the gap and ignores an unknown alignment", () => {
    expect(parseLayout({ type: "hstack", items: [], gap: 10_000, align: "sideways" })).toEqual({
      type: "hstack",
      items: [],
      gap: 128,
      align: undefined,
    });
    expect(parseLayout({ type: "callout", kind: "shout", body: {} })).toEqual({ type: "callout", kind: "neutral", body: {} });
  });

  it.each([
    ["an unknown type", { type: "grid", items: [] }],
    ["items that are not bundles", { type: "vstack", items: ["x"] }],
    ["a callout without a body", { type: "callout", kind: "info" }],
    ["not an object", "hstack"],
  ])("refuses %s", (_name, payload) => {
    draw(payload);
    expect(screen.getByText("This layout could not be read.")).toBeInTheDocument();
  });
});
