import { defineConfig } from "vitest/config";

import { alkeraSvgr } from "./svgr.config";

// Component tests run co-located with their component (bulletproof-react): each
// base/<C>/<C>.test.tsx drives the primitive through the same jsdom harness the
// consuming apps use. The one plugin mirrors the apps' Vite setup so the connector
// marks' vendor `.svg?react` imports resolve and tests exercise the real files.
export default defineConfig({
  plugins: [alkeraSvgr()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
  },
});
