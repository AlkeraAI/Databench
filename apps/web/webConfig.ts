import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { searchForWorkspaceRoot } from "vite";
import { defineConfig } from "vitest/config";

import { frameModules } from "../../packages/notebook-ui/vite/frameModules";
import { alkeraSvgr } from "../../packages/ui/svgr.config";
import { DATABENCH_IDENTITY } from "../../packages/ui/src/brand/databench/identity";
import { brandStatic } from "./src/vite/brandStatic";
import { productExtensions, sourceOverlay, type SourceOverlayOptions } from "./src/vite/sourceOverlay";
import { devServerHeaders } from "./src/dev/devCsp";
import { readWorkspaceEnv } from "./src/dev/envFiles";
import { devProxyTable } from "./src/dev/proxyTable";

// The web app's Vite and Vitest config, as a factory so a package that builds
// the same app (the product web package) reuses it by package name instead of
// copying it. `vite.config.ts` is the open app's one call.

export interface WebConfigOptions {
  /** The directory of the package being built: its outputs, tests and env root. */
  readonly packageDir: string;
  /**
   * Source a product package layers over the open app (src/vite/sourceOverlay.ts).
   * The open app passes none.
   */
  readonly overlay?: {
    /** The product package's src/, layered over the open app's (`@/` included). */
    readonly src: string;
    /** Further layers: a private library's src/ over the open library it extends. */
    readonly layers?: readonly SourceOverlayOptions[];
    /**
     * Module names the overlay answers instead of the open package: a private
     * library's entries under the names the product's pages import them by
     * (`@alkera/ui/lineage` from `@alkera/ui-private`).
     */
    readonly aliases?: readonly { readonly find: RegExp; readonly replacement: string }[];
  };
  /**
   * The VS Code webview, built with `--mode vscode`: the directory holding its
   * `vscode.html` and the `src/` that html loads (the webview under `src/webview/`).
   * The webview ships with the extension, which is not part of the open
   * release, so only the package that owns it passes this.
   */
  readonly webview?: { readonly dir: string };
  /**
   * The module exporting the product's `PORTAL_EXTENSIONS`, which the portal
   * entry installs before it boots. The open app passes none and installs none.
   */
  readonly portal?: string;
  /**
   * The product's brand: its name for the page title and the directory of its static files
   * (favicons, touch icons, web manifest, email wordmark). The open app passes none and is
   * served as Databench. The product's logo itself is registered through `portal`.
   */
  readonly brand?: { readonly productName: string; readonly staticDir: string };
}

/** The open app's brand, served when a package passes none. */
export const OPEN_BRAND = {
  productName: DATABENCH_IDENTITY.productName,
  staticDir: fileURLToPath(new URL("../../packages/ui/src/brand/databench/static", import.meta.url)),
} as const;

/** The open web app: its index.html, public/ and src/ live here. */
const APP_DIR = resolve(fileURLToPath(new URL(".", import.meta.url)));

