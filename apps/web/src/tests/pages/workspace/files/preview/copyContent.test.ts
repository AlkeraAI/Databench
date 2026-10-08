import { describe, expect, it, vi } from "vitest";

import type { PreviewContent } from "@alkera/ui";
import { copyPreviewContent, copyableKind, type CopyDeps } from "@/pages/workspace/files/preview/copyContent";

const TEXT: PreviewContent = { kind: "text", text: "alpha\nbeta\n" };
const PNG: PreviewContent = { kind: "blob", url: "blob:png", mime: "image/png" };
const JPEG: PreviewContent = { kind: "blob", url: "blob:jpeg", mime: "image/jpeg" };
const AUDIO: PreviewContent = { kind: "blob", url: "blob:audio", mime: "audio/mpeg" };
const FRAME: PreviewContent = { kind: "frame", url: "http://files/x.pdf", sandboxed: true, title: "x.pdf" };
const NONE: PreviewContent = { kind: "none" };

/** A clipboard that remembers what it was handed, and a ClipboardItem that
 *  keeps its parts, so a test reads what would have been pasted. */
function deps(over: Partial<CopyDeps> = {}) {
  const written: string[] = [];
  const items: Array<Record<string, Blob>> = [];
  class Item {
    constructor(public parts: Record<string, Blob>) {}
  }
  const clipboard = {
    writeText: vi.fn(async (text: string) => {
      written.push(text);
    }),
    write: vi.fn(async (list: ClipboardItem[]) => {
      for (const one of list) items.push((one as unknown as Item).parts);
    }),
  };
  const converted: Blob[] = [];
  const all: CopyDeps = {
    fetchBlob: async (url) => new Blob([url], { type: url === "blob:png" ? "image/png" : "image/jpeg" }),
    toPng: async (blob) => {
      converted.push(blob);
      return new Blob(["png"], { type: "image/png" });
    },
    clipboard,
    clipboardItem: Item as unknown as typeof ClipboardItem,
    ...over,
  };
  return { all, written, items, converted, clipboard };
}

describe("what a preview's Copy would put on the clipboard", () => {
  it.each([
    ["text", TEXT, "text"],
    ["a PNG", PNG, "image"],
    ["a JPEG", JPEG, "image"],
    ["a sound", AUDIO, null],
    ["a framed document", FRAME, null],
    ["nothing", NONE, null],
  ])("for %s is %s", (_what, content, kind) => {
    expect(copyableKind(content)).toBe(kind);
  });
});

describe("copying a preview", () => {
  it("copies text as text, byte for byte", async () => {
    const d = deps();
    expect(await copyPreviewContent(TEXT, d.all)).toBe("copied");
    expect(d.written).toEqual(["alpha\nbeta\n"]);
    expect(d.items).toEqual([]);
  });

  it("copies a PNG as the image it is, without re-encoding it", async () => {
    const d = deps();
    expect(await copyPreviewContent(PNG, d.all)).toBe("copied");
    expect(d.converted).toEqual([]);
    expect(Object.keys(d.items[0] ?? {})).toEqual(["image/png"]);
    // The very blob that was fetched, untouched: same type, same size.
    expect(d.items[0]?.["image/png"]?.type).toBe("image/png");
    expect(d.items[0]?.["image/png"]?.size).toBe("blob:png".length);
  });

  it("re-encodes any other raster as PNG, which is the one image the clipboard admits", async () => {
    const d = deps();
    expect(await copyPreviewContent(JPEG, d.all)).toBe("copied");
    expect(d.converted.map((blob) => blob.type)).toEqual(["image/jpeg"]);
    expect(d.items[0]?.["image/png"]?.type).toBe("image/png");
    expect(d.items[0]?.["image/png"]?.size).toBe("png".length);
  });

  it.each([
    ["a sound", AUDIO],
    ["a framed document", FRAME],
    ["nothing", NONE],
  ])("has no answer for %s and touches the clipboard for none of it", async (_what, content) => {
    const d = deps();
    expect(await copyPreviewContent(content, d.all)).toBe("unsupported");
    expect(d.clipboard.writeText).not.toHaveBeenCalled();
    expect(d.clipboard.write).not.toHaveBeenCalled();
  });

  it("says it failed when the page has no clipboard, rather than throwing", async () => {
    const d = deps({ clipboard: undefined });
    expect(await copyPreviewContent(TEXT, d.all)).toBe("failed");
    expect(await copyPreviewContent(PNG, d.all)).toBe("failed");
  });

  it("says it failed when the clipboard refuses, rather than throwing", async () => {
    const d = deps();
    d.clipboard.writeText.mockRejectedValueOnce(new Error("denied"));
    expect(await copyPreviewContent(TEXT, d.all)).toBe("failed");
  });
});

describe("copying a text file still arriving in windows", () => {
  const WINDOW = "row 1\nrow 2\n";
  const FILE = "row 1\nrow 2\nrow 3\nrow 4\n";

  function windowed(
    whole?: () => Promise<string>,
    over: { text?: string; loaded?: number } = {},
  ): PreviewContent {
    return {
      kind: "text",
      text: over.text ?? WINDOW,
      loaded: over.loaded ?? WINDOW.length,
      total: FILE.length,
      more: async () => {},
      ...(whole ? { whole } : {}),
    };
  }

  it("copies the whole file, not the window on screen", async () => {
    const d = deps();
    expect(await copyPreviewContent(windowed(async () => FILE), d.all)).toBe("copied");
    expect(d.written).toEqual([FILE]);
  });

  it("copies what is on screen once every window has landed", async () => {
    const d = deps();
    const landed = windowed(async () => "a stale read", { text: FILE, loaded: FILE.length });
    expect(await copyPreviewContent(landed, d.all)).toBe("copied");
    expect(d.written).toEqual([FILE]);
  });

  it("says it failed, and copies no part of the file, when the whole read fails", async () => {
    const d = deps();
    const failing = windowed(async () => {
      throw new Error("The next part of the file could not be loaded");
    });
    expect(await copyPreviewContent(failing, d.all)).toBe("failed");
    expect(d.written).toEqual([]);
  });

  it("says it failed rather than copy a part when the host offers no whole read", async () => {
    const d = deps();
    expect(await copyPreviewContent(windowed(), d.all)).toBe("failed");
    expect(d.written).toEqual([]);
  });
});
