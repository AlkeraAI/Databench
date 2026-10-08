import { describe, expect, it } from "vitest";

import { titleFrom } from "@/pages/workspace/chat/data/chatTitle";

describe("the title a chat is given from its first message", () => {
  it("capitalises an opening word that is plainly prose", () => {
    expect(titleFrom("reply with the single word ok")).toBe("Reply with the single word ok");
    expect(titleFrom("  fix   the   spacing ")).toBe("Fix the spacing");
  });

  it("capitalises prose that is not written in ASCII", () => {
    expect(titleFrom("étude sur les chats")).toBe("Étude sur les chats");
    expect(titleFrom("über den wolken")).toBe("Über den wolken");
    expect(titleFrom("naïve approach to caching")).toBe("Naïve approach to caching");
    expect(titleFrom("привет мир")).toBe("Привет мир");
  });

  it("leaves a script with no capitals alone", () => {
    expect(titleFrom("日本語のテスト")).toBe("日本語のテスト");
  });

  it("leaves a name the reader typed exactly as they typed it", () => {
    for (const typed of [
      "qa-c2-alpha: reply with the single word ok",
      "iPhone battery drain in the logs",
      "eBay listing importer is failing",
      "main.py raises on an empty file",
      "--dry-run does nothing",
      "src/api/keys.ts needs an entry",
      "v2 of the migration",
      "_internal helper is exported",
    ]) {
      expect(titleFrom(typed)).toBe(typed);
    }
  });

  it("still capitalises a word carrying its sentence's punctuation", () => {
    expect(titleFrom("hello, is anyone there")).toBe("Hello, is anyone there");
    expect(titleFrom("why: it keeps failing")).toBe("Why: it keeps failing");
  });

  it("has nothing to title when the message is only space", () => {
    expect(titleFrom("   ")).toBeNull();
    expect(titleFrom("")).toBeNull();
  });

  it("cuts a long message at a word boundary", () => {
    const long = titleFrom(`please ${"investigate the failing build ".repeat(5)}`);
    expect(long).toMatch(/^Please /);
    expect(long?.endsWith("…")).toBe(true);
    expect(long?.length).toBeLessThanOrEqual(71);
  });

  it("cuts a long message with no word boundary rather than growing one", () => {
    const long = titleFrom("x".repeat(200));
    expect(long).toHaveLength(71);
    expect(long?.endsWith("…")).toBe(true);
  });
});
