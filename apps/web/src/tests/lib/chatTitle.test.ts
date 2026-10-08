// A chat the server has not titled yet reads "Untitled chat", never a blank.

import { describe, expect, it } from "vitest";

import { chatTitle } from "@/lib/chatTitle";

describe("chatTitle", () => {
  it.each([
    ["a titled chat keeps its title", { title: "Q3 review" }, "Q3 review"],
    ["a null title", { title: null }, "Untitled chat"],
    ["an empty title", { title: "" }, "Untitled chat"],
    ["no title field at all", {}, "Untitled chat"],
  ])("%s", (_label, chat, expected) => {
    expect(chatTitle(chat)).toBe(expected);
  });
});
