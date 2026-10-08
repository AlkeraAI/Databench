import { resolve } from "node:path";

import type { ServerOptions } from "vite";

import { defineWebConfig } from "../../../webConfig";

/** The dev server options the web Vite config produces for a package directory. */
export function devServerConfig(packageDir: string = resolve(process.cwd())): ServerOptions {
  // vitest runs with apps/web as its root.
  const config = defineWebConfig({ packageDir })({ mode: "development", command: "serve" });
  if (!config.server) throw new Error("the web config sets no dev server options");
  return config.server;
}
