// The editor's keyboard, the way an editor spells it.
//
//   Cmd/Ctrl+\         split the tab in front to the right
//   Cmd/Ctrl+W         close the tab in front
//   Cmd/Ctrl+Shift+V   switch the tab in front between its source and its preview
//   Cmd/Ctrl+1..4      put the reader in group 1..4
//
// The accelerator is the platform's own (Cmd on a Mac, Ctrl elsewhere) and the
// other one must be up, so Ctrl+W on a Mac is still the terminal's word-delete
// in an editor and never closes a tab. Pure, so the whole table is a test.

import type { Platform } from "@/lib/platform";

export type EditorCommand =
  | { kind: "split" }
  | { kind: "close" }
  | { kind: "toggle-preview" }
  | { kind: "focus-group"; index: number };

export interface KeyFacts {
  key: string;
  code?: string;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
}

export function editorCommandFor(event: KeyFacts, platform: Platform): EditorCommand | null {
  const accel = platform === "mac" ? event.metaKey : event.ctrlKey;
  const other = platform === "mac" ? event.ctrlKey : event.metaKey;
  if (!accel || other || event.altKey) return null;
  const key = event.key.toLowerCase();
  if (event.shiftKey) {
    return key === "v" || event.code === "KeyV" ? { kind: "toggle-preview" } : null;
  }
  if (key === "\\" || event.code === "Backslash") return { kind: "split" };
  if (key === "w" || event.code === "KeyW") return { kind: "close" };
  const digit = /^[1-4]$/.test(key) ? Number(key) : /^Digit[1-4]$/.test(event.code ?? "") ? Number(event.code?.slice(5)) : 0;
  if (digit > 0) return { kind: "focus-group", index: digit - 1 };
  return null;
}
