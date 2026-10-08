import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

import { RuleTester } from "eslint";
import tseslint from "typescript-eslint";
import { afterAll, describe, expect, it } from "vitest";

import {
  globToRegExp,
  manifestSetting,
  openBoundaryRule,
  parseManifest,
  privateGroupOf,
  resolveSpecifier,
  tsconfigAliases,
} from "./openBoundary.js";

// The TypeScript side of the open/private boundary. The rule reads the manifest the
// `openBoundary.manifest` setting names and refuses an open file that imports a private
// one, however the specifier is spelled. These pin that the rule is not blind: each
// spelling a crossing can take is caught, and each legitimate import passes. They run
// against a planted workspace with invented paths, so they hold in any checkout.

const REPO = mkdtempSync(join(tmpdir(), "open-boundary-composed-"));
afterAll(() => rmSync(REPO, { recursive: true, force: true }));
const at = (path) => resolve(REPO, path);
function plantIn(root, path, text = "") {
  mkdirSync(dirname(join(root, path)), { recursive: true });
  writeFileSync(join(root, path), text);
}

plantIn(REPO, "pnpm-workspace.yaml", 'packages:\n  - "site/web"\n  - "kit/*"\n');
const MANIFEST_PATH = "boundary/groups.toml";
const SETTINGS = { openBoundary: { manifest: MANIFEST_PATH } };
plantIn(
  REPO,
  MANIFEST_PATH,
  `[groups.platform]
side = "open"
paths = ["site/web/**", "kit/ui/**", "kit/chat-model/**"]

[groups.data]
side = "private"
paths = [
    "site/web/src/webview/**",
    "site/web/src/api/lineage.ts",
    "site/web/src/pages/graph/**",
    "site/web/src/pages/lineage/**",
    "site/web/src/pages/organization/gate/**",
    "site/web/src/pages/workspace/connections/**",
    "site/web/src/pages/workspace/knowledge/**",
    "site/web/src/tests/pages/lineage/**",
    "kit/ui/src/connections/**",
    "kit/ui/src/graph/**",
    "kit/ui/src/lineage/**",
]
`,
);
plantIn(REPO, "site/web/package.json", JSON.stringify({ name: "@alkera/web" }));
plantIn(REPO, "site/web/tsconfig.json", JSON.stringify({ compilerOptions: { baseUrl: ".", paths: { "@/*": ["src/*"] } } }));
plantIn(
  REPO,
  "kit/ui/package.json",
  JSON.stringify({
    name: "@alkera/ui",
    exports: {
      ".": "./src/index.ts",
      "./connections": "./src/connections/index.ts",
      "./graph": "./src/graph/index.ts",
      "./lineage": "./src/lineage/index.ts",
    },
  }),
);
plantIn(REPO, "kit/chat-model/package.json", JSON.stringify({ name: "@alkera/chat-model" }));
for (const file of [
  "site/web/src/App.tsx",
  "site/web/src/api/lineage.ts",
  "site/web/src/pages/shell/portal.ts",
  "site/web/src/pages/graph/GraphPage.tsx",
  "site/web/src/pages/lineage/seam/store.ts",
  "site/web/src/pages/organization/gate/extension.ts",
  "site/web/src/pages/organization/gate/format.ts",
  "site/web/src/pages/organization/settings/fields.tsx",
  "site/web/src/pages/workspace/knowledge/KnowledgePage.tsx",
  "site/web/src/webview/lineage/LineageSurface.tsx",
  "site/web/src/webview/plugins/PluginsSurface.tsx",
  "kit/ui/src/index.ts",
  "kit/ui/src/connections/index.ts",
  "kit/ui/src/graph/index.ts",
  "kit/ui/src/lineage/index.ts",
]) {
  plantIn(REPO, file);
}

RuleTester.describe = describe;
RuleTester.it = it;
const languageOptions = { parser: tseslint.parser, sourceType: "module" };
const tester = new RuleTester({ languageOptions, settings: SETTINGS });

