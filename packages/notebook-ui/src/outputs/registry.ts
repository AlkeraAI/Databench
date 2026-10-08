// The output renderer registry: which renderer draws a MIME bundle.
//
// A bundle offers one value per MIME type; the registry walks the types from
// richest to plainest and hands the first one a registered, available renderer
// draws. A new type, or a better renderer for an existing one, is a
// registration; nothing here branches on a type.

import type { MimeBundle } from "../model/types";
import type { OutputContext, OutputRenderer } from "./types";

/** The MIME types a bundle is read in, richest first. A type not listed here
 *  ranks below every listed rich type and above the generic fallbacks
 *  (`application/json`, `text/plain`), so a registered custom type still beats
 *  a plain-text repr of the same value. */
export const MIME_PREFERENCE: readonly string[] = [
  "application/vnd.alkera.layout+json",
  "application/vnd.alkera.chart+json",
  "application/vnd.alkera.table+json",
  "application/vnd.jupyter.widget-view+json",
  "application/vnd.plotly.v1+json",
  "application/vnd.vegalite.v6+json",
  "application/vnd.vegalite.v5+json",
  // Before HTML: a bundle with both is Markdown with an HTML copy for other
  // readers (`alkera.md` sends both), and only the Markdown renderer draws
  // its math.
  "text/markdown",
  "text/html",
  "image/svg+xml",
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "application/json",
  "text/plain",
];

const GENERIC_FALLBACKS = new Set(["application/json", "text/plain"]);

function preferenceIndex(mime: string): number {
  const index = MIME_PREFERENCE.indexOf(mime);
  if (index >= 0) return index;
  // Unlisted types sit just before the generic fallbacks.
  return MIME_PREFERENCE.findIndex((m) => GENERIC_FALLBACKS.has(m)) - 0.5;
}

/** The bundle's MIME types in the order they are tried. */
export function orderedMimes(bundle: MimeBundle): string[] {
  const present = Object.keys(bundle).filter((mime) => bundle[mime] !== undefined && bundle[mime] !== null);
  // A stable sort keeps the bundle's own order among unlisted types.
  return present
    .map((mime, position) => ({ mime, position, rank: preferenceIndex(mime) }))
    .sort((a, b) => a.rank - b.rank || a.position - b.position)
    .map((entry) => entry.mime);
}

export interface RendererChoice {
  renderer: OutputRenderer;
  mime: string;
}

/** A set of renderers. The package keeps one default instance; a host or a
 *  test can hold its own. */
export class OutputRegistry {
  private readonly renderers = new Map<string, OutputRenderer>();
  private order = 0;
  private readonly sequence = new Map<string, number>();

  /** Adds a renderer, replacing one with the same id. Returns a function
   *  that removes it again (only if it is still the registered one). */
  register(renderer: OutputRenderer): () => void {
    this.renderers.set(renderer.id, renderer);
    this.sequence.set(renderer.id, this.order++);
    return () => {
      if (this.renderers.get(renderer.id) === renderer) this.unregister(renderer.id);
    };
  }

  unregister(id: string): boolean {
    this.sequence.delete(id);
    return this.renderers.delete(id);
  }

  list(): OutputRenderer[] {
    return [...this.renderers.values()];
  }

  /** The renderers that draw `mime` here, best first. Among equal ranks the
   *  later registration wins, so a host can override without renumbering. */
  candidates(mime: string, context: OutputContext): OutputRenderer[] {
    return this.list()
      .filter((renderer) => renderer.mimes.includes(mime))
      .filter((renderer) => (renderer.available ? renderer.available(context) : true))
      .sort((a, b) => b.rank - a.rank || (this.sequence.get(b.id) ?? 0) - (this.sequence.get(a.id) ?? 0));
  }

  /** The richest MIME in `bundle` an available renderer draws, and that
   *  renderer; `null` when nothing here draws any of it. */
  pick(bundle: MimeBundle, context: OutputContext): RendererChoice | null {
    for (const mime of orderedMimes(bundle)) {
      const [renderer] = this.candidates(mime, context);
      if (renderer) return { renderer, mime };
    }
    return null;
  }
}

/** The registry the components use unless handed another. */
export const defaultOutputRegistry = new OutputRegistry();

export function registerOutputRenderer(renderer: OutputRenderer, registry: OutputRegistry = defaultOutputRegistry): () => void {
  return registry.register(renderer);
}

export function unregisterOutputRenderer(id: string, registry: OutputRegistry = defaultOutputRegistry): boolean {
  return registry.unregister(id);
}

export function pickRenderer(
  bundle: MimeBundle,
  context: OutputContext,
  registry: OutputRegistry = defaultOutputRegistry,
): RendererChoice | null {
  return registry.pick(bundle, context);
}
