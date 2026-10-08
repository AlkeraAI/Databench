import { renderHook } from "@testing-library/react";
import { installExtensions } from "@alkera/ui/extensions";
import { describe, expect, it } from "vitest";

import { CHAT_MENU_ACTIONS, useChatMenuRows } from "@/app/extensions/portal";

// The chat header's overflow menu takes rows from installed extensions. Each entry's hook runs
// for the open chat, in registration order, and says whether it has a row for that chat.

installExtensions([
  {
    name: "test.chat_menu",
    install() {
      CHAT_MENU_ACTIONS.register({
        key: "first",
        useRow: (chatId) => ({
          action: chatId ? { id: "first", label: `First for ${chatId}`, onSelect: () => {} } : null,
          dialog: null,
        }),
      });
      CHAT_MENU_ACTIONS.register({ key: "second", useRow: () => ({ action: null, dialog: "a dialog" }) });
    },
  },
]);

describe("useChatMenuRows", () => {
  it("asks every entry about the open chat, in registration order", () => {
    const { result } = renderHook(() => useChatMenuRows("c1"));
    expect(result.current.map((row) => [row.key, row.action?.label ?? null, row.dialog])).toEqual([
      ["first", "First for c1", null],
      ["second", null, "a dialog"],
    ]);
  });

  it("hands a shell with no chat to offer rows for a null id", () => {
    const { result } = renderHook(() => useChatMenuRows(null));
    expect(result.current.map((row) => row.action)).toEqual([null, null]);
  });
});
