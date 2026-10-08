// One store, one door.
//
// `useColorScheme` was fixed for the private-window crash and `useScopeToggle`
// was not, because nothing held the line: a second hook reached for the global
// and no test, no type and no lint rule noticed. ESLint's
// `no-restricted-globals` covers `apps/web`, which is the only tree with a lint
// config; this covers all three, so the shared library and the conversation
// model are held to the same rule.

import { readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const REPO_ROOT = resolve(__dirname, "../../..");

/** The trees the browser runs. */
const SCANNED = ["apps/web/src", "packages/ui/src", "packages/chat-model/src"];

/**
 * The only files allowed to name a store.
 *
 * `storage.ts` IS the door. The two test setups install a working store where
 * the runner has none, and a test that proves the guard has to be able to take
 * one away — so tests are exempt, product code never is.
 */
const ALLOWED = new Set(["packages/ui/src/storage.ts"]);
const ALLOWED_SUFFIXES = ["test-setup.ts"];

const STORES = /\b(localStorage|sessionStorage|indexedDB)\b/;

/**
 * The key families that hold org data: what this browser remembers about one
 * person's work in one org. A person can switch orgs in the same browser, so
 * every one of these keys has to name both, and the only way to build one is
 * `accountKey(userId, orgId, family)`. A family belongs here when what it stores
 * was made in an org and must not be offered in another (a chat, a queued
 * message, an upload, a stashed edit, a notice id). Preferences about the
 * machine (`size`, theme) are not org data and stay off it.
 */
const ORG_DATA_FAMILIES = [
  "chat.last",
  "chat.draftPicks",
  "chat.queued",
  "chat.layout",
  "files.uploads",
  "crdt.unsent",
  "billing.noticesSeen",
];

const ORG_DATA_LITERAL = new RegExp(
  `["'\`](alkera\\.(?:${ORG_DATA_FAMILIES.map((f) => f.replace(/\./g, "\\.")).join("|")}))(?![A-Za-z0-9_])`,
  "g",
);

/** Every string or template literal that spells an org-data key by hand. A key
 *  built through `accountKey` names only the family (`"chat.last"`), never the
 *  `alkera.` head, so any literal carrying the head is one that bypassed it. */
function rawOrgDataKeys(code: string): string[] {
  return [...code.matchAll(ORG_DATA_LITERAL)].map((match) => match[1] ?? "");
}

/** Comments are prose: a file may explain why it does not reach for the global. */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`\\])\/\/[^\n]*/g, "$1");
}

function isTest(path: string): boolean {
  return /\.(test|spec)\.[cm]?[jt]sx?$/.test(path) || path.includes("/src/tests/");
}

function sourceFiles(root: string): string[] {
  const found: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== "node_modules" && entry.name !== "generated") walk(path);
      } else if (/\.[cm]?[jt]sx?$/.test(entry.name)) {
        found.push(path);
      }
    }
  };
  walk(root);
  return found;
}

describe("the browser stores are reached through one guarded module", () => {
  it("scans every browser source tree", () => {
    // A scan that found nothing to look at would pass forever.
    const counted = SCANNED.map((tree) => sourceFiles(join(REPO_ROOT, tree)).length);
    for (const count of counted) expect(count).toBeGreaterThan(5);
  });

  it("finds no raw store outside the module that guards it", () => {
    const offenders: string[] = [];
    for (const tree of SCANNED) {
      for (const path of sourceFiles(join(REPO_ROOT, tree))) {
        const rel = relative(REPO_ROOT, path);
        if (ALLOWED.has(rel) || ALLOWED_SUFFIXES.some((end) => rel.endsWith(end))) continue;
        if (isTest(rel)) continue;
        const code = stripComments(readFileSync(path, "utf8"));
        const hit = STORES.exec(code);
        if (hit) offenders.push(`${rel}: ${hit[1]}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("finds no org-data key spelled by hand outside accountKey", () => {
    const offenders: string[] = [];
    for (const tree of SCANNED) {
      for (const path of sourceFiles(join(REPO_ROOT, tree))) {
        const rel = relative(REPO_ROOT, path);
        if (isTest(rel)) continue;
        const code = stripComments(readFileSync(path, "utf8"));
        for (const hit of rawOrgDataKeys(code)) offenders.push(`${rel}: ${hit}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("would catch an org-data key spelled by hand if one came back", () => {
    // The scan is only worth its green if the predicate actually fires.
    expect(rawOrgDataKeys(stripComments('const key = "alkera.chat.last:" + user;'))).toEqual([
      "alkera.chat.last",
    ]);
    expect(rawOrgDataKeys(stripComments("const key = `alkera.chat.queued:${chatId}`;"))).toEqual([
      "alkera.chat.queued",
    ]);
    expect(rawOrgDataKeys(stripComments("export const UPLOAD_STORAGE_KEY = 'alkera.files.uploads';"))).toEqual([
      "alkera.files.uploads",
    ]);
    // The same family built the one sanctioned way is not a hit.
    expect(rawOrgDataKeys(stripComments('const key = accountKey(user, org, "chat.last");'))).toEqual([]);
    // Nor is prose about it, a family that is not org data, or a longer name
    // that only starts the same way.
    expect(rawOrgDataKeys(stripComments('// was "alkera.chat.last:<user>"'))).toEqual([]);
    expect(rawOrgDataKeys(stripComments('const k = "alkera.size:rail";'))).toEqual([]);
    expect(rawOrgDataKeys(stripComments('const k = "alkera.chat.lastSeen";'))).toEqual([]);
  });

  it("would catch a raw store if one came back", () => {
    // The scan is only worth its green if the predicate actually fires.
    expect(STORES.test(stripComments("const v = window.localStorage.getItem('k');"))).toBe(true);
    expect(STORES.test(stripComments("const db = indexedDB.open('x');"))).toBe(true);
    expect(STORES.test(stripComments("// localStorage throws in a private window"))).toBe(false);
    expect(STORES.test(stripComments("/** reads localStorage */"))).toBe(false);
  });
});
