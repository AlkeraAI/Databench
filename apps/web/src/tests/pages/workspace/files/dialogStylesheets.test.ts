// The files dialogs a chat can open — Versions, Move to… / Copy to…, Save as
// template — each bring their own stylesheet. The chat header, the chat rail and
// a chat's file and files tabs are lazy chunks that never load the files
// browser's sheet, so a dialog whose rules live there renders from a chat as raw
// browser controls. The share dialog's own case lives in shareDialog.test.tsx.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

// Whether loading each dialog's module loads its sheet: each factory runs only
// when something imports that sheet, so removing the import fails its case.
const loaded = vi.hoisted(() => ({ versions: false, picker: false, template: false }));
vi.mock("@/pages/workspace/files/file-versions-dialog.css", () => {
  loaded.versions = true;
  return {};
});
vi.mock("@/pages/workspace/files/move-to-dialog.css", () => {
  loaded.picker = true;
  return {};
});
vi.mock("@/pages/workspace/files/save-as-template-dialog.css", () => {
  loaded.template = true;
  return {};
});

// Importing the modules is the act under test; nothing else is needed from them.
import "@/pages/workspace/files/FileVersionsDialog";
import "@/pages/workspace/files/MoveToDialog";
import "@/pages/workspace/files/SaveAsTemplateDialog";

const DIR = join(process.cwd(), "src/pages/workspace/files");
const read = (name: string): string =>
  readFileSync(join(DIR, name), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

const DIALOGS = [
  { key: "versions", source: "FileVersionsDialog.tsx", sheet: "file-versions-dialog.css", prefix: "alk-files-versions" },
  { key: "picker", source: "MoveToDialog.tsx", sheet: "move-to-dialog.css", prefix: "alk-files-picker" },
  { key: "template", source: "SaveAsTemplateDialog.tsx", sheet: "save-as-template-dialog.css", prefix: "alk-files-template" },
] as const;

describe.each(DIALOGS)("$source", ({ key, source, sheet, prefix }) => {
  it("imports its own stylesheet", () => {
    expect(loaded[key]).toBe(true);
  });

  it("leaves none of its rules on the files browser's sheet", () => {
    expect(read("files-page.css")).not.toContain(`.${prefix}`);
  });

  it("has a rule in its own sheet for every class of its own it names", () => {
    const css = read(sheet);
    const named = new Set(
      Array.from(read(source).matchAll(new RegExp(`\\b${prefix}(?:__[a-z-]+)?\\b`, "g")), (m) => m[0]),
    );
    expect(named.size).toBeGreaterThan(1);
    const bare = [...named].filter((name) => !new RegExp(`\\.${name}(?![\\w-])`).test(css));
    expect(bare).toEqual([]);
  });
});
