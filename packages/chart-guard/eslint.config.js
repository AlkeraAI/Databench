// ESLint 9 flat config: the shared workspace rules, plus the guard's boundary.
import shared from "@alkera/eslint-config";

export default [
  ...shared,
  // The guard sits below every renderer: @alkera/ui and @alkera/notebook-ui
  // depend on it, and it depends on nothing, so the notebook's open core can
  // run it without the UI library.
  {
    files: ["src/**/*.ts"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["@alkera/*", "**/packages/**", "**/apps/**", "vega", "vega-*"],
              message: "@alkera/chart-guard depends on nothing: renderers depend on it.",
            },
          ],
        },
      ],
    },
  },
];
