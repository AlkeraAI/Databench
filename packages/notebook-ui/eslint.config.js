// ESLint 9 flat config: the shared workspace rules, plus the open core's boundary.
import shared from "@alkera/eslint-config";

export default [
  ...shared,
  // The notebook core is open and presentational: data arrives as props, so it
  // never reaches for a data layer, a router, an app, or Alkera's own UI
  // library (the open core carries the tokens it needs).
  {
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["@tanstack/react-query", "@tanstack/react-query/*"],
              message: "@alkera/notebook-ui is presentation only. The host owns data fetching.",
            },
            {
              group: ["react-router", "react-router/*", "react-router-dom", "react-router-dom/*"],
              message: "@alkera/notebook-ui is presentation only. The host owns routing.",
            },
            {
              group: ["**/apps/*", "**/apps/**", "@alkera/web", "@alkera/web/*", "@alkera/ui", "@alkera/ui/*"],
              message: "The open notebook core depends on no app and not on @alkera/ui.",
            },
            { group: ["loro-crdt", "loro-crdt/*"], message: "The live document is the host's; components take text and callbacks." },
          ],
        },
      ],
      "react-refresh/only-export-components": "off",
    },
  },
];
