import { describe, expect, it } from "vitest";

import { isEditableText, previewKindFor, type PreviewKind } from "./kind";

// The one question every renderer keys on: what IS this file? A sniffed mime is
// the best answer when the server produced one, but a writer that never set a
// content type leaves `application/octet-stream` on bytes that plainly are a PNG —
// so the name gets to answer where the mime has said nothing.

const facts = (name: string, mime: string, size = 1024) => ({
  name,
  mime,
  size,
});

describe("a mime the server actually sniffed", () => {
  it.each<[string, PreviewKind]>([
    ["image/png", "image"],
    ["image/jpeg", "image"],
    ["image/gif", "image"],
    ["image/webp", "image"],
    ["image/avif", "image"],
    ["image/bmp", "image"],
    ["image/svg+xml", "svg"],
    ["application/pdf", "pdf"],
    ["text/html", "html"],
    ["text/html; charset=utf-8", "html"],
    ["text/csv", "csv"],
    ["text/tab-separated-values", "csv"],
    ["text/markdown", "markdown"],
    ["application/json", "code"],
    ["application/yaml", "code"],
    ["audio/mpeg", "audio"],
    ["audio/wav", "audio"],
    ["video/mp4", "video"],
    ["video/quicktime", "video"],
    ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "office"],
    ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "office"],
    ["application/vnd.openxmlformats-officedocument.presentationml.presentation", "office"],
  ])("%s decides the kind on its own", (mime, kind) => {
    expect(previewKindFor(facts("anything.bin", mime))).toBe(kind);
  });

  it("outranks a name that disagrees — a sniffed PNG named .txt is an image", () => {
    expect(previewKindFor(facts("report.txt", "image/png"))).toBe("image");
  });
});

describe("a name answering for a mime that said nothing", () => {
  it.each<[string, PreviewKind]>([
    ["linked-q3.png", "image"],
    ["shot.jpg", "image"],
    ["shot.jpeg", "image"],
    ["loop.gif", "image"],
    ["hero.webp", "image"],
    ["hero.avif", "image"],
    ["old.bmp", "image"],
    ["logo.svg", "svg"],
    ["contract.pdf", "pdf"],
    ["rows.csv", "csv"],
    ["rows.tsv", "csv"],
    ["notes.md", "markdown"],
    ["notes.markdown", "markdown"],
    ["config.json", "code"],
    ["config.yaml", "code"],
    ["config.yml", "code"],
    ["config.toml", "code"],
    ["main.py", "code"],
    ["main.ts", "code"],
    ["App.tsx", "code"],
    ["main.rs", "code"],
    ["main.go", "code"],
    ["query.sql", "code"],
    ["run.sh", "code"],
    ["notes.txt", "text"],
    ["server.log", "text"],
    ["page.html", "html"],
    ["page.htm", "html"],
    ["theme.mp3", "audio"],
    ["theme.wav", "audio"],
    ["theme.m4a", "audio"],
    ["clip.mp4", "video"],
    ["clip.webm", "video"],
    ["clip.mov", "video"],
    ["book.xlsx", "office"],
    ["memo.docx", "office"],
    ["deck.pptx", "office"],
  ])("%s is read off the extension", (name, kind) => {
    expect(previewKindFor(facts(name, "application/octet-stream"))).toBe(kind);
  });

  it.each(["", "application/octet-stream", "binary/octet-stream", "application/x-binary", "*/*"])(
    "%s is not evidence, so the name decides",
    (mime) => {
      expect(previewKindFor(facts("linked-q3.png", mime))).toBe("image");
    },
  );

  it("is case-insensitive about the extension", () => {
    expect(previewKindFor(facts("LINKED-Q3.PNG", "application/octet-stream"))).toBe("image");
  });

  it("reads the LAST extension, not the first", () => {
    expect(previewKindFor(facts("archive.png.gz", "application/octet-stream"))).toBe("binary");
  });

  it("falls to binary when neither the mime nor the name says anything", () => {
    expect(previewKindFor(facts("dump", "application/octet-stream"))).toBe("binary");
  });

  it("does not read a dotfile's whole name as an extension", () => {
    expect(previewKindFor(facts(".gitignore", "application/octet-stream"))).toBe("binary");
  });
});

describe("text/plain, which is a family rather than a type", () => {
  it.each<[string, PreviewKind]>([
    ["rows.csv", "csv"],
    ["notes.md", "markdown"],
    ["main.py", "code"],
    ["config.json", "code"],
    ["notes.txt", "text"],
    ["untitled", "text"],
  ])("%s under text/plain is refined by its name", (name, kind) => {
    expect(previewKindFor(facts(name, "text/plain"))).toBe(kind);
  });

  it("does not let a name pull text/plain out of the text family", () => {
    // Bytes the server really did read as text are text, whatever they are called:
    // handing them to the image renderer would draw a broken picture.
    expect(previewKindFor(facts("linked-q3.png", "text/plain"))).toBe("text");
  });
});

describe("isEditableText", () => {
  it.each<[PreviewKind, boolean]>([
    ["text", true],
    ["code", true],
    ["markdown", true],
    ["csv", false],
    ["image", false],
    ["binary", false],
  ])("%s: %s", (kind, editable) => {
    expect(isEditableText(kind)).toBe(editable);
  });
});
