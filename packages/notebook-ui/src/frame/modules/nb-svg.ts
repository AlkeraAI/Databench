// SVG outputs, parsed as XML so namespaces and entities behave as in a file.

import type { FrameModule } from "../protocol";
import { outputText, registerFrameModule } from "./register";

export function renderSvg(root: HTMLElement, text: string): void {
  const parsed = new DOMParser().parseFromString(text, "image/svg+xml");
  const svg = parsed.documentElement;
  if (parsed.getElementsByTagName("parsererror").length > 0 || svg.localName !== "svg") {
    throw new Error("This SVG output could not be read.");
  }
  const node = document.importNode(svg, true);
  node.style.maxWidth = "100%";
  node.style.height = "auto";
  root.replaceChildren(node);
}

export const svgModule: FrameModule = {
  mimes: ["image/svg+xml"],
  render(init, api) {
    renderSvg(api.root, outputText(init.data));
  },
};

registerFrameModule("nb-svg", svgModule);
