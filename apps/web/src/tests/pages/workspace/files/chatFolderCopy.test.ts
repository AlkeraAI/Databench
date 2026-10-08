import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// A person browsing a chat's files is standing in the chat's working
// directory, and the page dresses that node as the chat: its title on the
// trail, its notice above the rows. The directory's own name is the box's
// business and must never reach a person — not in a label, a notice, a menu
// entry or a dialog title. So no string a Files surface could render is allowed
// to spell it; the one place the name may appear in this code is a test that
// sets it up as the SERVER's row, to prove the page hides it.

/** The Files page, and the files domain library whose labels and error copy
 *  it renders. */
const FILES_UI = [
  join(__dirname, "../../../../pages/workspace/files"),
  join(__dirname, "../../../../lib/files"),
];

/** Every source file of the Files UI, with its comments stripped so only what
 *  could reach the screen — string literals, JSX text — is judged. */
function renderableSources(): Array<[string, string]> {
  return FILES_UI.flatMap((dir) => readdirSync(dir).map((name) => [dir, name] as const))
    .filter(([, name]) => /\.(ts|tsx)$/.test(name))
    .map(([dir, name]) => {
      const raw = readFileSync(join(dir, name), "utf8");
      const stripped = raw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
      return [name, stripped];
    });
}

describe("what the Files UI may say about a chat's files", () => {
  it("never spells the chat's working directory by name", () => {
    const leaks = renderableSources().filter(([, source]) => /scratch/i.test(source));
    expect(leaks.map(([name]) => name)).toEqual([]);
  });

  it("reads a directory of sources at all, so an empty answer is a real one", () => {
    const names = renderableSources().map(([name]) => name);
    expect(names).toContain("FilesPage.tsx");
    expect(names).toContain("chatFolder.ts");
  });
});
