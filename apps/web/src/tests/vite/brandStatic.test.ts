// @vitest-environment node
// The brand's static files and page title: the open build serves Databench's, a product
// build serves its own, and neither may shadow a public/ file.
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Plugin } from "vite";
import { describe, expect, it, vi } from "vitest";

import { brandFiles, brandStatic } from "../../vite/brandStatic";
import { OPEN_BRAND } from "../../../webConfig";

function tree(files: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), "brand-"));
  for (const [path, body] of Object.entries(files)) {
    mkdirSync(join(dir, path, ".."), { recursive: true });
    writeFileSync(join(dir, path), body);
  }
  return dir;
}

type Hook = (this: unknown, ...args: unknown[]) => unknown;
const hook = (plugin: Plugin, name: keyof Plugin) => plugin[name] as unknown as Hook;

describe("brandStatic", () => {
  it("emits every brand file at the site root, nested paths kept", () => {
    const staticDir = tree({ "favicon.svg": "<svg/>", "email/wordmark.png": "png" });
    const plugin = brandStatic({ productName: "Acme", staticDir, publicDir: tree({ "theme-boot.js": "" }) });
    const emitted: { fileName: string; source: Buffer }[] = [];
    hook(plugin, "generateBundle").call({ emitFile: (file: { fileName: string; source: Buffer }) => emitted.push(file) });
    expect(emitted.map((file) => [file.fileName, file.source.toString()])).toEqual([
      ["email/wordmark.png", "png"],
      ["favicon.svg", "<svg/>"],
    ]);
  });

  it("titles the page with the brand's product name, escaped", () => {
    const plugin = brandStatic({ productName: "A<b>", staticDir: tree({}), publicDir: tree({}) });
    const html = hook(plugin, "transformIndexHtml")("<head><title></title></head>");
    expect(html).toBe("<head><title>A&lt;b&gt;</title></head>");
  });

  it("refuses a brand file that would shadow a public/ file", () => {
    const plugin = brandStatic({
      productName: "Acme",
      staticDir: tree({ "theme-boot.js": "x", "favicon.svg": "" }),
      publicDir: tree({ "theme-boot.js": "" }),
    });
    const error = vi.fn();
    hook(plugin, "buildStart").call({ error });
    expect(error).toHaveBeenCalledWith("brand files shadow public/ files: theme-boot.js");
  });

  it("serves the open build as Databench, with the icons index.html links", () => {
    expect(OPEN_BRAND.productName).toBe("Databench");
    expect(brandFiles(OPEN_BRAND.staticDir)).toEqual([
      "apple-touch-icon.png",
      "email/databench-wordmark.png",
      "email/databench-wordmark.svg",
      "favicon.ico",
      "favicon.svg",
      "icon-192.png",
      "icon-512.png",
      "site.webmanifest",
    ]);
  });
});
