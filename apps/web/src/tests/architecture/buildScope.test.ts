// The SPA deploy installs only `pnpm install --filter "@alkera/web..."`: the web
// app and the workspace packages it depends on, transitively. A workspace package
// the web build reaches by path (an import out of vite.config.ts, or a build
// script a plugin runs) and that nothing in that closure declares is outside the
// install, so the deploy's `vite build` fails although the gate, which installs
// the whole workspace, passed. This holds every package the build reaches to the
// declared closure.

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { REPO_ROOT } from "./scan";

const WEB = "apps/web";

interface PackageJson {
  name?: string;
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
}

function readJson(path: string): PackageJson {
  return JSON.parse(readFileSync(join(REPO_ROOT, path), "utf-8")) as PackageJson;
}

/** Every workspace package directory, by its package name. */
function workspaceDirs(): Map<string, string> {
  const dirs = new Map<string, string>();
  for (const root of ["apps", "packages"]) {
    const abs = join(REPO_ROOT, root);
    for (const name of existsSync(abs) ? readdirSync(abs) : []) {
      const manifest = join(root, name, "package.json");
      if (!existsSync(join(REPO_ROOT, manifest))) continue;
      const pkg = readJson(manifest);
      if (pkg.name) dirs.set(pkg.name, join(root, name));
    }
  }
  return dirs;
}

/** The directories `pnpm --filter "<name>..."` selects: the package and every
 *  workspace package it declares, transitively. */
export function filterClosure(start: string, dirs: Map<string, string>): Set<string> {
  const seen = new Set<string>();
  const queue = [start];
  while (queue.length > 0) {
    const dir = queue.pop() as string;
    if (seen.has(dir)) continue;
    seen.add(dir);
    const pkg = readJson(join(dir, "package.json"));
    for (const [name, spec] of Object.entries({ ...pkg.dependencies, ...pkg.devDependencies })) {
      const target = dirs.get(name);
      if (target && spec.startsWith("workspace:")) queue.push(target);
    }
  }
  return seen;
}

/** Repo-relative workspace package directories a build file reaches by path:
 *  relative imports, and `"../<sibling>"` references resolved against its
 *  package root. Returns the reached directories and the files imported. */
export function reachedByPath(file: string, source: string): { dirs: string[]; files: string[] } {
  const from = dirname(join(REPO_ROOT, file));
  const files: string[] = [];
  const dirs: string[] = [];
  for (const match of source.matchAll(/from\s+["'](\.{1,2}\/[^"']+)["']/g)) {
    const target = relative(REPO_ROOT, resolve(from, match[1]));
    files.push(target.endsWith(".ts") ? target : `${target}.ts`);
  }
  const packageRoot = file.split("/").slice(0, 2).join("/");
  for (const match of source.matchAll(/["']\.\.\/([a-z0-9-]+)["']/g)) {
    dirs.push(join(dirname(packageRoot), match[1]));
  }
  for (const target of files) dirs.push(target.split("/").slice(0, 2).join("/"));
  return { dirs, files };
}

/** Every workspace package directory the web build reaches from vite.config.ts. */
function buildReach(): Set<string> {
  const reached = new Set<string>();
  const queue = [`${WEB}/vite.config.ts`];
  const done = new Set<string>();
  while (queue.length > 0) {
    const file = queue.pop() as string;
    if (done.has(file) || !existsSync(join(REPO_ROOT, file))) continue;
    done.add(file);
    const { dirs, files } = reachedByPath(file, readFileSync(join(REPO_ROOT, file), "utf-8"));
    for (const dir of dirs) if (existsSync(join(REPO_ROOT, dir, "package.json"))) reached.add(dir);
    // Only the build tooling outside the app is followed; the app's own sources
    // are installed with it.
    for (const next of files) if (!next.startsWith(`${WEB}/`)) queue.push(next);
  }
  return reached;
}

describe("the SPA deploy's filtered install", () => {
  it("finds an import out of the config and a sibling a build plugin runs", () => {
    const config = reachedByPath(
      "apps/web/vite.config.ts",
      'import { frameModules } from "../../packages/notebook-ui/vite/frameModules";',
    );
    expect(config.dirs).toEqual(["packages/notebook-ui"]);
    const plugin = reachedByPath(
      "packages/notebook-ui/vite/frameModules.ts",
      'const WIDGETS_ROOT = resolve(PACKAGE_ROOT, "../widgets");',
    );
    expect(plugin.dirs).toEqual(["packages/widgets"]);
  });

  it("holds every workspace package the web build reaches", () => {
    const closure = filterClosure(WEB, workspaceDirs());
    const missing = [...buildReach()].filter((dir) => !closure.has(dir)).sort();
    expect(missing).toEqual([]);
  });
});
