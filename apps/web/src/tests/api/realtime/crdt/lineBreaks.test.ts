// Offsets between the editor and the Loro text, over real CodeMirror documents
// split the way the editor splits them.

import { EditorState } from "@codemirror/state";
import { describe, expect, it } from "vitest";

import { fromLoro, lineSeparatorFor, toLoro, type LineSeparator } from "@/api/realtime/crdt/lineBreaks";

function docOf(text: string, separator: LineSeparator) {
  return EditorState.create({ doc: text, extensions: EditorState.lineSeparator.of(separator) }).doc;
}

describe("lineSeparatorFor", () => {
  it.each([
    ["a\r\nb\r\n", "\r\n"],
    ["a\nb\n", "\n"],
    ["a\r\nb\n", "\n"],
    ["no breaks", "\n"],
    ["", "\n"],
    ["a\rb", "\n"],
    ["\r\n", "\r\n"],
  ])("splits %j on %j", (text, separator) => {
    expect(lineSeparatorFor(text)).toBe(separator);
  });
});

describe("with CRLF", () => {
  const text = "ab\r\ncd\r\n\r\nef";
  const doc = docOf(text, "\r\n");

  it("joins back to the very same text", () => {
    expect(doc.sliceString(0, doc.length, "\r\n")).toBe(text);
  });

  it("maps every editor position to the Loro offset of the same character and back", () => {
    for (let pos = 0; pos <= doc.length; pos += 1) {
      const index = toLoro(doc, pos, "\r\n");
      expect(fromLoro(doc, index, "\r\n")).toBe(pos);
      // The character after the position is the same on both sides.
      const after = doc.sliceString(pos, pos + 1, "\r\n");
      if (after !== "\r\n") expect(text.slice(index, index + after.length)).toBe(after);
    }
  });

  it("has no editor position between the two halves of a break", () => {
    // "ab\r\n": offset 3 is the "\n" of the first break.
    expect(fromLoro(doc, 2, "\r\n")).toBe(2);
    expect(fromLoro(doc, 3, "\r\n")).toBeNull();
    expect(fromLoro(doc, 4, "\r\n")).toBe(3);
    expect(fromLoro(doc, text.length, "\r\n")).toBe(doc.length);
  });
});

describe("with LF", () => {
  it("is the identity, whatever else the text holds", () => {
    const doc = docOf("a\r\nb\rc", "\n");
    for (const n of [0, 1, 2, 3, 4, 5, 6]) {
      expect(toLoro(doc, n, "\n")).toBe(n);
      expect(fromLoro(doc, n, "\n")).toBe(n);
    }
  });
});
