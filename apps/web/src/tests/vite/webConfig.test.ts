// @vitest-environment node
// Which html the web config builds, from which root, for the open app and for
// a package that owns the VS Code webview.
import { resolve } from "node:path";
import type { UserConfig } from "vite";
import { describe, expect, it } from "vitest";

import { defineWebConfig, type WebConfigOptions } from "../../../webConfig";

const APP = resolve(process.cwd());
const PRODUCT = "/checkout/apps/web";

function built(options: WebConfigOptions, mode: string): UserConfig {
  return defineWebConfig(options)({ mode, command: "build" });
}

function entries(config: UserConfig): Record<string, string> {
  return config.build?.rolldownOptions?.input as Record<string, string>;
}

function aliasFor(config: UserConfig, find: string): string | undefined {
  const aliases = (config.resolve?.alias ?? []) as { find: string | RegExp; replacement: string }[];
  return aliases.find((alias) => alias.find === find)?.replacement;
}

describe("the open app with no webview", () => {
  const options = { packageDir: APP };

  it("builds only the portal", () => {
    expect(entries(built(options, "production"))).toEqual({ index: resolve(APP, "index.html") });
    expect(aliasFor(built(options, "production"), "@webview")).toBeUndefined();
  });

  it("refuses the webview mode instead of building nothing", () => {
    expect(() => built(options, "vscode")).toThrow(/has none/);
  });
});

describe("a webview beside the portal", () => {
  const options = { packageDir: APP, webview: { dir: APP } };

  it("builds both from the app's root, and the webview alone in its mode", () => {
    const app = built(options, "production");
    expect(app.root).toBe(APP);
    expect(entries(app)).toEqual({ index: resolve(APP, "index.html"), vscode: resolve(APP, "vscode.html") });
    const webview = built(options, "vscode");
    expect(entries(webview)).toEqual({ vscode: resolve(APP, "vscode.html") });
    expect(webview.build?.outDir).toBe(resolve(APP, "dist-vscode"));
  });
});

describe("a webview in another package", () => {
  const options = { packageDir: PRODUCT, webview: { dir: PRODUCT } };

  it("builds the portal from the open app's root and the webview from its own", () => {
    const app = built(options, "production");
    expect(app.root).toBe(APP);
    expect(entries(app)).toEqual({ index: resolve(APP, "index.html") });
    expect(app.build?.outDir).toBe(resolve(PRODUCT, "dist"));

    const webview = built(options, "vscode");
    expect(webview.root).toBe(PRODUCT);
    expect(entries(webview)).toEqual({ vscode: resolve(PRODUCT, "vscode.html") });
    expect(webview.publicDir).toBe(resolve(APP, "public"));
    // The webview is reached as `@/webview/…`, through the package that has it.
    expect(aliasFor(webview, "@webview")).toBeUndefined();
  });
});

describe("an overlay's module aliases", () => {
  const lineage = resolve(PRODUCT, "ui-private/src/lineage/index.ts");
  const options = {
    packageDir: PRODUCT,
    overlay: { src: resolve(PRODUCT, "src"), aliases: [{ find: /^@alkera\/ui\/lineage$/, replacement: lineage }] },
  };

  it("answer the names the product's pages import, ahead of the open package", () => {
    const aliases = (built(options, "production").resolve?.alias ?? []) as { find: string | RegExp; replacement: string }[];
    const match = aliases.find((alias) => alias.find instanceof RegExp && alias.find.test("@alkera/ui/lineage"));
    expect(match?.replacement).toBe(lineage);
    expect(aliases.some((alias) => alias.find instanceof RegExp && alias.find.test("@alkera/ui/lineage/x"))).toBe(false);
  });

  it("are none for the open app", () => {
    const aliases = (built({ packageDir: APP }, "production").resolve?.alias ?? []) as { find: string | RegExp; replacement: string }[];
    expect(aliases.some((alias) => alias.find instanceof RegExp && alias.find.test("@alkera/ui/lineage"))).toBe(false);
  });
});
