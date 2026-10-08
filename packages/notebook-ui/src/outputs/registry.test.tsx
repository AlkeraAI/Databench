import { render, screen } from "@testing-library/react";

import type { MimeBundle } from "../model/types";
import { appRenderers, registerDefaultOutputRenderers } from "./defaults";
import { OutputView } from "./OutputView";
import { MIME_PREFERENCE, OutputRegistry, orderedMimes } from "./registry";
import type { FrameServices, OutputAreaContext, OutputContext, OutputRenderer, OutputRendererProps } from "./types";

const frame: FrameServices = {
  bootstrapUrl: "https://content.example/c/nb-output/abc",
  loadModule: () => Promise.reject(new Error("unused")),
};

function context(overrides: Partial<OutputContext> = {}): OutputContext {
  return {
    theme: "light",
    readonly: false,
    cellId: "c1",
    outputId: "o1",
    renderBundle: () => null,
    ...overrides,
  };
}

function renderer(id: string, mimes: string[], rank = 0, extra: Partial<OutputRenderer> = {}): OutputRenderer {
  const Component = ({ mime }: OutputRendererProps) => <span>{`${id}:${mime}`}</span>;
  return { id, mimes, rank, place: "app", Component, ...extra };
}

const framed = (id: string, mimes: string[]) =>
  renderer(id, mimes, 0, { place: "frame", available: (ctx: OutputContext) => ctx.frame !== undefined });

