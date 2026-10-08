// Where the dev server reads the per-checkout env files.
//
// The same rule as the Python settings: the env files live in the nearest
// directory at or above the package that holds a `.env` or `.env.workspace`.
// An open clone keeps them at its own root; a composed checkout keeps them at
// the private root, above the open folder; either way the web package finds
// them without knowing which layout it sits in.

import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

/** The files whose presence marks the env root. */
const ROOT_MARKERS = [".env", ".env.workspace"] as const;

/** The nearest directory at or above `start` holding an env file, or null. */
export function findEnvRoot(start: string): string | null {
  let dir = resolve(start);
  for (;;) {
    if (ROOT_MARKERS.some((name) => existsSync(join(dir, name)))) return dir;
    const parent = dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

/** `KEY=value` lines of a dotenv file; a missing file reads as empty. */
export function readDotenv(path: string): Record<string, string> {
  const out: Record<string, string> = {};
  if (!existsSync(path)) return out;
  for (const line of readFileSync(path, "utf-8").split("\n")) {
    const match = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/.exec(line);
    if (match) out[match[1]] = match[2];
  }
  return out;
}

/** The generated per-checkout `.env.workspace` for the package at `packageDir`. */
export function readWorkspaceEnv(packageDir: string): Record<string, string> {
  const root = findEnvRoot(packageDir);
  return root === null ? {} : readDotenv(join(root, ".env.workspace"));
}
