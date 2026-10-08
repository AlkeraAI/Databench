// The open/private boundary for TypeScript: an open file may not import a private one.
//
// Which paths are private is not written here. The private side names a manifest of
// `[groups.<name>]` tables (a `side` and a `paths` glob list each) through the ESLint
// setting `openBoundary.manifest`, a path relative to the importer's pnpm workspace
// root. With no manifest configured, or none at that path, the rule reports nothing:
// the open repository holds nothing private, and its own workspace boundary already
// refuses any import that leaves the folder.
//
// A plain `no-restricted-imports` pattern matches the specifier as written, so it cannot
// see that `../gate/format` from pages/organization/settings lands in a private folder.
// This rule resolves every specifier (relative, a path alias of the importer's package,
// and a workspace package's `exports` entry such as `@alkera/ui/<concern>`) to a
// repository path and classifies that path with the manifest's globs: private wins over
// open. The aliases are the importer's package's own `compilerOptions.paths`, so the lint
// resolves `@/x` exactly as the typecheck does and no package's layout is spelled here.

import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";

import { workspacePackageDirs, workspaceRootOf } from "./workspaceBoundary.js";

/** `**` spans directories, `*` and `?` stay inside one path segment. */
export function globToRegExp(pattern) {
  let source = "";
  for (const token of pattern.split(/(\*\*|\*|\?)/)) {
    if (token === "**") source += ".*";
    else if (token === "*") source += "[^/]*";
    else if (token === "?") source += "[^/]";
    else source += token.replace(/[.+^${}()|[\]\\]/g, "\\$&");
  }
  return new RegExp(`^${source}$`);
}

/** The manifest's groups with their side and path globs. Reads the small TOML subset the
 *  manifest is written in: `[groups.<name>]` tables holding `side = "…"` and a `paths`
 *  array of strings, with `#` comments. */
export function parseManifest(text) {
  const groups = [];
  let current = null;
  let inPaths = false;
  for (const raw of text.split("\n")) {
    const line = raw.replace(/#.*$/, "").trim();
    if (!line) continue;
    const table = /^\[([^\]]+)\]$/.exec(line);
    if (table) {
      inPaths = false;
      const name = table[1].startsWith("groups.") ? table[1].slice("groups.".length) : null;
      current = name ? { name, side: null, paths: [] } : null;
      if (current) groups.push(current);
      continue;
    }
    if (!current) continue;
    const side = /^side\s*=\s*"([^"]*)"$/.exec(line);
    if (side) current.side = side[1];
    const opens = /^paths\s*=\s*\[/.test(line);
    if (opens) inPaths = true;
    if (!inPaths) continue;
    for (const m of line.matchAll(/"([^"]*)"/g)) current.paths.push(m[1]);
    if (line.includes("]")) inPaths = false;
  }
  return groups.map((g) => ({ ...g, regexps: g.paths.map(globToRegExp) }));
}

/** The private group a repository path belongs to, or null when it is open. */
export function privateGroupOf(groups, path) {
  return groups.find((g) => g.side === "private" && g.regexps.some((r) => r.test(path)))?.name ?? null;
}

const EXTENSIONS = ["", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".css", "/index.ts", "/index.tsx", "/index.js"];

function asFile(absolute) {
  for (const ext of EXTENSIONS) {
    const candidate = absolute + ext;
    if (existsSync(candidate) && statSync(candidate).isFile()) return candidate;
  }
  return absolute;
}

/** Every workspace package by name, with the directory its `exports` resolve against. */
function workspacePackages(root) {
  const packages = new Map();
  for (const dir of workspacePackageDirs(root)) {
    const manifest = join(dir, "package.json");
    if (!existsSync(manifest)) continue;
    const pkg = JSON.parse(readFileSync(manifest, "utf8"));
    if (pkg.name) packages.set(pkg.name, { dir, exports: pkg.exports ?? null, main: pkg.main ?? null });
  }
  return packages;
}

