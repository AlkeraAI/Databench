import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { resolveModelMark } from "./modelMarks";

afterEach(cleanup);

/** Render the resolved node and hand back its svg, so assertions check what a
 *  consumer actually paints rather than the registry's internals. */
function svgOf(node: ReturnType<typeof resolveModelMark>): SVGElement {
  const { container } = render(<>{node}</>);
  const svg = container.querySelector("svg");
  if (!svg) throw new Error("resolved mark rendered no svg");
  return svg;
}

// The two brand marks are visually distinct by their fill: the Anthropic blossom
// keeps its brand vermilion; the OpenAI monoblossom rides currentColor.
const ANTHROPIC_FILL = "#d97757";

describe("resolveModelMark", () => {
  it.each([
    [["claude-opus-4-7", undefined], ANTHROPIC_FILL],
    [["opus", "Claude Opus 4.7"], ANTHROPIC_FILL],
    [["anthropic/claude-3", undefined], ANTHROPIC_FILL],
    [["gpt-5-mini", undefined], "currentColor"],
    [["mini", "GPT-5 mini"], "currentColor"],
    [["openai/o3", undefined], "currentColor"],
  ] as const)("%s takes its provider's brand mark", ([id, label], fill) => {
    const svg = svgOf(resolveModelMark(id, label));
    expect(svg.classList.contains("alk-provider-mark")).toBe(true);
    expect(svg.querySelector("path")?.getAttribute("fill")).toBe(fill);
  });

  it("an unmapped provider falls back to a brain glyph", () => {
    // The tabler glyph carries the icon name in its class. Proves the fallback
    // path, not a mis-mapped provider.
    const svg = svgOf(resolveModelMark("llama-3", "Llama 3"));
    expect(svg.getAttribute("class") ?? "").toMatch(/icon-brain/i);
  });
});
