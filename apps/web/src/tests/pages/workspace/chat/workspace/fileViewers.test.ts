// Which ways each type of file can be shown, which it opens in, and how a later
// viewer joins by registration.

import { afterEach, describe, expect, it } from "vitest";

import type { PreviewFacts } from "@alkera/ui";

import {
  EDIT_VIEW,
  PREVIEW_VIEW,
  counterpart,
  fileViewerFor,
  offersEditing,
  openingView,
  previewOf,
  readOnlyRendererFor,
  registerFileViewer,
  resolveView,
  unregisterFileViewer,
  type FileViewDef,
  type FileViewer,
} from "@/pages/workspace/chat/workspace/fileViewers";

const facts = (name: string, mime: string): PreviewFacts => ({ name, mime, size: 10 });

afterEach(() => {
  unregisterFileViewer("notebook");
});

describe("the views each type offers", () => {
  it.each([
    // Source, config and text: an editor and nothing else.
    ["main.py", "text/plain", [EDIT_VIEW], EDIT_VIEW, undefined],
    ["data.json", "application/json", [EDIT_VIEW], EDIT_VIEW, undefined],
    ["deploy.yaml", "text/yaml", [EDIT_VIEW], EDIT_VIEW, undefined],
    ["pyproject.toml", "text/plain", [EDIT_VIEW], EDIT_VIEW, undefined],
    ["notes.txt", "text/plain", [EDIT_VIEW], EDIT_VIEW, undefined],
    // Every type with a preview opens in it, its source one toggle away. A page
    // and a drawing render only through the framed renderer.
    ["README.md", "text/plain", [EDIT_VIEW, PREVIEW_VIEW], PREVIEW_VIEW, "markdown"],
    ["report.html", "text/html", [EDIT_VIEW, PREVIEW_VIEW], PREVIEW_VIEW, "html"],
    ["chart.svg", "image/svg+xml", [EDIT_VIEW, PREVIEW_VIEW], PREVIEW_VIEW, "html"],
    ["revenue.csv", "text/csv", [EDIT_VIEW, PREVIEW_VIEW], PREVIEW_VIEW, "csv"],
    ["revenue.tsv", "text/tab-separated-values", [EDIT_VIEW, PREVIEW_VIEW], PREVIEW_VIEW, "csv"],
    // Pictures, documents, media and binaries are drawn and never edited.
    ["photo.png", "image/png", [PREVIEW_VIEW], PREVIEW_VIEW, undefined],
    ["deck.pdf", "application/pdf", [PREVIEW_VIEW], PREVIEW_VIEW, undefined],
    ["talk.mp3", "audio/mpeg", [PREVIEW_VIEW], PREVIEW_VIEW, undefined],
    ["demo.mp4", "video/mp4", [PREVIEW_VIEW], PREVIEW_VIEW, undefined],
    ["bundle.zip", "application/zip", [PREVIEW_VIEW], PREVIEW_VIEW, undefined],
  ])("%s (%s): %j, opening in %s", (name, mime, views, opens, renderer) => {
    const viewer = fileViewerFor(facts(name, mime));
    expect(viewer.views.map((view) => view.id)).toEqual(views);
    expect(openingView(viewer).id).toBe(opens);
    const preview = viewer.views.find((view) => view.role === "preview");
    if (preview && preview.draw.kind === "render") expect(preview.draw.renderer).toBe(renderer);
    // Every editing view is the live editor: there is nothing to save.
    for (const view of viewer.views.filter((candidate) => candidate.role === "edit")) {
      expect(view.draw.kind).toBe("live-editor");
    }
  });

  it("draws a note's and a table's rendering from the live text, and a page's only from the drive", () => {
    const live = (name: string, mime: string): boolean | undefined => {
      const view = fileViewerFor(facts(name, mime)).views.find((candidate) => candidate.role === "preview");
      return view?.draw.kind === "render" ? view.draw.live === true : undefined;
    };
    expect(live("README.md", "text/plain")).toBe(true);
    expect(live("revenue.csv", "text/csv")).toBe(true);
    expect(live("report.html", "text/html")).toBe(false);
    expect(live("chart.svg", "image/svg+xml")).toBe(false);
  });
});

