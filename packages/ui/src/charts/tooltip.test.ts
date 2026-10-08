import { describe, expect, it } from "vitest";

import { DEFAULT_CHART_TOKENS } from "./theme";
import { createTooltip, fillTooltip } from "./tooltip";

const tokens = DEFAULT_CHART_TOKENS.light;
const HOSTILE = '<img src=x onerror="alert(1)"><script>alert(2)</script>';

describe("fillTooltip", () => {
  it("writes hostile keys and values as text, never as markup", () => {
    const root = document.createElement("div");
    fillTooltip(root, { [HOSTILE]: HOSTILE }, tokens);
    expect(root.querySelector("img, script")).toBeNull();
    const cells = root.querySelectorAll("td");
    expect(cells[0]?.textContent).toBe(HOSTILE);
    expect(cells[1]?.textContent).toBe(HOSTILE);
  });

  it("writes a scalar value as one line of text", () => {
    const root = document.createElement("div");
    fillTooltip(root, HOSTILE, tokens);
    expect(root.children).toHaveLength(0);
    expect(root.textContent).toBe(HOSTILE);
  });

  it("bounds a long value and a wide row", () => {
    const root = document.createElement("div");
    const wide = Object.fromEntries(Array.from({ length: 40 }, (_, i) => [`k${i}`, "v".repeat(500)]));
    fillTooltip(root, wide, tokens);
    expect(root.querySelectorAll("tr")).toHaveLength(24);
    expect(root.querySelector("td:last-child")?.textContent?.length).toBe(200);
  });
});

describe("createTooltip", () => {
  it("shows at the pointer, hides on an empty value, and removes itself", () => {
    const host = document.createElement("div");
    document.body.append(host);
    const tip = createTooltip(host, tokens);
    const el = host.querySelector<HTMLElement>("[role=tooltip]")!;
    tip.show(null, new MouseEvent("mousemove", { clientX: 10, clientY: 20 }), null, { a: 1 });
    expect(el.style.display).toBe("block");
    expect(el.style.top).toBe("32px");
    tip.show(null, new MouseEvent("mousemove"), null, undefined);
    expect(el.style.display).toBe("none");
    tip.destroy();
    expect(host.querySelector("[role=tooltip]")).toBeNull();
    host.remove();
  });
});
