// The chrome around the chat spends tokens that EXIST.
//
// `chat-page.css` and the saved-object sheets were written against an `--alk-*`
// vocabulary (kebab: `--alk-surface-2`, `--alk-border`, `--alk-space-3`) that
// the design system never declared — it spells its tokens `--alkCardBg`,
// `--alkBorder`, `--alkSpace5`. Every one of those `var()`s therefore fell to
// its literal fallback, which was written for a light page: the chat rail's
// divider was `rgba(0,0,0,0.1)` and its selected row `rgba(0,0,0,0.08)`, both
// invisible on the dark shell, and none of it moved when the scheme did.
//
// A misspelled token cannot fail a render, a type-check or a lint — it just
// quietly stops following the theme. So the check is here: every `--alk…` name
// these sheets read must be one the token layer actually declares.
//
// The same bug kept landing in the sheets next door — the files browser, the
// panes the chat opens beside its transcript, and the surfaces they share out
// of the design system — so the gate reads whole directories rather than a
// hand-kept list of files. A directory with no stylesheets in it yet
// contributes nothing, which is what lets the list name a surface before that
// surface is built.

import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";

import { afterAll, describe, expect, it } from "vitest";

const WEB = process.cwd();
const REPO = resolve(WEB, "../..");
const UI_SRC = join(REPO, "packages/ui/src");

/** Every `--alk…` custom property the design system declares, from its own token
 *  file plus the two app-level sheets that extend the same layer. */
function declaredTokens(): Set<string> {
  const sources = [
    join(UI_SRC, "theme/tokens.css"),
    join(UI_SRC, "theme/motion.css"),
    join(WEB, "src/styles/concept-tokens.css"),
    join(WEB, "src/webview/vscode-theme.css"),
  ];
  const names = new Set<string>();
  for (const path of sources) {
    for (const match of readFileSync(path, "utf8").matchAll(/(--alk[A-Za-z0-9-]*)\s*:/g)) {
      names.add(match[1]!);
    }
  }
  return names;
}

/** Every `--alk…` name a sheet READS, labelled with the sheet it was read from
 *  (relative to `root`, so a sheet in `packages/ui` names itself the same way
 *  one in `apps/web` does). */
