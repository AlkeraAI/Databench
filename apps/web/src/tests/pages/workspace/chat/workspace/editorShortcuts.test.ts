// The editor's keyboard, as a table: what each chord does on each platform, and
// the chords that must do nothing.

import { describe, expect, it } from "vitest";

import { editorCommandFor, type KeyFacts } from "@/pages/workspace/chat/workspace/editorShortcuts";

const key = (over: Partial<KeyFacts> & { key: string }): KeyFacts => ({
  metaKey: false,
  ctrlKey: false,
  shiftKey: false,
  altKey: false,
  ...over,
});

describe("on a Mac", () => {
  it.each([
    [key({ key: "\\", metaKey: true }), { kind: "split" }],
    [key({ key: "w", metaKey: true }), { kind: "close" }],
    [key({ key: "V", metaKey: true, shiftKey: true }), { kind: "toggle-preview" }],
    [key({ key: "1", metaKey: true }), { kind: "focus-group", index: 0 }],
    [key({ key: "4", metaKey: true }), { kind: "focus-group", index: 3 }],
    // A layout that types something else on the key still has its code.
    [key({ key: "«", code: "Backslash", metaKey: true }), { kind: "split" }],
  ])("%j is %j", (event, command) => {
    expect(editorCommandFor(event, "mac")).toEqual(command);
  });

  it.each([
    ["Ctrl+W is the editor's own word delete", key({ key: "w", ctrlKey: true })],
    ["Cmd+Ctrl+W is no chord of ours", key({ key: "w", metaKey: true, ctrlKey: true })],
    ["Cmd+Alt+\\ is the editor's", key({ key: "\\", metaKey: true, altKey: true })],
    ["Cmd+Shift+W is not close", key({ key: "w", metaKey: true, shiftKey: true })],
    ["Cmd+V is paste", key({ key: "v", metaKey: true })],
    ["Cmd+5 is past the last group", key({ key: "5", metaKey: true })],
    ["a bare backslash is typing", key({ key: "\\" })],
  ])("%s", (_label, event) => {
    expect(editorCommandFor(event, "mac")).toBeNull();
  });
});

describe("elsewhere", () => {
  it("answers to Ctrl, and not to the Windows key", () => {
    expect(editorCommandFor(key({ key: "\\", ctrlKey: true }), "other")).toEqual({ kind: "split" });
    expect(editorCommandFor(key({ key: "2", ctrlKey: true }), "other")).toEqual({ kind: "focus-group", index: 1 });
    expect(editorCommandFor(key({ key: "\\", metaKey: true }), "other")).toBeNull();
  });
});
