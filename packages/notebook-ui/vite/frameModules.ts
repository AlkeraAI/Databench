// Frame modules as strings, for the notebook output frame.
//
// The output frame runs under a policy that loads nothing by URL: renderer code
// reaches it as text in a `module` message and is injected as an inline
// script. This plugin builds each module under `src/frame/modules/` into one
// self-contained IIFE with esbuild and exposes it as
// `virtual:nb-frame-module/<name>`, whose default export is that code. The app
// imports them lazily, so a module (Plotly is several megabytes) is its own
// chunk, fetched the first time an output needs it.

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";
import type { Plugin } from "vite";

export const FRAME_MODULE_PREFIX = "virtual:nb-frame-module/";
const RESOLVED_PREFIX = `\0${FRAME_MODULE_PREFIX}`;
const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const MODULES_DIR = resolve(PACKAGE_ROOT, "src/frame/modules");
const NAME = /^nb-[a-z0-9-]+$/;

export interface BuiltFrameModule {
  code: string;
  /** Every source file the module was built from, for watching. */
  inputs: string[];
}

/** The widget manager's frame module: the `@alkera/widgets` package's own
 *  build (its bundle and manifest), made when the app first asks for it. */
export const WIDGETS_FRAME_MODULE = "alkera-widgets";
const WIDGETS_ROOT = resolve(PACKAGE_ROOT, "../widgets");

function buildWidgets(): BuiltFrameModule {
  const bundle = resolve(WIDGETS_ROOT, "dist/alkera-widgets.js");
  if (!existsSync(bundle)) {
    if (!existsSync(resolve(WIDGETS_ROOT, "build.mjs"))) throw new Error("The widget manager package is not in this checkout.");
    execFileSync(process.execPath, [resolve(WIDGETS_ROOT, "build.mjs")], { cwd: WIDGETS_ROOT, stdio: "ignore" });
  }
  return { code: readFileSync(bundle, "utf8"), inputs: [] };
}

/** Builds one frame module (`src/frame/modules/<name>.ts`) to IIFE text. */
export async function buildFrameModule(name: string): Promise<BuiltFrameModule> {
  if (name === WIDGETS_FRAME_MODULE) return buildWidgets();
  const entry = resolve(MODULES_DIR, `${name}.ts`);
  if (!NAME.test(name) || !existsSync(entry)) throw new Error(`There is no frame module named ${name}.`);
  return bundleIife(entry, `The frame module ${name}`);
}

/** Bundles one entry and everything it imports into a single minified IIFE
 *  for the browser. Also how the browser check bundles its stand-in page. */
export async function bundleIife(entry: string, label = entry): Promise<BuiltFrameModule> {
  const result = await build({
    absWorkingDir: PACKAGE_ROOT,
    entryPoints: [entry],
    bundle: true,
    format: "iife",
    platform: "browser",
    target: "es2020",
    minify: true,
    write: false,
    metafile: true,
    legalComments: "none",
    logLevel: "silent",
    define: { "process.env.NODE_ENV": '"production"', global: "globalThis" },
  });
  const output = result.outputFiles[0];
  if (!output) throw new Error(`${label} built to nothing.`);
  return {
    code: output.text,
    inputs: Object.keys(result.metafile.inputs).map((input) => resolve(PACKAGE_ROOT, input)),
  };
}

/** The Vite plugin serving `virtual:nb-frame-module/<name>`. */
export function frameModules(): Plugin {
  const cache = new Map<string, Promise<BuiltFrameModule>>();
  return {
    name: "alkera-nb-frame-modules",
    resolveId(id) {
      return id.startsWith(FRAME_MODULE_PREFIX) ? `${RESOLVED_PREFIX}${id.slice(FRAME_MODULE_PREFIX.length)}` : null;
    },
    async load(id) {
      if (!id.startsWith(RESOLVED_PREFIX)) return null;
      const name = id.slice(RESOLVED_PREFIX.length);
      let pending = cache.get(name);
      if (!pending) {
        pending = buildFrameModule(name);
        cache.set(name, pending);
      }
      let built: BuiltFrameModule;
      try {
        built = await pending;
      } catch (error) {
        cache.delete(name);
        throw error;
      }
      for (const input of built.inputs) {
        if (!input.includes("node_modules")) this.addWatchFile(input);
      }
      return `export default ${JSON.stringify(built.code)};`;
    },
    watchChange() {
      cache.clear();
    },
  };
}
