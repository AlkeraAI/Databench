/**
 * The Files page's own hygiene, asserted on the source rather than on prose.
 *
 * The definition of done for the page names four things a reviewer would
 * otherwise have to eyeball on every change: no `any`, no query key spelled
 * inline instead of in `api/keys.ts`, no Tailwind, no third-party component
 * framework. Each is a real scan of every shipped page file — introducing any
 * one of them turns the matching case red and names the file and line.
 */

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/** `src/<rel>`, found from wherever vitest was started. */
function sourceDir(rel: string): string {
  const candidates = [join(process.cwd(), "src", rel), join(process.cwd(), "apps/web/src", rel)];
  const found = candidates.find((candidate) => existsSync(candidate));
  if (found === undefined) throw new Error(`Files sources not found: ${candidates.join(", ")}`);
  return found;
}

/** The page, and the files domain library it is built on. */
const PAGE_DIR = sourceDir("pages/workspace/files");
const LIB_DIR = sourceDir("lib/files");

interface SourceFile {
  readonly name: string;
  readonly text: string;
}

function collect(dir: string, prefix = ""): SourceFile[] {
  const out: SourceFile[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const rel = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) {
      out.push(...collect(join(dir, entry.name), rel));
      continue;
    }
    if (!/\.tsx?$/.test(entry.name)) continue;
    out.push({ name: rel, text: readFileSync(join(dir, entry.name), "utf8") });
  }
  return out;
}

const SOURCES = [...collect(PAGE_DIR), ...collect(LIB_DIR, "lib/files")];

interface CodeLine {
  readonly line: number;
  readonly text: string;
}

/** Every line with its `//` and block-comment bodies removed, so prose never trips a scan. */
function codeLines(file: SourceFile): CodeLine[] {
  const lines: CodeLine[] = [];
  let inBlock = false;
  file.text.split("\n").forEach((raw, index) => {
    let text = raw;
    if (inBlock) {
      const close = text.indexOf("*/");
      if (close === -1) {
        lines.push({ line: index + 1, text: "" });
        return;
      }
      text = text.slice(close + 2);
      inBlock = false;
    }
    const open = text.indexOf("/*");
    if (open !== -1) {
      const close = text.indexOf("*/", open + 2);
      if (close === -1) {
        inBlock = true;
        text = text.slice(0, open);
      } else {
        text = text.slice(0, open) + text.slice(close + 2);
      }
    }
    const slash = text.indexOf("//");
    if (slash !== -1) text = text.slice(0, slash);
    lines.push({ line: index + 1, text });
  });
  return lines;
}

function hits(pattern: RegExp): string[] {
  const found: string[] = [];
  for (const file of SOURCES) {
    for (const { line, text } of codeLines(file)) {
      if (pattern.test(text)) found.push(`${file.name}:${line}: ${text.trim()}`);
    }
  }
  return found;
}

interface ImportSite {
  readonly where: string;
  readonly specifier: string;
}

/** Bare module specifiers, in either quote style, with their file and line. */
function imports(): ImportSite[] {
  const out: ImportSite[] = [];
  for (const file of SOURCES) {
    for (const { line, text } of codeLines(file)) {
      const match = /(?:from|import)\s+["']([^"']+)["']/.exec(text);
      const specifier = match?.[1];
      if (specifier !== undefined) out.push({ where: `${file.name}:${line}`, specifier });
    }
  }
  return out;
}

describe("the Files page's definition of done", () => {
  it("ships the source the scans below read, so a green run is never an empty one", () => {
    expect(SOURCES.length).toBeGreaterThan(20);
    const names = SOURCES.map((file) => file.name);
    expect(names).toContain("FilesPage.tsx");
    expect(names).toContain("state/shortcuts.ts");
  });

  it("uses no `any`, in a type position or an assertion", () => {
    expect(hits(/(?::\s*any\b|\bas\s+any\b|<any[,>]|\bany\[\]|Record<[^>]*\bany\b)/)).toEqual([]);
  });

  it("spells no query key inline — every key comes from api/keys.ts", () => {
    expect(hits(/\[\s*["'](?:files|drives|nodes)["']/)).toEqual([]);
    // The scan above would pass vacuously if the page stopped reading the
    // cache at all, so pin that the keys module is still where keys come from.
    const readsCache = SOURCES.some((file) => /queryKey|invalidates|QueryData/.test(file.text));
    expect(readsCache).toBe(true);
    expect(imports().some((site) => site.specifier === "@/api/keys")).toBe(true);
  });

  it("carries no Tailwind — neither the utility classes nor the toolchain", () => {
    expect(hits(/\btailwind/i)).toEqual([]);
    expect(
      hits(
        /className=\{?["'`][^"'`]*\b(?:p|m|px|py|mx|my|mt|mb|ml|mr|pt|pb|pl|pr|w|h|gap|text|bg|border|rounded|flex|grid|space|justify|items)-(?:x-|y-)?(?:\d+|px|full|auto|screen|xs|sm|md|lg|xl|center|between)\b/,
      ),
    ).toEqual([]);
  });

  it("imports no third-party component framework and writes no CSS-in-JS", () => {
    const allowed = (specifier: string): boolean =>
      specifier.startsWith(".") ||
      specifier.startsWith("@/") ||
      specifier.startsWith("@alkera/") ||
      specifier.startsWith("@tanstack/") ||
      specifier.startsWith("node:") ||
      specifier === "react" ||
      specifier === "react-dom" ||
      specifier === "react-router-dom" ||
      specifier.endsWith(".css");
    expect(imports().filter((site) => !allowed(site.specifier))).toEqual([]);
    expect(hits(/\bstyled[.(]|\bcss`|makeStyles|@emotion/)).toEqual([]);
  });

  it("never reaches the new-tab fallback for a row the preview can draw", () => {
    // Opening a file is decided in one place, and the order of the two branches
    // is the whole rule: a row the registry draws opens in the modal over the
    // listing, and only what nothing draws falls through to a tab of its own.
    // Inverted, every deliverable an agent writes would leave the page.
    const page = SOURCES.find((file) => file.name === "FilesPage.tsx");
    expect(page).toBeDefined();
    const body = (page as SourceFile).text;
    const from = body.indexOf("const openItem = useCallback(");
    const to = body.indexOf("const openObjectPage", from);
    expect(from).toBeGreaterThan(-1);
    expect(to).toBeGreaterThan(from);
    const dispatch = body.slice(from, to);
    const drawable = dispatch.indexOf("isPreviewable(");
    const newTab = dispatch.indexOf("window.open(");
    expect(drawable).toBeGreaterThan(-1);
    expect(newTab === -1 || drawable < newTab).toBe(true);
  });

  it("draws its icons from the shipped set, never from emoji", () => {
    expect(hits(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u)).toEqual([]);
  });
});
