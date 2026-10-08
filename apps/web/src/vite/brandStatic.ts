// The brand's static files and page title, served by the web build.
//
// The favicons, touch icons, web manifest and email wordmark belong to a brand, not to the app,
// so they do not live in public/. The open build serves the Databench set; a product build passes
// its own brand directory (webConfig `brand`). Either way the files appear at the site root
// exactly as public/ files would, and index.html's <title> is the brand's product name.

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { extname, join, relative, sep } from "node:path";

import type { Plugin } from "vite";

export interface BrandStaticOptions {
  /** The product name a person reads in the browser tab before the app sets a page title. */
  readonly productName: string;
  /** The brand's static files, served at the site root. */
  readonly staticDir: string;
  /** The app's public/ directory: a brand file may not shadow one of its files. */
  readonly publicDir: string;
}

const CONTENT_TYPES: Record<string, string> = {
  ".ico": "image/x-icon",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

/** Every file under `dir`, as a site path with forward slashes (`email/x.png`). */
export function brandFiles(dir: string): string[] {
  const walk = (at: string): string[] =>
    readdirSync(at).flatMap((name) => {
      const path = join(at, name);
      return statSync(path).isDirectory() ? walk(path) : [relative(dir, path).split(sep).join("/")];
    });
  return existsSync(dir) ? walk(dir).sort() : [];
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function brandStatic({ productName, staticDir, publicDir }: BrandStaticOptions): Plugin {
  const files = new Set(brandFiles(staticDir));
  return {
    name: "alkera:brand-static",
    buildStart() {
      const shadowed = [...files].filter((file) => existsSync(join(publicDir, file)));
      if (shadowed.length > 0) this.error(`brand files shadow public/ files: ${shadowed.join(", ")}`);
    },
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const path = decodeURIComponent((req.url ?? "").split("?")[0] ?? "").replace(/^\/+/, "");
        if (!files.has(path)) return next();
        res.setHeader("Content-Type", CONTENT_TYPES[extname(path)] ?? "application/octet-stream");
        res.end(readFileSync(join(staticDir, path)));
      });
    },
    generateBundle() {
      for (const file of files) {
        this.emitFile({ type: "asset", fileName: file, source: readFileSync(join(staticDir, file)) });
      }
    },
    transformIndexHtml: (html) => html.replace(/<title>[^<]*<\/title>/, `<title>${escapeHtml(productName)}</title>`),
  };
}
