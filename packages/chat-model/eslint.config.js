// ESLint 9 flat config: the shared workspace rules, plus the model's boundary.
import shared from "@alkera/eslint-config";

export default [
  // Generated from the backend tool surface (`make gen-tool-manifest`); never
  // hand-edited, so never linted.
  { ignores: ["src/generated/**"] },
  ...shared,
  // The conversation model sits below the UI library: @alkera/ui depends on
  // it, never the reverse.
  {
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["@alkera/ui", "@alkera/ui/*", "**/packages/ui/**"],
              message: "@alkera/chat-model never imports @alkera/ui. The UI depends on the model.",
            },
          ],
        },
      ],
    },
  },
];