function readNames(root: string, paths: string[]): [file: string, token: string][] {
  const out: [string, string][] = [];
  for (const path of paths) {
    const css = readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    for (const match of css.matchAll(/var\(\s*(--alk[A-Za-z0-9-]*)/g)) {
      out.push([relative(root, path), match[1]!]);
    }
  }
  return out;
}

/** The sheets the chat page itself owns, named one by one. */
const CHROME_FILES = [
  "apps/web/src/pages/workspace/chat/chat-page.css",
  "apps/web/src/pages/workspace/chat/chat-surface.css",
];

/** Every directory whose stylesheets frame the chat, the files browser, the
 *  panes they open and the surfaces those panes share. */
const CHROME_DIRS = [
  "apps/web/src/pages/workspace/objects",
  "apps/web/src/pages/workspace/files",
  "apps/web/src/pages/workspace/chat/workspace",
  "apps/web/src/pages/workspace/templates",
  "packages/ui/src/preview",
  "packages/ui/src/layout/SplitPane",
  "packages/ui/src/primitives/controls/TabStrip",
];

function sheetsUnder(root: string, dir: string): string[] {
  let entries: string[];
  try {
    entries = readdirSync(join(root, dir));
  } catch {
    return [];
  }
  return entries
    .filter((name) => name.endsWith(".css"))
    .sort()
    .map((name) => join(root, dir, name));
}

function chromeSheets(root: string): string[] {
  return [
    ...CHROME_FILES.map((file) => join(root, file)).filter((path) => existsSync(path)),
    ...CHROME_DIRS.flatMap((dir) => sheetsUnder(root, dir)),
  ];
}

function undeclaredIn(root: string): [file: string, token: string][] {
  const declared = declaredTokens();
  return readNames(root, chromeSheets(root)).filter(([, token]) => !declared.has(token));
}

describe("the chrome around the chat", () => {
  const scratch: string[] = [];
  afterAll(() => {
    for (const dir of scratch) rmSync(dir, { recursive: true, force: true });
  });

  it("names sheets that are really there", () => {
    // A path typo drops a sheet out of the gate and still reads as green.
    for (const file of CHROME_FILES) expect(existsSync(join(REPO, file))).toBe(true);
    const sheets = chromeSheets(REPO).map((path) => relative(REPO, path));
    expect(sheets).toContain("apps/web/src/pages/workspace/files/files-page.css");
    expect(sheets.length).toBeGreaterThan(4);
  });

  it("reads only tokens the design system declares", () => {
    const declared = declaredTokens();
    expect(declared.size).toBeGreaterThan(80);
    expect(undeclaredIn(REPO)).toEqual([]);
  });

  it("catches an undeclared token in any of the directories it gates", () => {
    // The gate earns its run time only if a sheet dropped into one of these
    // directories tomorrow is actually read. Plant one in each, in a throwaway
    // tree with the same shape, and every one must come back named.
    const gated = [
      "apps/web/src/pages/workspace/objects",
      "apps/web/src/pages/workspace/files",
      "apps/web/src/pages/workspace/chat/workspace",
      "apps/web/src/pages/workspace/templates",
      "packages/ui/src/preview",
      "packages/ui/src/layout/SplitPane",
      "packages/ui/src/primitives/controls/TabStrip",
    ];
    const root = mkdtempSync(join(tmpdir(), "alk-token-gate-"));
    scratch.push(root);
    for (const dir of gated) {
      const path = join(root, dir, "planted.css");
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, ".planted { color: var(--alkNope); }\n");
    }
    expect(undeclaredIn(root)).toEqual(gated.map((dir) => [join(dir, "planted.css"), "--alkNope"]));
  });

  it("reads a token in every sheet that paints — none is a page of literals", () => {
    const painting = chromeSheets(REPO).filter((path) =>
      /color|background|border/.test(readFileSync(path, "utf8")),
    );
    const seen = new Set(readNames(REPO, painting).map(([file]) => file));
    expect(painting.map((path) => relative(REPO, path)).filter((path) => !seen.has(path))).toEqual(
      [],
    );
  });

  it("spaces the top-right cluster off the chrome above it", () => {
    // A hard-coded `gap: 10px` with no vertical padding put the faces against
    // one another and the whole row against the machine banner's dashed edge
    // above it. Both steps come from the spacing scale, so the row breathes the
    // way the rest of this chrome does and follows it when the scale moves.
    const css = readFileSync(join(WEB, "src/pages/workspace/chat/chat-page.css"), "utf8");
    const rule = /\.chat-topbar\s*\{([^}]*)\}/.exec(css);
    expect(rule).not.toBeNull();
    const body = rule![1]!;
    expect(/gap:\s*var\(--alkSpace\w+\)/.test(body)).toBe(true);
    expect(/padding(-block)?:\s*var\(--alkSpace\w+\)/.test(body)).toBe(true);
    // A 26px face against a text label centres on nothing by default.
    expect(body).toContain("align-items: center");
    // No bare pixel step survives in the row: it is tokens or nothing.
    expect(/:\s*-?\d+px/.test(body)).toBe(false);
  });

  it("paints the chat rail's selected row with the shell's own selection wash", () => {
    // The rail sits beside the portal's sidebar, so the row that is open reads
    // the way the nav item for the page you are on reads: the brand wash and the
    // brand ink, never a neutral grey that says nothing about where you are.
    const css = readFileSync(join(WEB, "src/pages/workspace/chat/chat-page.css"), "utf8");
    const rule = /\.chat-page__row\[aria-current="page"\]\s*\{([^}]*)\}/.exec(css);
    expect(rule).not.toBeNull();
    expect(rule![1]).toContain("var(--alkBrandSubtle)");
    expect(rule![1]).toContain("var(--alkBrandText)");
  });
});
