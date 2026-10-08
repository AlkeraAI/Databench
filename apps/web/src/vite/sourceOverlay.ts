// A package that builds the web app with source of its own (the product web
// package) layers its `src/` over the open app's `src/`.
//
// The same holds for a private library over the open one it extends
// (`@alkera/ui-private` over `@alkera/ui`), which has no `@/` alias.
//
// The two trees read as one for the overlay's files: `@/x` and a relative
// import that leaves the overlay's own files resolve in the overlay first and
// then in the open source. The open source never sees the overlay: from an
// open file, `@/x` and relative imports resolve only in the open tree, so an
// open module that names a private one fails the build instead of linking.
// A path present in both trees is refused, so the overlay can add modules but
// never silently replace an open one.
//
// The TypeScript side of the same rule is `paths` (open first) plus `rootDirs`
// in the overlay package's tsconfig.

import { existsSync, readdirSync, realpathSync, statSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

import type { Plugin } from "vite";

export interface SourceOverlayOptions {
  /** The open package's source root. */
  readonly openSrc: string;
  /** The overlay package's source root. */
  readonly overlaySrc: string;
  /** The prefix that names a source root (`@/` for the web app); none for a library. */
  readonly alias?: string;
}

function within(root: string, path: string): boolean {
  const rel = relative(root, path);
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

/** A directory as importers name it: Vite hands plugins real paths, so a
 *  symlinked root (a pnpm link, a temp folder) is compared by its target. */
function realDir(path: string): string {
  return existsSync(path) ? realpathSync(path) : resolve(path);
}

function stripQuery(id: string): string {
  const at = id.indexOf("?");
  return at === -1 ? id : id.slice(0, at);
}

function querySuffix(id: string): string {
  const at = id.indexOf("?");
  return at === -1 ? "" : id.slice(at);
}

/** Every file path under `dir`, relative to it. */
function filesUnder(dir: string, prefix = ""): string[] {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    const rel = prefix ? `${prefix}${sep}${name}` : name;
    return statSync(path).isDirectory() ? filesUnder(path, rel) : [rel];
  });
}

/** The paths the overlay would shadow in the open source. */
export function overlayCollisions({ openSrc, overlaySrc }: SourceOverlayOptions): string[] {
  return filesUnder(overlaySrc)
    .filter((rel) => existsSync(join(openSrc, rel)))
    .sort();
}

export function sourceOverlay(options: SourceOverlayOptions): Plugin {
  const openSrc = realDir(options.openSrc);
  const overlaySrc = realDir(options.overlaySrc);
  const alias = options.alias;
  return {
    name: "alkera:source-overlay",
    enforce: "pre",
    buildStart() {
      const collisions = overlayCollisions({ openSrc, overlaySrc });
      if (collisions.length > 0) {
        this.error(`the overlay at ${overlaySrc} shadows open source files: ${collisions.join(", ")}`);
      }
    },
    async resolveId(source, importer, resolveOptions) {
      const from = importer ? stripQuery(importer) : undefined;
      const fromOverlay = from !== undefined && within(overlaySrc, from);
      const lookup = (path: string) => this.resolve(path, importer, { ...resolveOptions, skipSelf: true });

      if (alias !== undefined && source.startsWith(alias)) {
        const rel = source.slice(alias.length);
        if (!fromOverlay) return lookup(join(openSrc, rel));
        return (await lookup(join(overlaySrc, rel))) ?? lookup(join(openSrc, rel));
      }

      if (!fromOverlay || !(source.startsWith("./") || source.startsWith("../"))) return null;
      const target = resolve(dirname(from), stripQuery(source));
      if (!within(overlaySrc, target)) return null;
      const own = await lookup(target + querySuffix(source));
      if (own) return own;
      return lookup(join(openSrc, relative(overlaySrc, target)) + querySuffix(source));
    },
  };
}

/** The virtual module the portal entry takes the product's extensions from. */
export const PRODUCT_MODULES_ID = "virtual:alkera-web-product";

/**
 * Serves {@link PRODUCT_MODULES_ID}: `PORTAL_EXTENSIONS`, re-exported from
 * `portal` (a module of the package that builds the product), or an empty list
 * for the open app.
 */
export function productExtensions(portal?: string): Plugin {
  const resolved = `\0${PRODUCT_MODULES_ID}`;
  return {
    name: "alkera:product-extensions",
    resolveId: (id) => (id === PRODUCT_MODULES_ID ? resolved : null),
    load: (id) => {
      if (id !== resolved) return null;
      return portal === undefined
        ? "export const PORTAL_EXTENSIONS = [];"
        : `export { PORTAL_EXTENSIONS } from ${JSON.stringify(portal)};`;
    },
  };
}
