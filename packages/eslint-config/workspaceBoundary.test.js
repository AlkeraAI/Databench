// The boundary rule against a real composed layout on disk: an outer
// workspace whose inner workspace sits in a nested folder with its own
// pnpm-workspace.yaml.
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { RuleTester } from "eslint";
import tseslint from "typescript-eslint";
import { afterAll, describe, expect, it } from "vitest";

import { workspaceBoundary, workspaceGlobs, workspaceRootOf } from "./workspaceBoundary.js";

RuleTester.describe = describe;
RuleTester.it = it;
RuleTester.itOnly = it.only;

const root = mkdtempSync(join(tmpdir(), "workspace-boundary-"));
afterAll(() => rmSync(root, { recursive: true, force: true }));

function write(path, text) {
  mkdirSync(join(root, path, ".."), { recursive: true });
  writeFileSync(join(root, path), text);
}
const pkg = (name) => JSON.stringify({ name });
// A specifier that climbs `levels` directories before naming `rest`.
const up = (levels, rest) => JSON.stringify("../".repeat(levels) + rest);

// The outer root lists the inner packages by path, plus its own.
write("pnpm-workspace.yaml", 'packages:\n  - "Open/site/web"\n  - "Open/kit/*"\n  - "site/web"\n  - "kit/ui" # trailing comment\n');
write("site/web/package.json", pkg("@alkera/outer-web"));
write("site/web/src/pages/extra/Page.tsx", "");
write("kit/ui/package.json", pkg("@alkera/outer-ui"));
// The open tree is a workspace of its own.
write("Open/pnpm-workspace.yaml", "onlyBuiltDependencies:\n  - esbuild\n\npackages:\n  - site/web\n  - 'kit/*'\n");
write("Open/site/web/package.json", pkg("@alkera/web"));
write("Open/site/web/src/main.tsx", "");
write("Open/kit/ui/package.json", pkg("@alkera/ui"));
write("Open/kit/ui/src/index.ts", "");
write("Open/kit/chat-model/package.json", pkg("@alkera/chat-model"));

const openFile = join(root, "Open/site/web/src/app/App.tsx");
const outerFile = join(root, "site/web/src/pages/extra/Page.tsx");

const tester = new RuleTester({ languageOptions: { parser: tseslint.parser, ecmaVersion: "latest", sourceType: "module" } });
const rule = workspaceBoundary.rules["imports-stay-in-workspace"];

describe("the workspace root", () => {
  it("is the nearest directory with a workspace file, so the open tree is its own", () => {
    expect(workspaceRootOf(join(root, "Open/site/web/src"))).toBe(join(root, "Open"));
    expect(workspaceRootOf(join(root, "site/web/src/pages"))).toBe(root);
  });

  it("reads the flat package list in either quoting, and nothing outside it", () => {
    expect(workspaceGlobs("onlyBuiltDependencies:\n  - esbuild\npackages:\n  - apps/web\n  - 'packages/*'\n  - \"preview\"  # local\nother:\n  - x\n")).toEqual([
      "apps/web",
      "packages/*",
      "preview",
    ]);
  });
});

tester.run("imports-stay-in-workspace", rule, {
  valid: [
    // Open code: relative imports inside the open tree, open packages by name,
    // third-party packages untouched.
    { filename: openFile, code: 'import { App } from "../main";' },
    { filename: openFile, code: `import x from ${up(4, "kit/ui/src/index")};` },
    { filename: openFile, code: 'import { Button } from "@alkera/ui";' },
    { filename: openFile, code: 'import { model } from "@alkera/chat-model/sub/path";' },
    { filename: openFile, code: 'import React from "react"; import { z } from "@tanstack/react-query";' },
    // Outer workspace code: open packages by name, its own packages by name, its own files.
    { filename: outerFile, code: 'import { Button } from "@alkera/ui"; import { Shell } from "@alkera/web";' },
    { filename: outerFile, code: 'import { Card } from "@alkera/outer-ui";' },
    { filename: outerFile, code: 'import { x } from "./Page";' },
    // A scope the options do not guard.
    { filename: openFile, code: 'import p from "@alkera/outer-web";', options: [{ scopes: ["@other"] }] },
  ],
  invalid: [
    // Open code naming an outer package, in every import form.
    { filename: openFile, code: 'import { Page } from "@alkera/outer-web";', errors: [{ messageId: "foreignPackage" }] },
    { filename: openFile, code: 'export { Card } from "@alkera/outer-ui/connections";', errors: [{ messageId: "foreignPackage" }] },
    { filename: openFile, code: 'export * from "@alkera/outer-ui";', errors: [{ messageId: "foreignPackage" }] },
    { filename: openFile, code: 'const m = import("@alkera/outer-web");', errors: [{ messageId: "foreignPackage" }] },
    { filename: openFile, code: 'vi.mock("@alkera/outer-ui", () => ({}));', errors: [{ messageId: "foreignPackage" }] },
    { filename: openFile, code: 'const r = require("@alkera/outer-web");', errors: [{ messageId: "foreignPackage" }] },
    { filename: openFile, code: 'type T = import("@alkera/outer-web").X;', errors: [{ messageId: "foreignPackage" }] },
    // Open code reaching out of its folder by path, to a file that exists and one that does not.
    { filename: openFile, code: `import p from ${up(5, "site/web/src/pages/extra/Page")};`, errors: [{ messageId: "outsidePath" }] },
    { filename: openFile, code: `import p from ${up(5, "gone/away")};`, errors: [{ messageId: "outsidePath" }] },
    // Outer code reaching into the inner folder by path instead of by name.
    { filename: outerFile, code: `import { App } from ${up(5, "Open/site/web/src/main")};`, errors: [{ messageId: "outsidePath" }] },
    { filename: outerFile, code: `import { App } from ${JSON.stringify(join(root, "Open/site/web/src/main"))};`, errors: [{ messageId: "outsidePath" }] },
    // A package of no workspace at all.
    { filename: outerFile, code: 'import x from "@alkera/nowhere";', errors: [{ messageId: "foreignPackage" }] },
  ],
});
