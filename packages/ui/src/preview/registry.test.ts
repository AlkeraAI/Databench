import { cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PreviewFacts, PreviewNeed, PreviewProps, PreviewRenderer } from "./types";

afterEach(cleanup);

type Registry = typeof import("./registry");

/** Every case gets its own module instance. The registry is module state, so a
 *  renderer one case registers must not decide the next case's answer. */
async function freshRegistry(): Promise<Registry> {
  vi.resetModules();
  return await import("./registry");
}

function facts(over: Partial<PreviewFacts> = {}): PreviewFacts {
  return { mime: "text/plain", name: "notes.txt", size: 10, ...over };
}

function renderer(over: Partial<PreviewRenderer> & Pick<PreviewRenderer, "id">): PreviewRenderer {
  return {
    priority: 0,
    match: () => true,
    needs: (): PreviewNeed => "text",
    Component: () => null,
    ...over,
  };
}

function previewProps(over: Partial<PreviewProps> = {}): PreviewProps {
  return {
    facts: facts(),
    content: { kind: "none" },
    version: "etag-1",
    status: "ready",
    onDownload: () => {},
    ...over,
  };
}

describe("planning a preview", () => {
  it("answers the fallback for a type no renderer claims", async () => {
    const { planPreview, FALLBACK_RENDERER_ID } = await freshRegistry();

    const plan = planPreview(facts({ mime: "application/x-widget", name: "a.widget", size: 4 }));

    expect(plan.renderer.id).toBe(FALLBACK_RENDERER_ID);
    expect(plan.need).toBe("none");
  });

  it.each([
    ["the low one first", ["low", "high"]],
    ["the high one first", ["high", "low"]],
  ])("gives a contested file to the higher priority, registered %s", async (_name, order) => {
    const { planPreview, registerPreviewRenderer } = await freshRegistry();
    const built: Record<string, PreviewRenderer> = {
      low: renderer({ id: "low", priority: 1, needs: () => "text" }),
      high: renderer({ id: "high", priority: 2, needs: () => "blob" }),
    };
    for (const id of order) registerPreviewRenderer(built[id]);

    const plan = planPreview(facts());

    expect(plan.renderer.id).toBe("high");
    expect(plan.need).toBe("blob");
  });

  it("breaks a priority tie in favour of the newest registration, so a surface can override a built-in", async () => {
    const { planPreview, registerPreviewRenderer } = await freshRegistry();
    registerPreviewRenderer(renderer({ id: "stock", priority: 5 }));
    registerPreviewRenderer(renderer({ id: "override", priority: 5 }));

    expect(planPreview(facts()).renderer.id).toBe("override");
  });

  it("moves a re-registered renderer to the front of a tie it had already lost", async () => {
    const { planPreview, registerPreviewRenderer } = await freshRegistry();
    registerPreviewRenderer(renderer({ id: "stock", priority: 5 }));
    registerPreviewRenderer(renderer({ id: "override", priority: 5 }));
    registerPreviewRenderer(renderer({ id: "stock", priority: 5 }));

    expect(planPreview(facts()).renderer.id).toBe("stock");
  });

  it("reads the name only where a renderer asks for it", async () => {
    const { planPreview, registerPreviewRenderer } = await freshRegistry();
    registerPreviewRenderer(
      renderer({ id: "text", priority: 1, match: (f) => f.mime === "text/plain" }),
    );
    registerPreviewRenderer(
      renderer({
        id: "markdown",
        priority: 2,
        match: (f) => f.mime === "text/plain" && f.name.endsWith(".md"),
      }),
    );

    expect(planPreview(facts({ name: "notes.txt" })).renderer.id).toBe("text");
    expect(planPreview(facts({ name: "notes.md" })).renderer.id).toBe("markdown");
  });

  it("gives a file of any size to the renderer that claims it", async () => {
    const { planPreview, registerPreviewRenderer } = await freshRegistry();
    registerPreviewRenderer(renderer({ id: "rich", priority: 2, needs: () => "blob" }));
    registerPreviewRenderer(renderer({ id: "plain", priority: 1 }));

    for (const size of [0, 1_001, 5_000_000_000]) {
      const plan = planPreview(facts({ size }));
      expect(plan.renderer.id).toBe("rich");
      expect(plan.need).toBe("blob");
    }
  });

  it("replaces a renderer registered again under the same id instead of keeping both", async () => {
    const { planPreview, registerPreviewRenderer, FALLBACK_RENDERER_ID } = await freshRegistry();
    registerPreviewRenderer(renderer({ id: "html", priority: 9 }));
    registerPreviewRenderer(renderer({ id: "html", priority: 9, match: () => false }));

    expect(planPreview(facts()).renderer.id).toBe(FALLBACK_RENDERER_ID);
  });

  it("reports whether unregistering removed anything", async () => {
    const { planPreview, registerPreviewRenderer, unregisterPreviewRenderer } =
      await freshRegistry();
    registerPreviewRenderer(renderer({ id: "html", priority: 9 }));

    expect(unregisterPreviewRenderer("never-registered")).toBe(false);
    expect(planPreview(facts()).renderer.id).toBe("html");
    expect(unregisterPreviewRenderer("html")).toBe(true);
    expect(planPreview(facts()).renderer.id).not.toBe("html");
  });

  it("refuses to answer when nothing matches, which is what the fallback is there to prevent", async () => {
    const { planPreview, unregisterPreviewRenderer, FALLBACK_RENDERER_ID } = await freshRegistry();

    expect(unregisterPreviewRenderer(FALLBACK_RENDERER_ID)).toBe(true);

    expect(() => planPreview(facts())).toThrow(/no preview renderer/i);
  });

  it("answers a huge file with its renderer even when the fallback is gone", async () => {
    const { planPreview, registerPreviewRenderer, unregisterPreviewRenderer, FALLBACK_RENDERER_ID } =
      await freshRegistry();
    registerPreviewRenderer(renderer({ id: "text", priority: 1 }));
    expect(unregisterPreviewRenderer(FALLBACK_RENDERER_ID)).toBe(true);

    expect(planPreview(facts({ size: 99 * 1024 ** 3 })).renderer.id).toBe("text");
  });
});

