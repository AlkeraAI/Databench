// Builds dist/alkera-widgets.js: one classic IIFE the output frame injects as
// an inline script, which registers itself through `window.__alkRegister`
// (src/frame-entry.ts), and dist/manifest.json naming its content hash, which
// is how the asset store and the frame address it.
import { build } from "esbuild";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const pkg = JSON.parse(readFileSync(join(here, "package.json"), "utf8"));
const dist = join(here, "dist");
mkdirSync(dist, { recursive: true });

// The controls stylesheet with its @imports resolved and any fonts or images
// inlined as data URLs (the frame's CSP allows only data: for both).
const controlsCss = require.resolve("@jupyter-widgets/controls/css/widgets.css");
const css = await build({
  entryPoints: [controlsCss],
  bundle: true,
  write: false,
  minify: true,
  loader: { ".svg": "dataurl", ".woff": "dataurl", ".woff2": "dataurl", ".png": "dataurl" },
});
const cssText = css.outputFiles[0].text;

const frameOnly = {
  name: "frame-only",
  setup(b) {
    b.onResolve({ filter: /^virtual:widgets-css$/ }, () => ({ path: "widgets-css", namespace: "virtual" }));
    b.onLoad({ filter: /.*/, namespace: "virtual" }, () => ({
      contents: `export default ${JSON.stringify(cssText)};`,
      loader: "js",
    }));
    // base-manager sanitizes widget descriptions with sanitize-html (postcss,
    // htmlparser2); inside the frame a DOM-based sanitizer does the same job.
    b.onResolve({ filter: /^sanitize-html$/ }, () => ({ path: join(here, "src/sanitize.ts") }));
  },
};

const outfile = join(dist, "alkera-widgets.js");
const result = await build({
  entryPoints: [join(here, "src/frame-entry.ts")],
  bundle: true,
  format: "iife",
  platform: "browser",
  target: "es2020",
  minify: true,
  metafile: true,
  legalComments: "none",
  outfile,
  define: {
    "process.env.NODE_ENV": '"production"',
    global: "globalThis",
    __ALK_WIDGETS_VERSION__: JSON.stringify(pkg.version),
  },
  plugins: [frameOnly],
  logLevel: "warning",
});

const bytes = readFileSync(outfile);
const sha256 = createHash("sha256").update(bytes).digest("hex");
const text = bytes.toString("utf8");
const manifest = {
  name: "@alkera/widgets",
  version: pkg.version,
  file: "alkera-widgets.js",
  sha256,
  bytes: bytes.length,
  // Modules this bundle carries; the asset store never accepts one of these
  // names from a notebook environment.
  provides: [
    "@alkera/widgets",
    "@alkera/ui-widgets",
    "@jupyter-widgets/base",
    "@jupyter-widgets/controls",
    "@jupyter-widgets/output",
    "anywidget",
  ],
  // Grep counts, not proof of execution: underscore's _.template keeps one
  // `new Function` that nothing on a widget path calls.
  eval_sites: {
    new_Function: (text.match(/new Function\(/g) || []).length,
    eval: (text.match(/[^.\w]eval\(/g) || []).length,
  },
};
writeFileSync(join(dist, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
writeFileSync(join(dist, "meta.json"), JSON.stringify(result.metafile));
console.log(JSON.stringify({ ...manifest, gzip: (await import("node:zlib")).gzipSync(bytes, { level: 9 }).length }));
