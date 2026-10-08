import { describe, expect, it } from "vitest";

import "./defaults";
import { FALLBACK_RENDERER_ID, planPreview } from "./registry";

const MIB = 1024 * 1024;

/** The plan every surface gets for a type, decided from the mime the SERVER sniffed. */
describe("the built-in renderers", () => {
  it.each([
    ["text/html", "html", "frame"],
    ["text/html; charset=utf-8", "html", "frame"],
    ["application/pdf", "pdf", "frame"],
    ["image/png", "image", "blob"],
    ["image/jpeg", "image", "blob"],
    ["image/gif", "image", "blob"],
    ["image/webp", "image", "blob"],
    ["image/svg+xml", "svg", "blob"],
    ["video/mp4", "media", "blob"],
    ["video/webm", "media", "blob"],
    ["audio/mpeg", "media", "blob"],
    ["audio/wav", "media", "blob"],
    ["text/plain", "text", "text"],
    ["application/octet-stream", FALLBACK_RENDERER_ID, "none"],
    ["application/zip", FALLBACK_RENDERER_ID, "none"],
  ])("plans %s as the %s renderer needing %s", (mime, id, need) => {
    const plan = planPreview({ mime, name: "file.bin", size: 2048 });

    expect(plan.renderer.id).toBe(id);
    expect(plan.need).toBe(need);
  });

  it("lets the name answer for a mime that says nothing", () => {
    // `application/octet-stream` is what a writer that set no content type leaves
    // behind — it is not a finding about the bytes. Keying on it alone put a PNG
    // behind a "Preview is not available for this type" card.
    const plan = planPreview({
      mime: "application/octet-stream",
      name: "linked-q3.png",
      size: 10,
      synced: true,
    });

    expect(plan.renderer.id).toBe("image");
  });

  it("keeps the card for a mime the server DID place, whatever the name says", () => {
    // A positive sniff is evidence: an archive named `.png` is still an archive.
    const plan = planPreview({
      mime: "application/zip",
      name: "trap.png",
      size: 10,
      synced: true,
    });

    expect(plan.renderer.id).toBe(FALLBACK_RENDERER_ID);
  });

  it("plans an empty file that DID land like any other of its type", () => {
    // Zero bytes is not the signal: an empty text file is a real, readable file.
    const plan = planPreview({ mime: "text/plain", name: "empty.txt", size: 0, synced: true });

    expect(plan.renderer.id).toBe("text");
    expect(plan.unsynced).toBe(false);
  });

  it("draws no type at all for a file whose bytes have not arrived", () => {
    // Planning the image renderer here would send the host fetching an empty body
    // and leave a broken picture where an explanation belongs.
    const plan = planPreview({
      mime: "application/octet-stream",
      name: "linked-q3.png",
      size: 0,
      synced: false,
    });

    expect(plan.renderer.id).toBe(FALLBACK_RENDERER_ID);
    expect(plan.need).toBe("none");
  });

  it.each([
    ["image/png", 65 * MIB],
    ["image/svg+xml", 65 * MIB],
    ["video/mp4", 65 * MIB],
    ["text/plain", 3 * MIB],
  ])("draws %s past the size it was once refused at", (mime, size) => {
    const plan = planPreview({ mime, name: "big", size });

    expect(plan.renderer.id).not.toBe(FALLBACK_RENDERER_ID);
    expect(plan.need).not.toBe("none");
  });

  it("streams a framed document of any size", () => {
    for (const mime of ["text/html", "application/pdf"]) {
      const plan = planPreview({ mime, name: "report", size: 800 * MIB });
      expect(plan.need).toBe("frame");
    }
  });
});
