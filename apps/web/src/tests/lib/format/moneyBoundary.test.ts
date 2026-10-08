import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// Money has one reading on every page a customer sees: `formatUsd` /
// `formatUsdNanos` / `formatPriceUsd` / `formatLedger*` from @alkera/chat-model
// (two decimals, half-up, "<$0.01" below half a cent). More precision is an admin
// reading — the model catalog's per-token prices, a compute rate per minute — and
// lives in `pages/platform/admin/shared/precise.ts`.
//
// These scans pin both halves of that: the precise module is imported only from
// the admin console, and no user-facing module formats a currency amount of its
// own, which is how six decimals reached the org billing page before.

const here = dirname(fileURLToPath(import.meta.url));
const WEB_SRC = resolve(here, "../../..");
const REPO = resolve(WEB_SRC, "../../..");
const UI_SRC = join(REPO, "packages/ui/src");
const CHAT_MODEL_SRC = join(REPO, "packages/chat-model/src");

const ADMIN_ROOT = join(WEB_SRC, "pages", "platform") + sep;
const PRECISE_MODULE = join(WEB_SRC, "pages/platform/admin/shared/precise.ts");

function sources(root: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(root)) {
    const path = join(root, name);
    if (statSync(path).isDirectory()) {
      if (name === "tests" || name === "node_modules" || name === "generated") continue;
      out.push(...sources(path));
    } else if (/\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name)) {
      out.push(path);
    }
  }
  return out;
}

const IMPORT_SPEC = /(?:from|import)\s*\(?\s*["']([^"']+)["']/g;

/** Every module a file imports, resolved to a path when it is relative or `@/`. */
function importsOf(file: string, text = readFileSync(file, "utf8")): string[] {
  return [...text.matchAll(IMPORT_SPEC)].map(([, spec]) => {
    if (spec.startsWith("@/")) return join(WEB_SRC, spec.slice(2));
    if (spec.startsWith(".")) return resolve(dirname(file), spec);
    return spec;
  });
}

const importsPrecise = (file: string, text?: string): boolean =>
  importsOf(file, text).some((target) => target === PRECISE_MODULE || `${target}.ts` === PRECISE_MODULE);

describe("the precise money reading is admin-only", () => {
  it("is imported only from the admin console", () => {
    const offenders = [...sources(WEB_SRC), ...sources(UI_SRC), ...sources(CHAT_MODEL_SRC)]
      .filter((file) => !file.startsWith(ADMIN_ROOT))
      .filter((file) => importsPrecise(file))
      .map((file) => relative(REPO, file));
    expect(offenders).toEqual([]);
  });

  it("is imported by the model catalog, so the scan above has something to find", () => {
    const users = sources(ADMIN_ROOT).filter((file) => importsPrecise(file)).map((file) => relative(WEB_SRC, file));
    expect(users).toContain(join("pages/platform/admin/catalog/ModelCard.tsx"));
  });

  it.each([
    'import { preciseUsd } from "../../platform/admin/shared/precise";',
    'import { perMillionUsd } from "@/pages/platform/admin/shared/precise";',
    'const m = await import("../../platform/admin/shared/precise.ts");',
  ])("would catch a billing page that did: %s", (line) => {
    const page = join(WEB_SRC, "pages/organization/billing/OrgBillingPage.tsx");
    expect(importsPrecise(page, line)).toBe(true);
  });
});

// A currency amount formatted by hand: an Intl currency formatter, or a "$" glued
// to toFixed / toLocaleString. The one allowed exception is the analytics chart's
// compact thousands tick ("$10K"), which the money rule has no reading for.
const HAND_FORMATTED_MONEY = [
  /style:\s*["']currency["']/,
  /\$\$\{[^}]*\.(toFixed|toLocaleString)\(/,
];
const COMPACT_TICK_EXCEPTION = join(WEB_SRC, "pages/workspace/analytics/orgUsageData.ts");

describe("user-facing money goes through the one formatter", () => {
  it("no user-facing module formats a currency amount of its own", () => {
    const offenders = [...sources(WEB_SRC), ...sources(UI_SRC), ...sources(CHAT_MODEL_SRC)]
      .filter((file) => !file.startsWith(ADMIN_ROOT) && file !== COMPACT_TICK_EXCEPTION)
      .filter((file) => {
        const text = readFileSync(file, "utf8");
        return HAND_FORMATTED_MONEY.some((pattern) => pattern.test(text));
      })
      .map((file) => relative(REPO, file));
    expect(offenders).toEqual([]);
  });

  it.each([
    'new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" })',
    "const USD = (value: number): string => `$${value.toFixed(2)}`;",
    "`$${n.toLocaleString()}`",
  ])("recognises a hand-formatted amount: %s", (line) => {
    expect(HAND_FORMATTED_MONEY.some((pattern) => pattern.test(line))).toBe(true);
  });
});
