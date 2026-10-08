// @vitest-environment node
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { findEnvRoot, readDotenv, readWorkspaceEnv } from "@/dev/envFiles";

import { devServerConfig } from "./devServerConfig";

// The three layouts the web package runs in: today's tree, an open clone, and
// the composed checkout where the open tree sits in a folder below the env files.

let root: string;
beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "env-root-"));
});
afterEach(() => rmSync(root, { recursive: true, force: true }));

function write(path: string, text: string): string {
  const full = join(root, path);
  mkdirSync(join(full, ".."), { recursive: true });
  writeFileSync(full, text);
  return full;
}

describe("the env root", () => {
  it("is the checkout root when the env files sit two levels above the package", () => {
    write(".env", "");
    write("apps/web/package.json", "{}");
    expect(findEnvRoot(join(root, "apps/web"))).toBe(root);
  });

  it("is the private root for the open package of a composed checkout", () => {
    write(".env.workspace", "WEB_PORT=41000\n");
    write("Open/apps/web/package.json", "{}");
    expect(findEnvRoot(join(root, "Open/apps/web"))).toBe(root);
    expect(readWorkspaceEnv(join(root, "Open/apps/web"))).toEqual({ WEB_PORT: "41000" });
  });

  it("is the open folder once it holds env files of its own", () => {
    write(".env.workspace", "WEB_PORT=41000\n");
    write("Open/.env", "");
    write("Open/.env.workspace", "WEB_PORT=42000\n");
    expect(readWorkspaceEnv(join(root, "Open/apps/web"))).toEqual({ WEB_PORT: "42000" });
  });

  it("is not marked by a file that only the web app reads", () => {
    write(".env.workspace", "WEB_PORT=41000\n");
    write("apps/web/.env.local", "VITE_X=1\n");
    write("apps/web/.env.example", "");
    expect(findEnvRoot(join(root, "apps/web"))).toBe(root);
  });
});

describe("a dotenv file", () => {
  it("reads as empty when it does not exist", () => {
    expect(readDotenv(join(root, ".env.workspace"))).toEqual({});
  });

  it("yields its assignments and skips comments and blank lines", () => {
    const path = write(".env.workspace", "# ports\nWEB_PORT = 41000\n\nALKERA_API_URL=http://localhost:41001\nnot a line\n");
    expect(readDotenv(path)).toEqual({ WEB_PORT: "41000", ALKERA_API_URL: "http://localhost:41001" });
  });
});

describe("the dev server", () => {
  it("binds the port the env root's workspace file names when the shell sets none", () => {
    write(".env.workspace", "WEB_PORT=41234\nALKERA_API_URL=http://localhost:41235\n");
    write("Open/apps/web/package.json", "{}");
    vi.stubEnv("WEB_PORT", undefined);
    vi.stubEnv("VITE_API_PROXY_TARGET", undefined);
    vi.stubEnv("ALKERA_API_URL", undefined);
    try {
      const server = devServerConfig(join(root, "Open/apps/web"));
      expect(server.port).toBe(41234);
      expect(Object.values(server.proxy ?? {}).map((entry) => (typeof entry === "string" ? entry : entry.target))).toContain(
        "http://localhost:41235",
      );
    } finally {
      vi.unstubAllEnvs();
    }
  });
});
