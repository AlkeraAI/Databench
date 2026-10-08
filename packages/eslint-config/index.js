// ESLint 9 flat config shared by every TypeScript package in the workspace.
// A package imports it by name (`@alkera/eslint-config`) and appends its own
// blocks, so no config reaches into another package's directory.
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";

import { openBoundaryConfig } from "./openBoundary.js";
import { workspaceBoundaryConfig } from "./workspaceBoundary.js";

export { openBoundaryConfig, openBoundaryPlugin, openBoundaryRule } from "./openBoundary.js";
export { workspaceBoundary, workspaceBoundaryConfig } from "./workspaceBoundary.js";

// Charts are drawn by one module, @alkera/ui's chart runtime, which forces the
// expression interpreter (no eval under the CSP) and a loader that fetches
// nothing. Importing Vega anywhere else would skip both.
export const VEGA_THROUGH_THE_RUNTIME = {
  group: ["vega", "vega/*", "vega-lite", "vega-lite/*", "vega-embed", "vega-embed/*", "vega-interpreter"],
  message: "Draw charts with <AlkeraChart> from @alkera/ui; only its runtime imports Vega.",
};

export const UI_THROUGH_ITS_ENTRYPOINTS = {
  group: ["packages/ui/src/*"],
  message: "Import shared UI through @alkera/ui entrypoints.",
};

// Settings a composing checkout adds to every package's lint (the open-boundary
// manifest, for one). The file sits beside this one only where the composing side
// provides it; the open tree has none.
const PRODUCT_SETTINGS = fileURLToPath(import.meta.resolve("./productSettings.json"));
const productSettings = existsSync(PRODUCT_SETTINGS) ? JSON.parse(readFileSync(PRODUCT_SETTINGS, "utf8")) : null;

export default [
  { ignores: ["dist", "node_modules", "coverage"] },
  ...(productSettings === null ? [] : [{ settings: productSettings }]),
  js.configs.recommended,
  ...tseslint.configs.recommended,
  workspaceBoundaryConfig,
  // The open/private boundary: open code never imports private code, in source or in
  // tests. It is active where the composing side names its manifest through the
  // `openBoundary.manifest` setting (productSettings.json above). A private page, surface or card reaches the open app
  // by registering into one of its extension points.
  openBoundaryConfig,
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: { window: "readonly", document: "readonly", fetch: "readonly" },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": "warn",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      // Naming a web store is what throws in a private window, a blocked-cookie
      // profile or an embedded webview — before any key is read — so an
      // unguarded access inside a render takes the page to the error boundary.
      // Everything goes through @alkera/ui/storage, which cannot throw and
      // keeps a refused write for the rest of the page session.
      "no-restricted-globals": [
        "error",
        ...["localStorage", "sessionStorage", "indexedDB"].map((name) => ({
          name,
          message:
            "Use safeLocalStorage()/safeSessionStorage() from @alkera/ui/storage — a raw store throws where site data is blocked.",
        })),
      ],
      "no-restricted-properties": [
        "error",
        ...["window", "globalThis", "self"].flatMap((object) =>
          ["localStorage", "sessionStorage", "indexedDB"].map((property) => ({
            object,
            property,
            message:
              "Use safeLocalStorage()/safeSessionStorage() from @alkera/ui/storage — a raw store throws where site data is blocked.",
          })),
        ),
      ],
      "no-restricted-imports": [
        "error",
        { patterns: [UI_THROUGH_ITS_ENTRYPOINTS, VEGA_THROUGH_THE_RUNTIME] },
      ],
    },
  },
  // The test harness installs a store where the runner has none, and the guard
  // tests take one away again — both have to name the globals the product code
  // may not.
  {
    files: [
      "src/test-setup.ts",
      "src/tests/**/*.{ts,tsx}",
      "src/**/*.{test,spec}.{ts,tsx}",
    ],
    rules: { "no-restricted-globals": "off", "no-restricted-properties": "off" },
  },
];
