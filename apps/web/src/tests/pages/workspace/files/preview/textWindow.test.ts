// Where a window of a text file is cut. The hook's own tests cover the common
// cuts end to end; these pin the edges a generated fixture would only hit by
// luck — a carriage-return blank line, a window with no newline at all, a window
// ending inside a multi-byte character, and the ranged answer's header.

import { describe, expect, it } from "vitest";

import {
  cutWindow,
  joinBytes,
  servedRange,
} from "@/pages/workspace/files/preview/textWindow";

const encode = (text: string): Uint8Array => new TextEncoder().encode(text);
const decode = (bytes: Uint8Array): string => new TextDecoder().decode(bytes);

describe("cutting a window", () => {
  it.each([
    ["after the last line", "one\ntwo\nthr", "line", "one\ntwo\n", "thr"],
    ["after the last blank line", "a\nb\n\nc\nd", "block", "a\nb\n\n", "c\nd"],
    ["after a blank line written with carriage returns", "a\r\n\r\nb\r\nc", "block", "a\r\n\r\n", "b\r\nc"],
    ["at the last line when a block window has no blank line", "a\nb\nc", "block", "a\nb\n", "c"],
    ["nowhere when the window ends on a line end", "a\nb\n", "line", "a\nb\n", ""],
  ] as const)("cuts %s", (_name, text, at, take, carry) => {
    const cut = cutWindow(encode(text), { final: false, at });

    expect(decode(cut.take)).toBe(take);
    expect(decode(cut.carry)).toBe(carry);
  });

  it("takes the last window whole, half line and all", () => {
    const cut = cutWindow(encode("a\nb"), { final: true, at: "line" });

    expect(decode(cut.take)).toBe("a\nb");
    expect(cut.carry).toHaveLength(0);
  });

  it.each([
    ["a two-byte character", "é"],
    ["a three-byte character", "€"],
    ["a four-byte character", "𝄞"],
  ])("never splits %s in a window with no newline", (_name, char) => {
    const whole = encode(`long${char}`);
    // The window ends one byte into the character.
    const window = whole.slice(0, 5);

    const cut = cutWindow(window, { final: false, at: "line" });

    expect(decode(cut.take)).toBe("long");
    expect(decode(joinBytes(cut.carry, whole.slice(5)))).toBe(char);
  });

  it("keeps a window of whole characters with no newline whole", () => {
    const cut = cutWindow(encode("longé"), { final: false, at: "line" });

    expect(decode(cut.take)).toBe("longé");
    expect(cut.carry).toHaveLength(0);
  });
});

describe("reading what a ranged answer holds", () => {
  it.each([
    ["bytes 0-1048575/3145728", { start: 0, end: 1048575, total: 3145728 }],
    ["bytes 10-19/*", { start: 10, end: 19, total: null }],
    [null, null],
    ["bytes */3145728", null],
    ["items 0-1/2", null],
    ["bytes 9-3/10", null],
  ])("reads %s", (header, span) => {
    expect(servedRange(header)).toEqual(span);
  });
});