describe("the built-in renderers at any size", () => {
  const GIB = 1024 ** 3;
  const MB = 1000 * 1000;

  it.each([
    ["a 5 GiB text file", "text/plain", "server.log", 5 * GIB, "text", "text"],
    ["a 5 GiB markdown file", "text/markdown", "book.md", 5 * GIB, "markdown", "text"],
    ["a 5 GiB spreadsheet", "text/csv", "rows.csv", 5 * GIB, "csv", "text"],
    ["a 5 GiB source file", "text/plain", "dump.sql", 5 * GIB, "code", "text"],
    ["a 900 MB video", "video/mp4", "talk.mp4", 900 * MB, "media", "blob"],
    ["a 900 MB image", "image/png", "scan.png", 900 * MB, "image", "blob"],
    ["a 900 MB pdf", "application/pdf", "atlas.pdf", 900 * MB, "pdf", "frame"],
  ])("plans %s for its renderer", async (_name, mime, name, size, id, need) => {
    vi.resetModules();
    const { planPreview } = await import("./registry");
    await import("./defaults");

    const plan = planPreview({ mime, name, size });

    expect(plan.renderer.id).toBe(id);
    expect(plan.need).toBe(need);
  });
});

describe("the fallback renderer", () => {
  async function fallbackComponent(): Promise<PreviewRenderer["Component"]> {
    const { planPreview } = await freshRegistry();
    return planPreview(facts({ mime: "application/x-widget", name: "a.widget" })).renderer.Component;
  }

  it("names the file, its sniffed type and its size, and says there is no preview for it", async () => {
    const Fallback = await fallbackComponent();

    render(
      createElement(
        Fallback,
        previewProps({ facts: { mime: "application/zip", name: "bundle.zip", size: 2048 } }),
      ),
    );

    expect(screen.getByText("bundle.zip")).toBeInTheDocument();
    expect(screen.getByText(/application\/zip/)).toBeInTheDocument();
    expect(screen.getByText(/2 KB/)).toBeInTheDocument();
    expect(screen.getByText("Preview is not available for this type")).toBeInTheDocument();
  });

  it("names a large file's size in megabytes", async () => {
    const Fallback = await fallbackComponent();

    render(
      createElement(
        Fallback,
        previewProps({ facts: { mime: "application/zip", name: "a.zip", size: 96 * 1024 * 1024 } }),
      ),
    );

    expect(screen.getByText("application/zip · 100.7 MB")).toBeInTheDocument();
  });

  it("hands a download request back to the host", async () => {
    const Fallback = await fallbackComponent();
    const onDownload = vi.fn();

    render(createElement(Fallback, previewProps({ onDownload })));
    screen.getByRole("button", { name: "Download" }).click();

    expect(onDownload).toHaveBeenCalledTimes(1);
  });
});

describe("planning with a renderer the host names", () => {
  it("uses that renderer and asks for what it needs, whatever the type would get", async () => {
    const registry = await freshRegistry();
    registry.registerPreviewRenderer(renderer({ id: "text", priority: 10, needs: () => "text" }));
    registry.registerPreviewRenderer(renderer({ id: "frame", priority: 1, match: () => false, needs: () => "frame" }));

    expect(registry.planPreview(facts()).renderer.id).toBe("text");
    const plan = registry.planPreviewWith(facts(), "frame");
    expect(plan.renderer.id).toBe("frame");
    expect(plan.need).toBe("frame");
  });

  it("falls back to the type's own plan for an id nobody registered, or none", async () => {
    const registry = await freshRegistry();
    registry.registerPreviewRenderer(renderer({ id: "text", priority: 10 }));
    expect(registry.planPreviewWith(facts(), "missing").renderer.id).toBe("text");
    expect(registry.planPreviewWith(facts(), undefined).renderer.id).toBe("text");
  });

  it("still draws the card for bytes that have not arrived", async () => {
    const registry = await freshRegistry();
    registry.registerPreviewRenderer(renderer({ id: "frame", needs: () => "frame" }));
    const plan = registry.planPreviewWith(facts({ synced: false }), "frame");
    expect(plan.renderer.id).toBe(registry.FALLBACK_RENDERER_ID);
    expect(plan.unsynced).toBe(true);
  });
});
