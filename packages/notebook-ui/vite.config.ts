import { defineConfig } from "vitest/config";
import { frameModules } from "./vite/frameModules";

// Tests sit beside what they cover and run under the same jsdom harness the
// host apps use.
export default defineConfig({
  plugins: [frameModules()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
  },
});
