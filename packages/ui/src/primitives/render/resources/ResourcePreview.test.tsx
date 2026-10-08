import { cleanup, render } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { LanguageIcon } from "./ResourcePreview";

// A file's icon tells the reader what the file is, so every recognized type must draw its own symbol
// and the plain Document symbol stays reserved for types the sprite has no icon for. Every glyph
// comes out of one generated spritesheet, so the symbol id in `<use href="...#Symbol">` is the only
// thing that distinguishes two icons on screen.

const DOCUMENT = "Document";

function wrapperOf(ui: ReactElement): HTMLElement {
  const { container } = render(ui);
  const wrapper = container.querySelector<HTMLElement>(".alk-language-icon");
  if (!wrapper) throw new Error("LanguageIcon rendered no wrapper element");
  return wrapper;
}

/** The sprite symbol the icon points at, or null when no sprite svg rendered at all. */
function symbolOf(ui: ReactElement): string | null {
  const href = wrapperOf(ui).querySelector("use")?.getAttribute("href");
  if (!href) return null;
  const hash = href.lastIndexOf("#");
  if (hash === -1) throw new Error(`use href names no sprite symbol: ${href}`);
  return href.slice(hash + 1);
}

afterEach(cleanup);

describe("LanguageIcon", () => {
  it.each([
    // by extension
    { props: { path: "etl/load_orders.py" }, symbol: "Python" },
    { props: { path: "queries/monthly_revenue.sql" }, symbol: "Database" },
    { props: { path: "guide/overview.md" }, symbol: "Markdown" },
    { props: { path: "src/client.ts" }, symbol: "Typescript" },
    { props: { path: "scripts/deploy.sh" }, symbol: "Console" },
    { props: { path: "src/settings.json" }, symbol: "Json" },
    // a bare language token maps the same way
    { props: { language: "python" }, symbol: "Python" },
    { props: { language: "sql" }, symbol: "Database" },
    { props: { language: "markdown" }, symbol: "Markdown" },
    { props: { language: "typescript" }, symbol: "Typescript" },
    { props: { language: "shell" }, symbol: "Console" },
    { props: { language: "bash" }, symbol: "Console" },
    // the file name wins over its extension
    { props: { path: "README.md" }, symbol: "Readme" },
    { props: { path: "guide/README.md" }, symbol: "Readme" },
    { props: { path: "Makefile" }, symbol: "Makefile" },
    { props: { path: "package.json" }, symbol: "Nodejs" },
    { props: { path: "tsconfig.json" }, symbol: "Tsconfig" },
    { props: { path: "vite.config.ts" }, symbol: "Vite" },
    // a dotted suffix matches longest-first, so a test or declaration file keeps its own icon
    { props: { path: "src/chatSurface.test.ts" }, symbol: "TestTs" },
    { props: { path: "src/types.d.ts" }, symbol: "TypescriptDef" },
    // case is ignored
    { props: { path: "REPORT.SQL" }, symbol: "Database" },
    { props: { path: "Analysis/Load_Orders.PY" }, symbol: "Python" },
    { props: { path: "MAKEFILE" }, symbol: "Makefile" },
    { props: { path: "Guide/Readme.MD" }, symbol: "Readme" },
    { props: { path: "PYTHON" }, symbol: "Python" },
    // the directories above the file are ignored, on either separator
    { props: { path: "dbt/tests/test_revenue_rollup_refunds.py" }, symbol: "Python" },
    { props: { path: "python/notebooks/summary.sql" }, symbol: "Database" },
    { props: { path: "release.v2/notes.md" }, symbol: "Markdown" },
    { props: { path: "src\\lib\\client.ts" }, symbol: "Typescript" },
    // the sprite ships no symbol for these, so Document is the honest answer over a wrong glyph
    { props: { path: "exports/rows.csv" }, symbol: DOCUMENT },
    { props: { path: "exports/rows.tsv" }, symbol: DOCUMENT },
    { props: { path: "notes.txt" }, symbol: DOCUMENT },
    { props: { path: "app/Widget.vue" }, symbol: DOCUMENT },
    { props: { path: "archive.zzz" }, symbol: DOCUMENT },
    { props: {}, symbol: DOCUMENT },
    // A directory reaches the map by the same door as a file name, and a folder drawn with
    // the document glyph is indistinguishable from the files beside it in a tree.
    { props: { path: "folder" }, symbol: "Folder" },
    { props: { language: "folder" }, symbol: "Folder" },
    { props: { path: "Folder" }, symbol: "Folder" },
    // The negative twin: only the bare hint means a directory. A file that happens to be
    // called "folder.md" is still Markdown.
    { props: { path: "folder.md" }, symbol: "Markdown" },
    { props: { path: "folders" }, symbol: DOCUMENT },
  ])("draws $symbol for $props", ({ props, symbol }) => {
    expect(symbolOf(<LanguageIcon {...props} />)).toBe(symbol);
  });

  it("uses the caller's fallback only when nothing matches", () => {
    const fallback = <span data-testid="caller-fallback">zzz</span>;
    const unmatched = wrapperOf(<LanguageIcon path="archive.zzz" fallback={fallback} />);
    expect(unmatched.querySelector("[data-testid='caller-fallback']")).not.toBeNull();
    expect(unmatched.querySelector("svg")).toBeNull();

    cleanup();
    const matched = wrapperOf(<LanguageIcon path="etl/load_orders.py" fallback={fallback} />);
    expect(matched.querySelector("[data-testid='caller-fallback']")).toBeNull();
    expect(matched.querySelector("use")?.getAttribute("href")).toContain("#Python");
  });

  it("hides the wrapper from assistive tech", () => {
    expect(wrapperOf(<LanguageIcon path="etl/load_orders.py" />)).toHaveAttribute("aria-hidden", "true");
  });
});
