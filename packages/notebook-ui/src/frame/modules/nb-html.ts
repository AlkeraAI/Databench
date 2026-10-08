// HTML outputs, drawn the way Jupyter draws them: into the document, with the
// output's own inline scripts run in order. The frame is what makes that safe:
// an opaque origin with no network, so a script can draw but reach nothing.
// Links are caught by the bootstrap and handed to the page for confirmation.

import type { FrameModule } from "../protocol";
import { outputText, registerFrameModule } from "./register";

/** Puts `html` into `root` and runs its scripts. A script inserted through
 *  `innerHTML` never runs, so each one is replaced by a fresh element carrying
 *  the same attributes and text, which runs when it is connected. */
export function renderHtml(root: HTMLElement, html: string): void {
  const template = document.createElement("template");
  template.innerHTML = html;
  for (const old of Array.from(template.content.querySelectorAll("script"))) {
    const script = document.createElement("script");
    for (const attribute of Array.from(old.attributes)) script.setAttribute(attribute.name, attribute.value);
    script.textContent = old.textContent;
    old.replaceWith(script);
  }
  root.replaceChildren(template.content);
}

export const htmlModule: FrameModule = {
  mimes: ["text/html"],
  render(init, api) {
    renderHtml(api.root, outputText(init.data));
  },
};

registerFrameModule("nb-html", htmlModule);
