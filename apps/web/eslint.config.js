// ESLint 9 flat config: the shared workspace rules, plus the portal's own.
import shared, { UI_THROUGH_ITS_ENTRYPOINTS, VEGA_THROUGH_THE_RUNTIME } from "@alkera/eslint-config";

export default [
  ...shared,
  // The ported portal tree (grafted from apps/portal) co-exports providers with
  // their hooks/context by convention — a one-file seam that deliberately trades
  // HMR fast-refresh away. The design lab's tool modules co-export each tool's
  // group-step adapter beside its card component, the same one-file seam. The
  // rule stays on for webview/ + shared/ code.
  {
    files: [
      "src/pages/**/*.{ts,tsx}",
      "src/app/**/*.{ts,tsx}",
      "src/lib/**/*.{ts,tsx}",
      "src/components/**/*.{ts,tsx}",
      "src/dev/**/*.{ts,tsx}",
    ],
    rules: { "react-refresh/only-export-components": "off" },
  },
  // Concern boundary. The browser portal (src/{pages,app,api,lib,components})
  // must never depend on the extension webview; the webview may reach into the
  // portal's pages (lineage/knowledge surfaces reuse them).
  {
    files: [
      "src/pages/**/*.{ts,tsx}",
      "src/app/**/*.{ts,tsx}",
      "src/api/**/*.{ts,tsx}",
      "src/lib/**/*.{ts,tsx}",
      "src/components/**/*.{ts,tsx}",
    ],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            UI_THROUGH_ITS_ENTRYPOINTS,
            {
              group: ["@/webview/*", "**/webview/*"],
              message:
                "the browser portal must not import the IDE webview; webview→pages is the allowed direction",
            },
            VEGA_THROUGH_THE_RUNTIME,
          ],
        },
      ],
    },
  },
];
