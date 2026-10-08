// @vitest-environment node
// The overlay through a real Vite build of a small two-tree fixture: what links,
// what is refused, and what the product modules contribute.
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { build, type Plugin, type Rolldown } from "vite";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { overlayCollisions, productExtensions, sourceOverlay } from "@/vite/sourceOverlay";

let root: string;
let openSrc: string;
let overlaySrc: string;

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "source-overlay-"));
  openSrc = join(root, "open/src");
  overlaySrc = join(root, "product/src");
  write(openSrc, "a.ts", 'export const a = "open-a";');
  write(openSrc, "shared/util.ts", 'export const util = "open-util";');
  write(openSrc, "pages/home.ts", 'export const home = "open-home";');
  write(overlaySrc, "pages/billing/y.ts", 'export const y = "own-y";');
});
afterEach(() => rmSync(root, { recursive: true, force: true }));

function write(base: string, path: string, text: string): string {
  const full = join(base, path);
  mkdirSync(join(full, ".."), { recursive: true });
  writeFileSync(full, text);
  return full;
}

async function bundle(entry: string, plugins: Plugin[]): Promise<string> {
  const result = await build({
    configFile: false,
    root,
    logLevel: "silent",
    plugins,
    build: { write: false, minify: false, lib: { entry, formats: ["es"], fileName: "out" } },
  });
  const outputs = (Array.isArray(result) ? result : [result]) as Rolldown.RolldownOutput[];
  return outputs.flatMap((output) => output.output.map((chunk) => ("code" in chunk ? chunk.code : ""))).join("\n");
}

const overlay = (): Plugin => sourceOverlay({ openSrc, overlaySrc, alias: "@/" });

describe("an overlay file", () => {
  it("reaches open modules by alias and by a relative path that leaves its own files", async () => {
    const entry = write(
      overlaySrc,
      "pages/billing/x.ts",
      [
        'import { a } from "@/a";',
        'import { util } from "../../shared/util";',
        'import { home } from "../home";',
        'import { y } from "./y";',
        'import { y as again } from "@/pages/billing/y";',
        "export const x = [a, util, home, y, again].join();",
      ].join("\n"),
    );
    const code = await bundle(entry, [overlay()]);
    for (const text of ["open-a", "open-util", "open-home", "own-y"]) expect(code).toContain(text);
  });

  it("does not resolve a module neither tree holds", async () => {
    const entry = write(overlaySrc, "pages/billing/x.ts", 'import { gone } from "@/gone"; export const x = gone;');
    await expect(bundle(entry, [overlay()])).rejects.toThrow(/gone/);
  });
});

describe("an open file", () => {
  it("never reaches the overlay, by alias or by relative path", async () => {
    const byAlias = write(openSrc, "entryAlias.ts", 'import { y } from "@/pages/billing/y"; export const e = y;');
    await expect(bundle(byAlias, [overlay()])).rejects.toThrow(/pages\/billing\/y/);
    const byPath = write(openSrc, "entryPath.ts", 'import { y } from "./pages/billing/y"; export const e = y;');
    await expect(bundle(byPath, [overlay()])).rejects.toThrow(/pages\/billing\/y/);
  });

  it("still resolves its own modules by alias", async () => {
    const entry = write(openSrc, "entry.ts", 'import { a } from "@/a"; export const e = a;');
    expect(await bundle(entry, [overlay()])).toContain("open-a");
  });
});

describe("a library layer", () => {
  it("maps relative imports into the open library, and leaves @/ to whoever owns it", async () => {
    const entry = write(
      overlaySrc,
      "connections/card.ts",
      'import { util } from "../shared/util"; import { a } from "@/a"; export const card = [util, a].join();',
    );
    const library = sourceOverlay({ openSrc, overlaySrc });
    await expect(bundle(entry, [library])).rejects.toThrow(/@\/a/);
    const code = await bundle(entry, [library, { name: "app-alias", resolveId: (id) => (id === "@/a" ? join(openSrc, "a.ts") : null) }]);
    expect(code).toContain("open-util");
    expect(code).toContain("open-a");
  });
});

describe("a path in both trees", () => {
  it("is refused before anything is built", async () => {
    write(overlaySrc, "shared/util.ts", 'export const util = "own-util";');
    expect(overlayCollisions({ openSrc, overlaySrc })).toEqual([join("shared", "util.ts")]);
    const entry = write(overlaySrc, "pages/billing/x.ts", 'import { util } from "../../shared/util"; export const x = util;');
    await expect(bundle(entry, [overlay()])).rejects.toThrow(/shadows open source files: shared.util\.ts/);
  });
});

describe("the product extensions", () => {
  const ENTRY = 'import { PORTAL_EXTENSIONS } from "virtual:alkera-web-product"; console.log("boots", PORTAL_EXTENSIONS.map((e) => e.id));';

  it("are the list the product's portal module exports", async () => {
    const portal = write(overlaySrc, "product/portal.ts", 'export const PORTAL_EXTENSIONS = [{ id: "billing-ext" }];');
    const code = await bundle(write(openSrc, "main.ts", ENTRY), [productExtensions(portal)]);
    expect(code).toContain("billing-ext");
  });

  it("are an empty list for the open app", async () => {
    const code = await bundle(write(openSrc, "main.ts", ENTRY), [productExtensions()]);
    expect(code).toContain("boots");
    expect(code).not.toContain("billing-ext");
  });
});