/** The nearest directory at or above `start` holding a package.json. */
function packageDirOf(start) {
  let dir = resolve(start);
  for (;;) {
    if (existsSync(join(dir, "package.json"))) return dir;
    const parent = dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

/** A package's path aliases from its tsconfig.json `compilerOptions.paths`, each as a
 *  directory relative to `root`: `{"@/*": ["src/*"]}` in apps/web gives
 *  `{"@/": "apps/web/src/"}`. Only the `prefix/*` → `dir/*` form is read. */
export function tsconfigAliases(packageDir, root) {
  const path = join(packageDir, "tsconfig.json");
  if (!existsSync(path)) return {};
  const { compilerOptions = {} } = JSON.parse(readFileSync(path, "utf8"));
  const base = join(packageDir, compilerOptions.baseUrl ?? ".");
  const aliases = {};
  for (const [pattern, targets] of Object.entries(compilerOptions.paths ?? {})) {
    const first = Array.isArray(targets) ? targets[0] : undefined;
    if (!pattern.endsWith("/*") || typeof first !== "string" || !first.endsWith("/*")) continue;
    const dir = relative(root, join(base, first.slice(0, -1))).split(sep).join("/");
    aliases[pattern.slice(0, -1)] = `${dir}/`;
  }
  return aliases;
}

/** The repository path a specifier lands on, or null for an external package. */
export function resolveSpecifier(specifier, fromFile, { root, aliases, packages }) {
  const bare = specifier.split("?")[0];
  let target = null;
  if (bare.startsWith(".")) target = resolve(dirname(fromFile), bare);
  for (const [prefix, dir] of Object.entries(aliases)) {
    if (target === null && bare.startsWith(prefix)) target = join(root, dir, bare.slice(prefix.length));
  }
  if (target === null) {
    const name = bare.startsWith("@") ? bare.split("/").slice(0, 2).join("/") : bare.split("/")[0];
    const pkg = packages.get(name);
    if (!pkg) return null;
    const subpath = `.${bare.slice(name.length)}`;
    const entry = typeof pkg.exports === "object" && pkg.exports !== null ? pkg.exports[subpath] : null;
    if (typeof entry === "string") target = join(pkg.dir, entry);
    else if (subpath === ".") target = join(pkg.dir, pkg.main ?? "index");
    else target = join(pkg.dir, subpath.slice(2));
  }
  return relative(root, asFile(target)).split(sep).join("/");
}

const boundaries = new Map();

/** The manifest path the private side configured, or null when none is. */
export function manifestSetting(settings) {
  const manifest = settings?.openBoundary?.manifest;
  return typeof manifest === "string" && manifest !== "" ? manifest : null;
}

/** The manifest and package map of the workspace `root`, or null when it has no manifest. */
function boundaryOf(root, manifestPath) {
  const key = `${root}\0${manifestPath}`;
  if (!boundaries.has(key)) {
    const manifest = join(root, manifestPath);
    boundaries.set(
      key,
      existsSync(manifest)
        ? { groups: parseManifest(readFileSync(manifest, "utf8")), packages: workspacePackages(root), aliases: new Map() }
        : null,
    );
  }
  return boundaries.get(key);
}

function aliasesOf(boundary, root, packageDir) {
  if (packageDir === null) return {};
  if (!boundary.aliases.has(packageDir)) boundary.aliases.set(packageDir, tsconfigAliases(packageDir, root));
  return boundary.aliases.get(packageDir);
}

/** @type {import("eslint").Rule.RuleModule} */
export const openBoundaryRule = {
  meta: {
    type: "problem",
    docs: { description: "Open code may not import private code (the manifest named by settings.openBoundary.manifest)." },
    schema: [],
    messages: {
      crossing:
        "{{file}} is open and {{target}} belongs to the private {{group}} group. Register the private part through an extension point instead of importing it.",
    },
  },
  create(context) {
    const manifestPath = manifestSetting(context.settings);
    const root = manifestPath === null ? null : workspaceRootOf(dirname(context.filename));
    const boundary = root === null ? null : boundaryOf(root, manifestPath);
    if (boundary === null || boundary.groups.length === 0) return {};
    const { groups } = boundary;
    const file = relative(root, context.filename).split(sep).join("/");
    if (privateGroupOf(groups, file) !== null) return {};
    const resolveContext = {
      root,
      aliases: aliasesOf(boundary, root, packageDirOf(dirname(context.filename))),
      packages: boundary.packages,
    };
    const check = (node, specifier) => {
      if (typeof specifier !== "string") return;
      const target = resolveSpecifier(specifier, context.filename, resolveContext);
      if (target === null) return;
      const group = privateGroupOf(groups, target);
      if (group !== null) context.report({ node, messageId: "crossing", data: { file, target, group } });
    };
    const sourceOf = (node) => (node.source && node.source.type === "Literal" ? node.source.value : null);
    return {
      ImportDeclaration: (node) => check(node, sourceOf(node)),
      ExportNamedDeclaration: (node) => check(node, sourceOf(node)),
      ExportAllDeclaration: (node) => check(node, sourceOf(node)),
      ImportExpression: (node) => check(node, sourceOf(node)),
      // A test's module mock names the module it stands in for.
      CallExpression(node) {
        const callee = node.callee;
        const isViModule =
          callee.type === "MemberExpression" &&
          callee.object.type === "Identifier" &&
          callee.object.name === "vi" &&
          callee.property.type === "Identifier" &&
          ["mock", "doMock", "importActual", "importMock"].includes(callee.property.name);
        const first = node.arguments[0];
        if (isViModule && first && first.type === "Literal") check(node, first.value);
      },
    };
  },
};

export const openBoundaryPlugin = { rules: { "open-boundary": openBoundaryRule } };

/** The rule over every source and test file of a package. */
export const openBoundaryConfig = {
  files: ["src/**/*.{ts,tsx,js,jsx}"],
  plugins: { oss: openBoundaryPlugin },
  rules: { "oss/open-boundary": "error" },
};
