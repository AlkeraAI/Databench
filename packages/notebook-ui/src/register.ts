// Every renderer the notebook ships with: the app's own and the framed ones.

import { frameRenderers } from "./frame/FramedOutput";
import { registerDefaultOutputRenderers } from "./outputs/defaults";
import { defaultOutputRegistry, type OutputRegistry } from "./outputs/registry";

/** Register the app and frame renderers; the returned function removes them. */
export function registerNotebookRenderers(registry: OutputRegistry = defaultOutputRegistry): () => void {
  const removeApp = registerDefaultOutputRenderers(registry);
  const removers = frameRenderers.map((renderer) => registry.register(renderer));
  return () => {
    removeApp();
    removers.forEach((remove) => remove());
  };
}
