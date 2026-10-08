import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { AUTH_REQUIRED_CODE } from "@/lib/rpcCodes";

// The web app cannot import the daemon, so the code is spelled in both. A
// drift between them would read a sign-out as an ordinary failure, so the
// daemon's spelling is read off disk here. The editor host spells it a third
// time; the product's own tests hold that copy to the same value.

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../..");

function spelled(relPath: string, pattern: RegExp): number {
  const match = readFileSync(resolve(ROOT, relPath), "utf8").match(pattern);
  if (!match) throw new Error(`${relPath} no longer spells the code`);
  return Number(match[1]);
}

describe("AUTH_REQUIRED_CODE", () => {
  it.each([
    { where: "the daemon", path: "apps/cli/alkera_cli/daemon/server.py", pattern: /^AUTH_REQUIRED = (-\d+)/m },
  ])("is the code $where raises", ({ path, pattern }) => {
    expect(spelled(path, pattern)).toBe(AUTH_REQUIRED_CODE);
  });
});