tester.run("oss/open-boundary with a manifest configured", openBoundaryRule, {
  valid: [
    {
      name: "a private webview module importing another private one",
      filename: at("site/web/src/webview/vscodeApp.tsx"),
      code: 'const Lazy = () => import("./plugins/PluginsSurface");',
    },
    {
      name: "an open page importing an open module",
      filename: at("site/web/src/pages/organization/settings/OrgBody.tsx"),
      code: 'import { SettingsSection } from "./fields";',
    },
    {
      name: "a private page importing another private module",
      filename: at("site/web/src/pages/organization/gate/GateSection.tsx"),
      code: 'import { ruleLabel } from "./format";',
    },
    {
      name: "a private page importing open code",
      filename: at("site/web/src/pages/workspace/connections/extension.tsx"),
      code: 'import { PORTAL_ROUTES } from "../../shell/portal";',
    },
    {
      name: "the open ui barrel",
      filename: at("site/web/src/pages/workspace/chat/ChatPage.tsx"),
      code: 'import { Button } from "@alkera/ui";',
    },
    {
      name: "an external package",
      filename: at("site/web/src/App.tsx"),
      code: 'import { Route } from "react-router-dom";',
    },
    {
      name: "a private test mocking a private module",
      filename: at("site/web/src/tests/pages/lineage/LineagePage.test.tsx"),
      code: 'vi.mock("@/pages/lineage/seam/store", () => ({}));',
    },
  ],
  invalid: [
    {
      name: "a relative import that climbs into a private folder",
      filename: at("site/web/src/pages/organization/settings/OrgBody.tsx"),
      code: 'import { ruleLabel } from "../gate/format";',
      errors: [{ messageId: "crossing" }],
    },
    {
      name: "the @/ alias",
      filename: at("site/web/src/App.tsx"),
      code: 'import { KnowledgePage } from "@/pages/workspace/knowledge/KnowledgePage";',
      errors: [{ messageId: "crossing" }],
    },
    {
      name: "the webview through the @/ alias",
      filename: at("site/web/src/App.tsx"),
      code: 'import { LineageSurface } from "@/webview/lineage/LineageSurface";',
      errors: [{ messageId: "crossing" }],
    },
    {
      name: "an open ui module importing a private ui concern",
      filename: at("kit/ui/src/chat/index.ts"),
      code: 'import { LogoChip } from "../connections";',
      errors: [{ messageId: "crossing" }],
    },
    {
      name: "a type-only import still crosses",
      filename: at("site/web/src/pages/workspace/chat/ChatPage.tsx"),
      code: 'import type { AlkeraGraphDoc } from "@alkera/ui/graph";',
      errors: [{ messageId: "crossing" }],
    },
    {
      name: "a private ui concern through its package entry",
      filename: at("site/web/src/pages/organization/teams/detail/ContextColumn.tsx"),
      code: 'import { LogoChip } from "@alkera/ui/connections";',
      errors: [{ messageId: "crossing" }],
    },
    {
      name: "a re-export from the open ui barrel",
      filename: at("kit/ui/src/index.ts"),
      code: 'export * from "./lineage";',
      errors: [{ messageId: "crossing" }],
    },
    {
      name: "a lazy import",
      filename: at("site/web/src/App.tsx"),
      code: 'const Lazy = () => import("./pages/graph/GraphPage");',
      errors: [{ messageId: "crossing" }],
    },
    {
      name: "an open test importing a private page",
      filename: at("site/web/src/tests/app/nav.test.ts"),
      code: 'import { GATE_PORTAL } from "@/pages/organization/gate/extension";',
      errors: [{ messageId: "crossing" }],
    },
    {
      name: "an open test mocking a private module",
      filename: at("site/web/src/tests/app/nav.test.ts"),
      code: 'vi.mock("@/api/lineage", () => ({}));',
      errors: [{ messageId: "crossing" }],
    },
    {
      name: "a raw-query import",
      filename: at("site/web/src/tests/app/nav.test.ts"),
      code: 'import source from "@/pages/graph/GraphPage.tsx?raw";',
      errors: [{ messageId: "crossing" }],
    },
  ],
});

describe("the manifest reader", () => {
  const MANIFEST = `
# a comment with "quotes" and [brackets]
[scan]
sources = ["x"]

[groups.platform]
side = "open"
paths = [
    "site/web/**",  # trailing comment
]

[groups.billing]
side = "private"
decision = "owner"
paths = ["site/web/src/pages/billing/**", "site/web/src/api/billing.ts"]
`;

  it("reads each group's side and globs, and nothing outside the groups", () => {
    const groups = parseManifest(MANIFEST).map(({ name, side, paths }) => ({ name, side, paths }));
    expect(groups).toEqual([
      { name: "platform", side: "open", paths: ["site/web/**"] },
      { name: "billing", side: "private", paths: ["site/web/src/pages/billing/**", "site/web/src/api/billing.ts"] },
    ]);
  });

  it("lets a private glob win over the open one that also matches", () => {
    const groups = parseManifest(MANIFEST);
    expect(privateGroupOf(groups, "site/web/src/pages/billing/Plan.tsx")).toBe("billing");
    expect(privateGroupOf(groups, "site/web/src/api/billing.ts")).toBe("billing");
    expect(privateGroupOf(groups, "site/web/src/api/billingx.ts")).toBeNull();
    expect(privateGroupOf(groups, "site/web/src/pages/chat/Chat.tsx")).toBeNull();
  });

  it.each([
    ["site/**", "site/web/src/a.ts", true],
    ["site/*", "site/web/src/a.ts", false],
    ["site/*/src/a.ts", "site/web/src/a.ts", true],
    ["site/web/a?.ts", "site/web/ab.ts", true],
    ["site/web/a?.ts", "site/web/a/.ts", false],
    ["site/web.ts", "site/webxts", false],
  ])("glob %s against %s is %s", (pattern, path, matches) => {
    expect(globToRegExp(pattern).test(path)).toBe(matches);
  });
});

