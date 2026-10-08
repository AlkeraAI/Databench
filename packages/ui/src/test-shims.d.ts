/* Minimal ambient typing for the node builtins tests use (this package ships no @types/node — it is
 * a browser library; only vitest runs its code under node). Stylesheet-contract tests read a CSS
 * file as text through this seam: vitest's CSS pipeline swallows `.css?raw` imports (they resolve
 * to an empty string), so fs is the only reliable way to see the real stylesheet. Delete this shim
 * if @types/node is ever added. */
declare module "node:fs" {
  export function readFileSync(path: URL | string, encoding: "utf8"): string;
}
declare module "node:path" {
  export function dirname(path: string): string;
  export function join(...parts: string[]): string;
}
