// The renderers the app draws itself. Framed renderers (HTML, SVG, Vega-Lite,
// Plotly, widgets) register separately, next to the frame host.

import { chartRenderer } from "./chart";
import { imageRenderer } from "./image";
import { jsonRenderer } from "./JsonTree";
import { layoutRenderer } from "./LayoutOutput";
import { markdownRenderer } from "./MarkdownOutput";
import { defaultOutputRegistry, type OutputRegistry } from "./registry";
import { tableRenderer } from "./TableView";
import { textRenderer } from "./text";
import type { OutputRenderer } from "./types";

export const appRenderers: readonly OutputRenderer[] = [
  textRenderer,
  imageRenderer,
  markdownRenderer,
  jsonRenderer,
  tableRenderer,
  layoutRenderer,
  chartRenderer,
];

/** Registers the app renderers (again, harmlessly: an id replaces itself).
 *  Returns a function that removes them. */
export function registerDefaultOutputRenderers(registry: OutputRegistry = defaultOutputRegistry): () => void {
  const removers = appRenderers.map((renderer) => registry.register(renderer));
  return () => removers.forEach((remove) => remove());
}