describe("choosing a view", () => {
  const markdown = fileViewerFor(facts("README.md", "text/plain"));

  it("shows the view a tab asked for when the file offers it", () => {
    expect(resolveView(markdown, PREVIEW_VIEW).id).toBe(PREVIEW_VIEW);
  });

  it("shows the view the file opens in for a view the file does not offer, or none", () => {
    expect(resolveView(markdown, "notebook").id).toBe(PREVIEW_VIEW);
    expect(resolveView(markdown, undefined).id).toBe(PREVIEW_VIEW);
    expect(resolveView(markdown, EDIT_VIEW).id).toBe(EDIT_VIEW);
    const image = fileViewerFor(facts("photo.png", "image/png"));
    expect(resolveView(image, EDIT_VIEW).id).toBe(PREVIEW_VIEW);
  });

  it("toggles to the other role, and has nothing to toggle to on a one-view file", () => {
    expect(counterpart(markdown, resolveView(markdown, EDIT_VIEW))?.id).toBe(PREVIEW_VIEW);
    expect(counterpart(markdown, resolveView(markdown, PREVIEW_VIEW))?.id).toBe(EDIT_VIEW);
    const code = fileViewerFor(facts("main.py", "text/plain"));
    expect(counterpart(code, resolveView(code, undefined))).toBeNull();
  });

  it("offers a preview to the side only where there is a source beside it", () => {
    expect(previewOf(markdown)?.id).toBe(PREVIEW_VIEW);
    expect(previewOf(fileViewerFor(facts("photo.png", "image/png")))).toBeNull();
    expect(previewOf(fileViewerFor(facts("main.py", "text/plain")))).toBeNull();
  });

  it("edits live only the types with an editing view", () => {
    expect(offersEditing(markdown)).toBe(true);
    expect(offersEditing(fileViewerFor(facts("deck.pdf", "application/pdf")))).toBe(false);
  });

  it.each([
    ["main.py", "text/plain", "code"],
    ["report.html", "text/html", "code"],
    ["chart.svg", "image/svg+xml", "code"],
    ["notes.txt", "text/plain", "text"],
    ["README.md", "text/plain", "text"],
  ])("shows %s read-only with the %s renderer when the editor cannot open it", (name, mime, renderer) => {
    expect(readOnlyRendererFor(facts(name, mime))).toBe(renderer);
  });
});

describe("a later viewer", () => {
  const notebook: FileViewer = {
    id: "notebook",
    priority: 50,
    match: (file) => file.name.endsWith(".ipynb"),
    views: [
      { id: "cells", role: "edit", label: "Cells", draw: { kind: "custom", Component: () => null } },
      { id: EDIT_VIEW, role: "edit", label: "Source", draw: { kind: "live-editor" } },
    ],
  };

  it("joins by registration and wins the files it matches", () => {
    expect(fileViewerFor(facts("model.ipynb", "application/json")).id).toBe("text");
    registerFileViewer(notebook);
    expect(fileViewerFor(facts("model.ipynb", "application/json")).id).toBe("notebook");
    // Everything it does not match is decided as before.
    expect(fileViewerFor(facts("data.json", "application/json")).id).toBe("text");
  });

  it("opens in its first view when it offers no preview, and in its preview when it does", () => {
    registerFileViewer(notebook);
    const file = facts("model.ipynb", "application/json");
    expect(resolveView(fileViewerFor(file), undefined).id).toBe("cells");
    const rendered: FileViewDef = { id: "rendered", role: "preview", label: "Output", draw: { kind: "render" } };
    registerFileViewer({ ...notebook, views: [...notebook.views, rendered] });
    expect(resolveView(fileViewerFor(file), undefined).id).toBe("rendered");
  });

  it("is refused when it offers no views", () => {
    expect(() => registerFileViewer({ ...notebook, views: [] })).toThrow();
  });
});