// Resolve the dev-server port + API proxy target from the per-worktree env so
// each Conductor workspace binds its own ports (see ops/scripts/workspace-env.sh).
// Prefer real env vars (present when launched via `make dev-web` / `make dev-all`,
// which export .env.workspace), then fall back to reading .env.workspace from
// the env root (src/dev/envFiles.ts) so a bare `pnpm --filter @alkera/web dev`
// still works. Classic defaults keep `main` (and a clean checkout) on 5173 → :8000.
export function defineWebConfig({ packageDir, overlay, webview, portal, brand = OPEN_BRAND }: WebConfigOptions) {
  const appSrc = resolve(APP_DIR, "src");
  const ws = readWorkspaceEnv(packageDir);
  const pick = (key: string): string | undefined => process.env[key] ?? ws[key];

  const webPort = Number(pick("WEB_PORT") ?? 5173);
  const apiTarget = pick("VITE_API_PROXY_TARGET") ?? pick("ALKERA_API_URL") ?? "http://localhost:8000";

  // https://vitejs.dev/config/
  return defineConfig(({ mode, command }) => {
    // `--mode vscode` builds ONLY the webview entry with a RELATIVE base, into a
    // separate dist-vscode/. The relative base is essential: the extension loads
    // the bundle through `asWebviewUri`, so CSS `url()` refs (bundled fonts,
    // icons) must resolve against the stylesheet's webview URI — an absolute
    // `/assets/…` (base "/") would hit the webview origin and 404. The SPA build
    // keeps base "/" because its deep client-side routes need absolute asset URLs.
    const vscodeWebview = mode === "vscode";
    if (vscodeWebview && !webview) {
      throw new Error("--mode vscode builds the VS Code webview, and this package has none");
    }
    const webviewDir = webview ? resolve(webview.dir) : undefined;
    // Fail a PRODUCTION SPA build that lacks the Turnstile site key. The backend
    // requires the captcha in prod and rejects tokenless login/signup/reset, so a
    // build without the key would ship a frontend that can never authenticate. This
    // is the frontend mirror of `alkera_core.config._validate_production`; it fires
    // ONLY for the real prod SPA build (not dev, tests, or the vscode webview).
    // Self-hosted / VPC / air-gapped builds opt out with VITE_TURNSTILE_REQUIRED=false
    // (mirrors the backend's TURNSTILE_REQUIRED) — no captcha, no Cloudflare.
    const turnstileRequired = process.env.VITE_TURNSTILE_REQUIRED !== "false";
    if (
      command === "build" &&
      !vscodeWebview &&
      process.env.VITE_APP_ENV === "production" &&
      turnstileRequired &&
      !process.env.VITE_TURNSTILE_SITE_KEY
    ) {
      throw new Error(
        "VITE_TURNSTILE_SITE_KEY must be set for a production web build " +
          "(Cloudflare Turnstile guards login, signup and password reset), or set " +
          "VITE_TURNSTILE_REQUIRED=false to build without it (self-hosted / air-gapped).",
      );
    }
    // Vite builds only html under its root. The webview build is rooted at the
    // webview's directory; the app build is rooted at the open app, and carries
    // the webview entry too when the two share a directory.
    const root = vscodeWebview && webviewDir ? webviewDir : APP_DIR;
    const webviewEntry: Record<string, string> = webviewDir ? { vscode: resolve(webviewDir, "vscode.html") } : {};
    const entries: Record<string, string> = vscodeWebview
      ? webviewEntry
      : { index: resolve(APP_DIR, "index.html"), ...(webviewDir === APP_DIR ? webviewEntry : {}) };
    // The overlay plugin owns `@/` for a product build; the alias would
    // answer first and never consult the overlay.
    const sourceAliases = overlay ? [] : [{ find: /^@\//, replacement: `${appSrc}/` }];
    return {
      root,
      publicDir: resolve(APP_DIR, "public"),
      plugins: [
        ...(overlay ? [sourceOverlay({ openSrc: appSrc, overlaySrc: overlay.src, alias: "@/" })] : []),
        ...(overlay?.layers ?? []).map((layer) => sourceOverlay(layer)),
        productExtensions(portal),
        brandStatic({ ...brand, publicDir: resolve(APP_DIR, "public") }),
        react(),
        alkeraSvgr(),
        frameModules(),
      ],
      base: vscodeWebview ? "./" : "/",
      // Concern aliases: the browser portal lives at src/ (pages/, app/, api/, …)
      // and the extension webview under src/webview/. The ESLint boundary rule
      // forbids the browser tree importing the webview — see eslint.config.js.
      resolve: {
        dedupe: ["react", "react-dom"],
        alias: [
          // `@/…` resolves to src/ — the ported portal's centralized tests reference
          // their subjects by this stable path (mirrors the tsconfig `paths` entry).
          ...(overlay?.aliases ?? []),
          ...sourceAliases,
          // The editor's webview has no live draft (one keyboard, no shared
          // composer) and its CSP does not admit WebAssembly: Loro's runtime is a
          // stub there, so the build ships neither its chunk nor its .wasm.
          ...(vscodeWebview
            ? [
                {
                  find: /^\.\/loroRuntime$/,
                  replacement: resolve(appSrc, "api/realtime/crdt/loroRuntime.unavailable.ts"),
                },
              ]
            : []),
        ],
      },
      // Ported portal pages use CSS Modules (*.module.css) with camelCase keys
      // (`styles.subrowMain`); shared classes they target (.alk-*, .pt-*) stay
      // global via :global() inside the module. Mirrors apps/portal's config.
      css: { modules: { localsConvention: "camelCase" } },
      build: {
        manifest: true,
        outDir: resolve(packageDir, vscodeWebview ? "dist-vscode" : "dist"),
        emptyOutDir: true,
        rolldownOptions: {
          input: entries,
          // Strip console.* and debugger out of PRODUCTION bundles (SPA + vscode
          // webview) — cuts incidental info-leak and dead diagnostics from the
          // shipped JS. Real client errors still reach Sentry via its handlers,
          // not console. Vite 8's Rolldown pipeline ignores the old
          // `esbuild.drop` transform option, so the dropping moves to the oxc
          // minifier; `mangle`/`compress`/`codegen` stay on to match the
          // `minify: "oxc"` default this object replaces. Build-only by
          // construction — the dev server never minifies.
          output: {
            minify: {
              mangle: true,
              compress: { dropConsole: true, dropDebugger: true },
              codegen: true,
            },
          },
        },
      },
      server: {
        // A product package's own source sits outside the open app's workspace.
        fs: { allow: [searchForWorkspaceRoot(APP_DIR), searchForWorkspaceRoot(packageDir)] },
        port: webPort,
        strictPort: true,
        // Allow the VS Code extension webview (origin vscode-webview://…) to load
        // dev modules cross-origin, and pin the HMR socket to localhost so Vite's
        // client connects correctly from inside the webview (whose own location is
        // not http://localhost). See projectView.ts dev-webview branch.
        cors: true,
        hmr: { host: "localhost", protocol: "ws", clientPort: webPort },
        // The one production CSP rule worth enforcing in dev too: a file preview
        // may be framed from the content origin and from nowhere else. See
        // src/dev/devCsp.ts for why this is the only directive sent.
        headers: devServerHeaders(pick("FILES_CONTENT_BASE_URL")),
        // Proxy backend paths to the FastAPI dev server so the frontend can call
        // same-origin paths in dev without CORS. The table lives in src/dev/proxyTable
        // so a test can check every key against the real SPA route list — a plain key
        // matches by PREFIX, so one that shares a leading substring with a client
        // route silently answers deep links with the backend's 404.
        proxy: devProxyTable(apiTarget),
      },
      test: {
        environment: "jsdom",
        globals: true,
        // The 5s default fits a dev machine; starved CI runners need headroom.
        // One bound everywhere: the 5 s local tier made journey tests flaky
        // under a loaded machine (the suite in parallel, the dev stack up)
        // while the same tests always passed at CI's 20 s. Slow beats flaky.
        testTimeout: 20_000,
        // Tests are the building package's own; the harness is the open app's.
        dir: packageDir,
        setupFiles: [resolve(appSrc, "test-setup.ts")],
        // Playwright specs live under e2e/ and use a different runner.
        exclude: ["**/node_modules/**", "**/dist/**", "**/dist-vscode/**", "e2e/**"],
      },
    };
  });
}
