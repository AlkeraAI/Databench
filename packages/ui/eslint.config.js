// ESLint 9 flat config: the shared workspace rules, plus the UI library's boundary.
import shared, { VEGA_THROUGH_THE_RUNTIME } from "@alkera/eslint-config";

const PRESENTATION_ONLY = [
  {
    group: ["@tanstack/react-query", "@tanstack/react-query/*"],
    message: "@alkera/ui is presentation only. The host app owns data fetching.",
  },
  {
    group: ["react-router", "react-router/*", "react-router-dom", "react-router-dom/*"],
    message: "@alkera/ui is presentation only. The host app owns routing.",
  },
  {
    group: ["**/apps/*", "**/apps/**", "@alkera/web", "@alkera/web/*"],
    message: "@alkera/ui never imports an app.",
  },
  { group: ["vscode"], message: "@alkera/ui never imports the VS Code host API." },
];

export default [
  ...shared,
  // A shared component is pure presentation: data arrives as props, so the
  // library never reaches for the portal's data layer, its router, an app's
  // source or the VS Code host API.
  {
    files: ["src/**/*.{ts,tsx}"],
    rules: { "no-restricted-imports": ["error", { patterns: [...PRESENTATION_ONLY, VEGA_THROUGH_THE_RUNTIME] }] },
  },
  // The chart runtime is the one module that may import Vega (it forces the
  // expression interpreter and the locked loader on every view); the chart
  // tests reach Vega directly to prove what stock Vega would do.
  {
    files: ["src/charts/vegaRuntime.ts", "src/charts/**/*.test.{ts,tsx}"],
    rules: { "no-restricted-imports": ["error", { patterns: PRESENTATION_ONLY }] },
  },
  // The library co-exports hooks, contexts and constants beside the component
  // that owns them (one module per primitive), the same one-file seam the
  // portal's pages use. Fast refresh is traded away for it.
  {
    files: ["src/**/*.{ts,tsx}"],
    rules: { "react-refresh/only-export-components": "off" },
  },
  // The one module allowed to name the raw web stores: it is the guarded
  // wrapper every other caller is sent to.
  {
    files: ["src/storage.ts"],
    rules: { "no-restricted-properties": "off" },
  },
  // Findings left in place because the fix would change runtime behaviour.
  // MoreWindow: the `partial` object is rebuilt each render, so the useCallback
  // hooks that depend on it change every render.
  {
    files: ["src/preview/renderers/MoreWindow.tsx"],
    rules: { "react-hooks/exhaustive-deps": "off" },
  },
  // ChatFiles: `seen` is keyed on `resolver` on purpose (a new resolver starts
  // a new set), which the rule reports as an unnecessary dependency.
  {
    files: ["src/primitives/render/Markdown/ChatFiles.tsx"],
    rules: { "react-hooks/exhaustive-deps": "off" },
  },
  // Text: `title` is destructured out so it is not forwarded; the binding is
  // unused by design.
  {
    files: ["src/primitives/display/Text/Text.tsx"],
    rules: { "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_title$" }] },
  },
  // Composer.carets test: the regex strips a zero-width space on purpose.
  {
    files: ["src/chat/composer/Composer.carets.test.tsx"],
    rules: { "no-irregular-whitespace": ["error", { skipRegExps: true }] },
  },
  // ConnectionFormFields test: its `no-bitwise` disable comment documents the
  // bitmask, and no-bitwise is not enabled, so the directive reads as unused.
  {
    files: ["src/connections/ConnectionFormFields/ConnectionFormFields.test.tsx"],
    linterOptions: { reportUnusedDisableDirectives: "off" },
  },
];