describe("pickRenderer", () => {
  it("takes the richest type a renderer draws, whatever order the bundle lists", () => {
    const registry = new OutputRegistry();
    registry.register(renderer("text", ["text/plain"]));
    registry.register(renderer("md", ["text/markdown"]));
    registry.register(renderer("png", ["image/png"]));
    const bundle: MimeBundle = { "text/plain": "x", "image/png": "AAAA", "text/markdown": "# x" };
    expect(registry.pick(bundle, context())).toMatchObject({ mime: "text/markdown", renderer: { id: "md" } });
  });

  it("follows the full preference order", () => {
    const registry = new OutputRegistry();
    registry.register(renderer("all", [...MIME_PREFERENCE]));
    // Remove the winner one at a time; the next in the list must win each time.
    const bundle: MimeBundle = Object.fromEntries(MIME_PREFERENCE.map((mime) => [mime, "v"]));
    for (const mime of MIME_PREFERENCE) {
      expect(registry.pick(bundle, context())?.mime).toBe(mime);
      delete bundle[mime];
    }
    expect(registry.pick(bundle, context())).toBeNull();
  });

  it.each([
    ["text/html", { "text/html": "<b>x</b>", "text/plain": "x" }],
    ["image/svg+xml", { "image/svg+xml": "<svg/>", "text/plain": "<Figure>" }],
    ["application/vnd.vegalite.v6+json", { "application/vnd.vegalite.v6+json": {}, "text/plain": "chart" }],
    ["application/vnd.jupyter.widget-view+json", { "application/vnd.jupyter.widget-view+json": {}, "text/plain": "widget" }],
  ])("falls back from framed %s to text/plain without frame services, and uses the frame with them", (mime, bundle) => {
    const registry = new OutputRegistry();
    registry.register(renderer("text", ["text/plain"]));
    registry.register(framed("frame", [mime]));
    expect(registry.pick(bundle, context())).toMatchObject({ mime: "text/plain", renderer: { id: "text" } });
    expect(registry.pick(bundle, context({ frame }))).toMatchObject({ mime, renderer: { id: "frame" } });
  });

  it("an unavailable renderer gives way to a lower-ranked available one for the same type", () => {
    const registry = new OutputRegistry();
    registry.register(framed("framed-html", ["text/html"]));
    registry.register(renderer("weak-html", ["text/html"], -10));
    expect(registry.pick({ "text/html": "<p/>" }, context())?.renderer.id).toBe("weak-html");
    expect(registry.pick({ "text/html": "<p/>" }, context({ frame }))?.renderer.id).toBe("framed-html");
  });

  it("breaks ties between renderers of a type by rank, regardless of registration order", () => {
    const registry = new OutputRegistry();
    registry.register(renderer("high", ["text/plain"], 5));
    registry.register(renderer("low", ["text/plain"], 1));
    expect(registry.pick({ "text/plain": "x" }, context())?.renderer.id).toBe("high");
  });

  it("lets the later registration win among equal ranks", () => {
    const registry = new OutputRegistry();
    registry.register(renderer("first", ["text/plain"], 0));
    registry.register(renderer("second", ["text/plain"], 0));
    expect(registry.pick({ "text/plain": "x" }, context())?.renderer.id).toBe("second");
  });

  it("a new registration wins without touching the others, and unregistering restores them", () => {
    const registry = new OutputRegistry();
    registerDefaultOutputRenderers(registry);
    const before = registry.list().map((r) => r.id);
    const bundle = { "text/plain": "x", "application/json": { a: 1 } };
    expect(registry.pick(bundle, context())?.renderer.id).toBe("alkera.json");

    const remove = registry.register(renderer("better-json", ["application/json"], 10));
    expect(registry.pick(bundle, context())?.renderer.id).toBe("better-json");
    expect(registry.list().map((r) => r.id)).toEqual([...before, "better-json"]);

    remove();
    expect(registry.pick(bundle, context())?.renderer.id).toBe("alkera.json");
  });

  it("a new MIME type is a registration and beats the generic fallbacks", () => {
    const registry = new OutputRegistry();
    registerDefaultOutputRenderers(registry);
    const bundle = { "text/plain": "repr", "application/json": {}, "application/x-custom": 1 };
    expect(registry.pick(bundle, context())?.mime).toBe("application/json");
    registry.register(renderer("custom", ["application/x-custom"]));
    expect(registry.pick(bundle, context())?.mime).toBe("application/x-custom");
    // ...but never a listed rich type.
    expect(registry.pick({ ...bundle, "text/markdown": "# x" }, context())?.mime).toBe("text/markdown");
  });

  it("a stale remover does not unregister a replacement with the same id", () => {
    const registry = new OutputRegistry();
    const removeOld = registry.register(renderer("same", ["text/plain"]));
    registry.register(renderer("same", ["text/markdown"]));
    removeOld();
    expect(registry.pick({ "text/markdown": "x" }, context())?.renderer.id).toBe("same");
  });

  it("ignores null and absent values in a bundle", () => {
    const registry = new OutputRegistry();
    registry.register(renderer("all", ["text/markdown", "text/plain"]));
    expect(registry.pick({ "text/markdown": null, "text/plain": "x" }, context())?.mime).toBe("text/plain");
    expect(registry.pick({}, context())).toBeNull();
  });

  it("orders unlisted types after rich types and before the fallbacks, keeping their own order", () => {
    expect(orderedMimes({ "text/plain": 1, "b/x": 1, "a/x": 1, "image/png": 1 })).toEqual(["image/png", "b/x", "a/x", "text/plain"]);
  });

  it("registers every app renderer by default and draws only in the app", () => {
    const registry = new OutputRegistry();
    const remove = registerDefaultOutputRenderers(registry);
    expect(registry.list().map((r) => r.id).sort()).toEqual(appRenderers.map((r) => r.id).sort());
    expect(registry.list().every((r) => r.place === "app")).toBe(true);
    remove();
    expect(registry.list()).toEqual([]);
  });
});

describe("OutputView with the registry", () => {
  const base: OutputAreaContext = { theme: "light", readonly: false, cellId: "c1" };

  it("draws the chosen renderer with the chosen MIME's value", () => {
    const registry = new OutputRegistry();
    const Probe = ({ mime, data }: OutputRendererProps) => <span>{`${mime}=${String(data)}`}</span>;
    registry.register({ id: "probe", mimes: ["text/markdown"], rank: 0, place: "app", Component: Probe });
    render(
      <OutputView
        output={{ output_id: "o", type: "display", data: { "text/plain": "p", "text/markdown": "m" } }}
        context={base}
        registry={registry}
      />,
    );
    expect(screen.getByText("text/markdown=m")).toBeInTheDocument();
  });

  it("says so when nothing draws the bundle", () => {
    render(<OutputView output={{ output_id: "o", type: "display", data: { "application/x-nothing": 1 } }} context={base} registry={new OutputRegistry()} />);
    expect(screen.getByText("No renderer for application/x-nothing")).toBeInTheDocument();
  });
});
