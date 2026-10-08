// What the built chart renderer may not contain: a way to load code or data
// from somewhere else. Charts render under CSPs with no network (the content
// frame) or only 'self' (the portal, the VS Code webview), so a CDN reference is
// at best a broken chart and at worst a supply-chain door. Vega's own strings
// (its schema URLs, the SVG namespace) are names, never fetched, and pass.
//
//   node scripts/checkChartsBundle.mjs dist/alkera-charts.iife.js [more files…]

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** Hosts that serve JavaScript packages to browsers. */
export const CDN_HOSTS = [
  "cdn.jsdelivr.net",
  "unpkg.com",
  "cdnjs.cloudflare.com",
  "esm.sh",
  "cdn.skypack.dev",
  "esm.run",
  "ga.jspm.io",
];

/** Code-loading calls aimed at an absolute URL. */
const REMOTE_LOADS = [
  /import\(\s*["'`](https?:)?\/\//,
  /importScripts\(/,
  /from\s*["'](https?:)?\/\//,
  /new\s+Worker\(\s*["'`](https?:)?\/\//,
];

/** What is wrong with one built file's text (empty: nothing). */
export function chartBundleProblems(name, text) {
  const problems = [];
  for (const host of CDN_HOSTS) {
    if (text.includes(host)) problems.push(`${name} names the package CDN ${host}`);
  }
  for (const pattern of REMOTE_LOADS) {
    if (pattern.test(text)) problems.push(`${name} loads code from a URL (${pattern.source})`);
  }
  return problems;
}

function main(files) {
  const problems = files.flatMap((file) => chartBundleProblems(file, readFileSync(file, "utf8")));
  if (problems.length > 0) {
    console.error(`chart bundle check failed:\n  ${problems.join("\n  ")}`);
    process.exit(1);
  }
  console.log(`chart bundle check: ${files.join(", ")} load nothing from elsewhere`);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main(process.argv.slice(2));
