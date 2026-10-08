// The splice a field edit becomes: exact, caret-faithful, and never half an emoji.

import { describe, expect, it } from "vitest";

import { spliceFor, splitsPair, toCodePointBoundary, type TextSplice } from "./textBinding";

function apply(text: string, splice: TextSplice | null): string {
  if (splice === null) return text;
  return text.slice(0, splice.index) + splice.insert + text.slice(splice.index + splice.remove);
}

describe("spliceFor", () => {
  it.each([
    ["insert in the middle", "hello world", "hello, world", undefined],
    ["append", "abc", "abcd", undefined],
    ["prepend", "abc", "xabc", undefined],
    ["delete a range", "hello cruel world", "hello world", undefined],
    ["replace a selection", "the cat sat", "the dog sat", undefined],
    ["clear everything", "something", "", undefined],
    ["fill from empty", "", "something", undefined],
    ["emoji in the middle", "a😀b", "a😀😀b", 3],
    ["delete an emoji", "a😀b", "ab", 1],
    ["replace one emoji with another sharing a high surrogate", "x😀y", "x😁y", 3],
    ["newline", "line one", "line\none", 5],
  ])("turns before into after exactly: %s", (_name, before, after, caret) => {
    expect(apply(before, spliceFor(before, after, caret))).toBe(after);
  });

  it("is null when nothing changed", () => {
    expect(spliceFor("same", "same")).toBeNull();
  });

  it.each([
    // Typing "a" with the caret ending at 1 is an insert at 0, not at the end.
    ["aa", "aaa", 1, { index: 0, remove: 0, insert: "a" }],
    ["aa", "aaa", 2, { index: 1, remove: 0, insert: "a" }],
    ["aa", "aaa", 3, { index: 2, remove: 0, insert: "a" }],
    // Deleting one of a run, caret left where the deleted character was.
    ["xaaay", "xaay", 1, { index: 1, remove: 1, insert: "" }],
    ["xaaay", "xaay", 3, { index: 3, remove: 1, insert: "" }],
  ])("reads an ambiguous edit where the caret says it happened: %s -> %s (caret %i)", (before, after, caret, expected) => {
    expect(spliceFor(before, after, caret)).toEqual(expected);
  });

  it("without a caret, settles an ambiguous edit at the end of the run", () => {
    expect(spliceFor("aa", "aaa")).toEqual({ index: 2, remove: 0, insert: "a" });
  });

  it.each([
    // The two emoji share a high surrogate: a code-unit diff would replace
    // only the low half, and a CRDT holding half a character corrupts.
    ["x😀y", "x😁y"],
    ["😀", "😁"],
    ["a😀", "a😁"],
  ])("never starts or ends inside a surrogate pair: %s -> %s", (before, after) => {
    const splice = spliceFor(before, after);
    expect(splice).not.toBeNull();
    const { index, remove } = splice!;
    expect(splitsPair(before, index)).toBe(false);
    expect(splitsPair(before, index + remove)).toBe(false);
    expect(splitsPair(after, index)).toBe(false);
    expect(apply(before, splice)).toBe(after);
  });
});

describe("toCodePointBoundary", () => {
  it("steps back off the middle of a pair and clamps into the text", () => {
    expect(toCodePointBoundary("a😀b", 2)).toBe(1);
    expect(toCodePointBoundary("a😀b", 3)).toBe(3);
    expect(toCodePointBoundary("abc", -4)).toBe(0);
    expect(toCodePointBoundary("abc", 99)).toBe(3);
  });
});
