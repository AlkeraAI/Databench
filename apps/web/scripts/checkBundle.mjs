// What a build may put in front of a reader before they ask for it.
//
// The live-editing Loro runtime and its WebAssembly (megabytes) load only when
// a shared composer or a live file goes live, and the CodeMirror editor only
// with a file tab that edits text. This fails a build that pulls any of them
// into what an entry loads up front (its static import graph), and fails an
// editor (webview) build that ships them at all: the webview has no live editing
// and its CSP does not admit WebAssembly.
//
// Charts are the third heavy half: Vega, Vega-Lite and the expression
// interpreter load with the first chart on screen, in both builds. The chunk
// they land in is also held to the chart bundle check (no package CDN, no code
// loaded from a URL): charts render where there is no network.
//
//   node scripts/checkBundle.mjs dist           # the portal build
//   node scripts/checkBundle.mjs dist-vscode    # the editor's webview build

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { chartBundleProblems } from "../../../packages/ui/scripts/checkChartsBundle.mjs";

/** Module ids that are the heavy half of live editing. */
export const LAZY_ONLY = [
  /loro-crdt/,
  /crdt\/loroRuntime\.ts$/,
  /@codemirror\//,
  /@lezer\//,
  /crdt\/codeMirrorBinding\.ts$/,
  /workspace\/LiveFileEditor\.tsx$/,
];

/** Module ids of the chart runtime: Vega and everything it brings. */
export const CHARTS_LAZY = [/\/vega(-[a-z]+)*\//, /charts\/vegaRuntime\.ts$/];

/** What CodeMirror leaves in any chunk it is bundled into, however the chunk
 *  is named: its base theme's class names. */
export const CODEMIRROR_MARK = /\.cm-editor|\.cm-content/;

/** What Vega leaves in any chunk it is bundled into: its dataflow's errors. */
export const VEGA_MARK = /Unrecognized data set|Unrecognized signal name/;

const lazyOnly = (id) => LAZY_ONLY.some((re) => re.test(id));
const chartsOnly = (id) => CHARTS_LAZY.some((re) => re.test(id));

/** The charts half of a build, for either entry kind: Vega must not be in
 *  what an entry loads up front, and wherever it is, it loads nothing from
 *  elsewhere. */
export function chartProblems(manifest, read = () => "") {
  const problems = [];
  for (const [key, chunk] of Object.entries(manifest)) {
    if (!chunk.isEntry) continue;
    for (const loaded of upfront(manifest, key)) {
      const file = String(manifest[loaded]?.file ?? "");
      if (chartsOnly(loaded)) problems.push(`${key} loads ${loaded} up front`);
      else if (file.endsWith(".js") && VEGA_MARK.test(read(file))) problems.push(`${key} loads Vega up front in ${file}`);
    }
  }
  const files = new Set(Object.values(manifest).map((chunk) => String(chunk.file ?? "")));
  for (const file of files) {
    if (!file.endsWith(".js")) continue;
    const text = read(file);
    if (VEGA_MARK.test(text)) problems.push(...chartBundleProblems(file, text));
  }
  return problems;
}

/** Every manifest key an entry loads up front: itself and its static imports. */
export function upfront(manifest, entry) {
  const seen = new Set();
  const walk = (key) => {
    if (seen.has(key) || !(key in manifest)) return;
    seen.add(key);
    for (const next of manifest[key].imports ?? []) walk(next);
  };
  walk(entry);
  return seen;
}

/** What is wrong with a portal build's manifest (empty: nothing). `read`
 *  answers an emitted file's text, so CodeMirror is caught in a shared chunk
 *  whose name says nothing about it. */
export function portalProblems(manifest, read = () => "") {
  const problems = [];
  for (const [key, chunk] of Object.entries(manifest)) {
    if (!chunk.isEntry) continue;
    for (const loaded of upfront(manifest, key)) {
      const file = String(manifest[loaded]?.file ?? "");
      if (lazyOnly(loaded) || file.endsWith(".wasm")) {
        problems.push(`${key} loads ${loaded} up front`);
      } else if (file.endsWith(".js") && CODEMIRROR_MARK.test(read(file))) {
        problems.push(`${key} loads CodeMirror up front in ${file}`);
      }
    }
  }
  return problems;
}

/** What is wrong with an editor build's emitted files (empty: nothing). */
export function editorProblems(files, read = () => "") {
  const problems = [];
  for (const f of files) {
    if (/loro/i.test(f) || f.endsWith(".wasm")) problems.push(`the editor build ships ${f}`);
    else if (f.endsWith(".js") && CODEMIRROR_MARK.test(read(f))) problems.push(`the editor build ships CodeMirror in ${f}`);
  }
  return problems;
}

function main(dir) {
  const assets = join(dir, "assets");
  const manifest = JSON.parse(readFileSync(join(dir, ".vite", "manifest.json"), "utf8"));
  const read = (f) => readFileSync(join(dir, f), "utf8");
  const problems = [
    ...(dir.endsWith("vscode")
      ? editorProblems(readdirSync(assets), (f) => readFileSync(join(assets, f), "utf8"))
      : portalProblems(manifest, read)),
    ...chartProblems(manifest, read),
  ];
  if (problems.length > 0) {
    console.error(`bundle check failed:\n  ${problems.join("\n  ")}`);
    process.exit(1);
  }
  console.log(`bundle check: ${dir} keeps live editing, the editor and charts lazy`);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main(process.argv[2] ?? "dist");
