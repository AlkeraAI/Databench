// ESLint 9 flat config: the shared workspace rules, plus the frame bundle's boundary.
import shared from "@alkera/eslint-config";

export default [
  ...shared,
  {
    files: ["src/**/*.ts"],
    languageOptions: {
      globals: { addEventListener: "readonly", queueMicrotask: "readonly", atob: "readonly", btoa: "readonly" },
    },
    rules: {
      // The bundle runs in a frame with no network and no eval.
      "no-restricted-globals": ["error", "fetch", "eval", "XMLHttpRequest", "WebSocket", "Worker"],
      "no-new-func": "error",
      "no-implied-eval": "error",
    },
  },
  // Tests run module text through Function: jsdom does not execute inline
  // scripts, and the real injection is covered under the real CSP.
  { files: ["src/tests/**/*.ts"], rules: { "no-new-func": "off" } },
];
