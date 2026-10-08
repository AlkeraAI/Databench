// The content-frame renderer: one IIFE with Vega, Vega-Lite and the
// interpreter inside it and nothing to fetch, so a frame under
// `default-src 'none'` can inline it whole.
import { resolve } from "node:path";

import { defineConfig } from "vite";

export default defineConfig({
  build: {
    outDir: "dist",
    emptyOutDir: false,
    target: "es2020",
    lib: {
      entry: resolve(__dirname, "src/charts/frame.ts"),
      name: "AlkeraCharts",
      formats: ["iife"],
      fileName: () => "alkera-charts.iife.js",
    },
    rolldownOptions: { output: { inlineDynamicImports: true } },
  },
});
