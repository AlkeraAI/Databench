// Money factors, the tier ladder and the permission-mode list each have one
// owner module. Any other declaration under the same name is a second copy that
// drifts on its own.

import { describe, expect, it } from "vitest";

import { PERMISSION_MODE_VALUES } from "@alkera/chat-model";

import { EXTRA_ALLOWLIST, constantDeclarations, countPerFile, listLiterals, moneyFactorLiterals, productSources, readSource } from "./scan";

const SCANNED = [
  ...productSources("apps/web/src"),
  ...productSources("packages/ui/src"),
  ...productSources("packages/chat-model/src"),
];

// The owners, plus today's copies. A copy may only be removed.
const DECLARATIONS_ALLOWED: Record<string, string[]> = {
  // Owners.
  "apps/web/src/lib/usageDisplay.ts": ["CREDITS_PER_USD"],
  "packages/chat-model/src/money.ts": ["NANOS_PER_USD"],
  "packages/chat-model/src/permissionPresentation.ts": ["PERMISSION_MODES"],
  ...EXTRA_ALLOWLIST.declarations,
};

describe("owned constant scanner", () => {
  it("finds a declaration, exported or not, and a prefixed copy", () => {
    const source = [
      "export const NANOS_PER_USD = 1_000_000_000;",
      'let CLOUD_PERMISSION_MODES = ["default"];',
      "const TIER_RANK: Record<string, number> = {};",
    ].join("\n");
    expect(constantDeclarations(source)).toEqual(["CLOUD_PERMISSION_MODES", "NANOS_PER_USD", "TIER_RANK"]);
  });

  it("ignores a use or an import", () => {
    const source = 'import { NANOS_PER_USD } from "./money";\nconst usd = nanos / NANOS_PER_USD;';
    expect(constantDeclarations(source)).toEqual([]);
  });
});

describe("each owned constant is declared only in its owner", () => {
  it("only the owners and today's copies declare one", () => {
    const found: Record<string, string[]> = {};
    for (const file of SCANNED) {
      const names = constantDeclarations(readSource(file));
      if (names.length > 0) found[file] = names;
    }
    expect(found).toEqual(DECLARATIONS_ALLOWED);
  });
});

describe("no money conversion spells the nano factor as a literal", () => {
  it("counts a bare factor in code and ignores comments and other figures", () => {
    const source = [
      "const usd = nanos / 1e9;",
      "const seed = String(cap.monthly_cap_nanos / 1_000_000_000);",
      "// USD × 1e9 on the wire, nanos / 1e9 here",
      " *  `per_token_nanos × 1e6 / 1e9`",
      "const plus = 1_000_000_000_000;",
      "const usd2 = nanos / NANOS_PER_USD;",
    ].join("\n");
    expect(moneyFactorLiterals(source)).toBe(2);
  });

  it("no product file converts with one", () => {
    expect(countPerFile(SCANNED, moneyFactorLiterals)).toEqual({});
  });
});

describe("no product file spells the permission-mode list by hand", () => {
  it("counts a list that names every mode, of strings or of options, and nothing shorter", () => {
    const source = [
      'const KNOWN = new Set(["read_only", "default", "auto", "plan", "bypass"]);',
      'const OPTIONS = [',
      '  { value: "default", label: "Default" },',
      '  { value: "plan", label: "Plan" },',
      '  { value: "auto", label: "Auto" },',
      '  { value: "read_only", label: "Read-only" },',
      '  { value: "bypass", label: "Bypass" },',
      '];',
      'const REMEMBERED = new Set(["read_only", "plan", "default"]);',
      'type Mode = "read_only" | "default" | "auto" | "plan" | "bypass";',
    ].join("\n");
    expect(listLiterals(source, PERMISSION_MODE_VALUES)).toBe(2);
  });

  it("reads every mode list from the generated vocabulary", () => {
    // The generated registry is the owner's output, not a copy.
    const product = SCANNED.filter((file) => !file.includes("/generated/"));
    expect(countPerFile(product, (source) => listLiterals(source, PERMISSION_MODE_VALUES))).toEqual({});
  });
});
