import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  define: { __ALK_WIDGETS_VERSION__: JSON.stringify("0.0.0-test") },
  resolve: {
    alias: {
      "virtual:widgets-css": fileURLToPath(new URL("./src/tests/fixtures/widgetsCss.ts", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    environmentOptions: { jsdom: { runScripts: "dangerously", url: "https://content.test/c/nb-output/x" } },
    include: ["src/tests/**/*.test.ts"],
    setupFiles: ["src/tests/setup.ts"],
  },
});
