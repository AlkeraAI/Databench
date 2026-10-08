/**
 * Copying what a preview shows, by what it is.
 *
 * A Copy button on a preview must put on the clipboard the thing a reader
 * would paste: the text of a text-like file as `text/plain`, an image as an
 * image. The clipboard admits one image encoding everywhere, PNG, so any other
 * raster is drawn onto a canvas and taken back out as PNG; the reader sees the
 * same picture. A document only a frame can draw (a PDF, a page), a video, a
 * sound or bytes nothing renders have no paste-able form, so for those there
 * is no button at all rather than a button that apologises.
 */

import type { PreviewContent } from "@alkera/ui";

export type CopyableKind = "text" | "image";

/** What a Copy of this content would put on the clipboard, or `null` when
 *  nothing sensible would. */
export function copyableKind(content: PreviewContent): CopyableKind | null {
  if (content.kind === "text") return "text";
  if (content.kind === "blob" && content.mime.startsWith("image/")) return "image";
  return null;
}

export interface CopyDeps {
  /** Fetches the bytes behind a blob URL. */
  fetchBlob(url: string): Promise<Blob>;
  /** Re-encodes any raster the browser can decode as PNG. */
  toPng(blob: Blob): Promise<Blob>;
  clipboard: Pick<Clipboard, "writeText" | "write"> | undefined;
  clipboardItem: typeof ClipboardItem | undefined;
}

async function rasterToPng(blob: Blob): Promise<Blob> {
  const bitmap = await createImageBitmap(blob);
  try {
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("no 2d context");
    context.drawImage(bitmap, 0, 0);
    return await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((png) => (png ? resolve(png) : reject(new Error("no png"))), "image/png");
    });
  } finally {
    bitmap.close();
  }
}

function browserDeps(): CopyDeps {
  return {
    fetchBlob: async (url) => (await fetch(url)).blob(),
    toPng: rasterToPng,
    clipboard: typeof navigator === "undefined" ? undefined : navigator.clipboard,
    clipboardItem: typeof ClipboardItem === "undefined" ? undefined : ClipboardItem,
  };
}

/** The whole file a text preview shows. A file still arriving in windows is read
 *  once more, whole: Copy means the file, not the part of it on screen. */
async function wholeText(content: Extract<PreviewContent, { kind: "text" }>): Promise<string> {
  const { loaded, total, whole } = content;
  const partial = loaded !== undefined && total !== undefined && loaded < total;
  if (!partial) return content.text;
  if (whole === undefined) throw new Error("the rest of the file cannot be read");
  return whole();
}

/** Puts the preview's content on the clipboard in the form `copyableKind`
 *  names. `"unsupported"` is the answer for content that has no such form; a
 *  surface should not have offered the button, and it changes nothing. */
export async function copyPreviewContent(
  content: PreviewContent,
  deps: CopyDeps = browserDeps(),
): Promise<"copied" | "failed" | "unsupported"> {
  const kind = copyableKind(content);
  if (kind === null) return "unsupported";
  try {
    if (content.kind === "text") {
      if (!deps.clipboard?.writeText) return "failed";
      await deps.clipboard.writeText(await wholeText(content));
      return "copied";
    }
    if (content.kind !== "blob" || !deps.clipboard?.write || !deps.clipboardItem) return "failed";
    const raw = await deps.fetchBlob(content.url);
    const png = content.mime === "image/png" ? raw : await deps.toPng(raw);
    await deps.clipboard.write([new deps.clipboardItem({ "image/png": png })]);
    return "copied";
  } catch {
    return "failed";
  }
}
