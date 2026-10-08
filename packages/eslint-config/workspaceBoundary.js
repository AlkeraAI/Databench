// The workspace boundary: a module imports only what its own pnpm workspace
// holds.
//
// This repository is a pnpm workspace of its own, and a downstream build may
// nest it inside an outer workspace. Nothing in it may name anything outside
// it, and outer code reaches it by package name, never by a path into the
// folder. Both directions are one rule: the workspace root of the importer (the
// nearest directory at or above it holding a pnpm-workspace.yaml) must be the
// workspace root of the import.
//
// - A relative or absolute import must land in the importer's workspace.
// - A bare import in a guarded scope (`@alkera/*` by default) must name a
//   package of the importer's workspace. In a standalone checkout an outer
//   package does not exist; in a nested checkout it would resolve, and this is
//   what refuses it.
//
// The rule reads only the file system, so it holds with no knowledge of what
// an outer workspace contains.
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

const WORKSPACE_FILE = "pnpm-workspace.yaml";

const rootCache = new Map();
const packagesCache = new Map();

/** The nearest directory at or above `start` that holds a pnpm workspace file. */
export function workspaceRootOf(start) {
  let dir = resolve(start);
  const visited = [];
  for (;;) {
    if (rootCache.has(dir)) {
      const found = rootCache.get(dir);
      for (const seen of visited) rootCache.set(seen, found);
      return found;
    }
    visited.push(dir);
    if (existsSync(join(dir, WORKSPACE_FILE))) {
      for (const seen of visited) rootCache.set(seen, dir);
      return dir;
    }
    const parent = dirname(dir);
    if (parent === dir) {
      for (const seen of visited) rootCache.set(seen, null);
      return null;
    }
    dir = parent;
  }
}

/** The `packages:` globs of a pnpm-workspace.yaml (the flat list form this repo uses). */
export function workspaceGlobs(text) {
  const globs = [];
  let inPackages = false;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/#.*$/, "");
    if (/^\S/.test(line)) {
      inPackages = /^packages\s*:/.test(line);
      continue;
    }
    const item = inPackages ? /^\s+-\s*["']?([^"'\s]+)["']?\s*$/.exec(line) : null;
    if (item) globs.push(item[1]);
  }
  return globs;
}

function packageDirs(root, glob) {
  if (glob.startsWith("!")) return [];
  if (glob.endsWith("/*")) {
    const base = join(root, glob.slice(0, -2));
    if (!existsSync(base)) return [];
    return readdirSync(base, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => join(base, entry.name));
  }
  return [join(root, glob)];
}

/** Every package directory the workspace rooted at `root` lists. */
export function workspacePackageDirs(root) {
  const globs = workspaceGlobs(readFileSync(join(root, WORKSPACE_FILE), "utf8"));
  return globs.flatMap((glob) => packageDirs(root, glob));
}

/** Every package name the workspace rooted at `root` declares. */
export function workspacePackageNames(root) {
  if (packagesCache.has(root)) return packagesCache.get(root);
  const names = new Set();
  for (const dir of workspacePackageDirs(root)) {
    const manifest = join(dir, "package.json");
    if (!existsSync(manifest)) continue;
    const name = JSON.parse(readFileSync(manifest, "utf8")).name;
    if (typeof name === "string") names.add(name);
  }
  packagesCache.set(root, names);
  return names;
}

function packageName(specifier) {
  const parts = specifier.split("/");
  return specifier.startsWith("@") ? parts.slice(0, 2).join("/") : parts[0];
}

function isPathSpecifier(specifier) {
  return specifier.startsWith("./") || specifier.startsWith("../") || specifier === "." || specifier === ".." || isAbsolute(specifier);
}

/** The workspace root a path would sit in, judged from its nearest existing ancestor. */
function workspaceRootOfTarget(target) {
  let dir = target;
  while (!existsSync(dir)) {
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return workspaceRootOf(dir);
}

function inside(root, path) {
  const rel = relative(root, path);
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel) && !rel.startsWith(sep));
}

const rule = {
  meta: {
    type: "problem",
    docs: { description: "A module imports only from its own pnpm workspace." },
    schema: [
      {
        type: "object",
        properties: { scopes: { type: "array", items: { type: "string" } } },
        additionalProperties: false,
      },
    ],
    messages: {
      outsidePath: "'{{specifier}}' reaches outside the workspace at {{root}}. Import another workspace's code by its package name.",
      foreignPackage: "'{{name}}' is not a package of the workspace at {{root}}.",
    },
  },
  create(context) {
    const filename = context.physicalFilename ?? context.filename;
    if (!filename || !isAbsolute(filename)) return {};
    const root = workspaceRootOf(dirname(filename));
    if (root === null) return {};
    const scopes = context.options[0]?.scopes ?? ["@alkera"];
    const shownRoot = root.split(sep).slice(-1)[0] || root;

    function check(node) {
      if (!node || node.type !== "Literal" || typeof node.value !== "string") return;
      const specifier = node.value;
      if (isPathSpecifier(specifier)) {
        const target = resolve(dirname(filename), specifier);
        if (inside(root, target) && workspaceRootOfTarget(target) === root) return;
        context.report({ node, messageId: "outsidePath", data: { specifier, root: shownRoot } });
        return;
      }
      const name = packageName(specifier);
      if (!scopes.some((scope) => name.startsWith(`${scope}/`))) return;
      if (workspacePackageNames(root).has(name)) return;
      context.report({ node, messageId: "foreignPackage", data: { name, root: shownRoot } });
    }

    function checkCall(node) {
      const callee = node.callee;
      const named =
        (callee.type === "Identifier" && callee.name === "require") ||
        (callee.type === "MemberExpression" &&
          callee.object.type === "Identifier" &&
          callee.object.name === "vi" &&
          callee.property.type === "Identifier" &&
          ["mock", "doMock", "importActual", "importMock", "unmock"].includes(callee.property.name));
      if (named) check(node.arguments[0]);
    }

    return {
      ImportDeclaration: (node) => check(node.source),
      ExportNamedDeclaration: (node) => check(node.source),
      ExportAllDeclaration: (node) => check(node.source),
      ImportExpression: (node) => check(node.source),
      TSImportType: (node) => check(node.source ?? node.argument?.literal),
      CallExpression: checkCall,
    };
  },
};

export const workspaceBoundary = {
  meta: { name: "@alkera/workspace-boundary" },
  rules: { "imports-stay-in-workspace": rule },
};

/** The flat-config block that turns the rule on for every source file. */
export const workspaceBoundaryConfig = {
  files: ["**/*.{ts,tsx,js,jsx,mjs,cjs,mts,cts}"],
  plugins: { "workspace-boundary": workspaceBoundary },
  rules: { "workspace-boundary/imports-stay-in-workspace": "error" },
};