describe("specifier resolution", () => {
  const context = {
    root: REPO,
    aliases: { "@/": "site/web/src/", "@webview/": "site/web/src/webview/" },
    packages: new Map([
      [
        "@alkera/ui",
        { dir: at("kit/ui"), exports: { ".": "./src/index.ts", "./lineage": "./src/lineage/index.ts" }, main: null },
      ],
    ]),
  };

  it.each([
    ["../gate/format", "site/web/src/pages/organization/settings/OrgBody.tsx", "site/web/src/pages/organization/gate/format.ts"],
    ["@/api/lineage", "site/web/src/App.tsx", "site/web/src/api/lineage.ts"],
    ["@webview/lineage/LineageSurface", "site/web/src/App.tsx", "site/web/src/webview/lineage/LineageSurface.tsx"],
    ["@alkera/ui/lineage", "site/web/src/App.tsx", "kit/ui/src/lineage/index.ts"],
    ["@alkera/ui", "site/web/src/App.tsx", "kit/ui/src/index.ts"],
    ["./lineage", "kit/ui/src/index.ts", "kit/ui/src/lineage/index.ts"],
  ])("%s from %s lands on %s", (specifier, from, expected) => {
    expect(resolveSpecifier(specifier, at(from), context)).toBe(expected);
  });

  it("leaves an external package unresolved", () => {
    expect(resolveSpecifier("react", at("site/web/src/App.tsx"), context)).toBeNull();
  });
});

describe("the importer's package aliases", () => {
  it("are its tsconfig paths, relative to the workspace root", () => {
    expect(tsconfigAliases(at("site/web"), REPO)).toMatchObject({ "@/": "site/web/src/" });
  });
  it("are empty for a package with no paths", () => {
    expect(tsconfigAliases(at("kit/chat-model"), REPO)).toEqual({});
  });
});

// With no manifest configured the rule has nothing to read, so it reports nothing even
// for an import the configured workspace refuses.
new RuleTester({ languageOptions }).run("oss/open-boundary with no manifest configured", openBoundaryRule, {
  valid: [
    {
      name: "the same import the configured workspace refuses",
      filename: at("site/web/src/App.tsx"),
      code: 'import { GraphPage } from "@/pages/graph/GraphPage";',
    },
  ],
  invalid: [],
});

// A workspace where the configured manifest does not exist is the open repository:
// nothing in it is private, so the rule has nothing to report.
const openRepo = mkdtempSync(join(tmpdir(), "open-boundary-"));
afterAll(() => rmSync(openRepo, { recursive: true, force: true }));
plantIn(openRepo, "pnpm-workspace.yaml", 'packages:\n  - "site/web"\n');
plantIn(openRepo, "site/web/package.json", JSON.stringify({ name: "@alkera/web" }));
plantIn(openRepo, "site/web/tsconfig.json", JSON.stringify({ compilerOptions: { paths: { "@/*": ["src/*"] } } }));
plantIn(openRepo, "site/web/src/pages/graph/GraphPage.tsx");
tester.run("oss/open-boundary in a workspace without the configured manifest", openBoundaryRule, {
  valid: [
    {
      name: "the same import the configured workspace refuses",
      filename: join(openRepo, "site/web/src/App.tsx"),
      code: 'import { GraphPage } from "@/pages/graph/GraphPage";',
    },
  ],
  invalid: [],
});

describe("the manifest setting", () => {
  it.each([
    [{ openBoundary: { manifest: "boundary/groups.toml" } }, "boundary/groups.toml"],
    [{ openBoundary: { manifest: "" } }, null],
    [{ openBoundary: {} }, null],
    [{}, null],
    [undefined, null],
  ])("%j gives %s", (settings, expected) => {
    expect(manifestSetting(settings)).toBe(expected);
  });
});
