// Source scanning for the architecture gates in this directory. Each gate reads
// the TypeScript sources as text, so the helpers here are pure: a string in, the
// findings out. The gates then hold the whole tree to an allowlist of today's
// findings that may only shrink.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../..");

/** Findings a composed checkout adds to the open allowlists. The file sits beside
 *  this one only where the composing side provides it; the open tree has none. */
export interface ExtraAllowlist {
  roleBranches?: Record<string, number>;
  ownerIdComparisons?: Record<string, number>;
  declarations?: Record<string, string[]>;
  schemaShadows?: Record<string, string[]>;
  rawErrorMessageDirs?: string[];
  rawErrorMessageCeiling?: number;
  statusBranches?: Record<string, number>;
  openOwnerDirs?: string[];
}

const EXTRA_MODULES = import.meta.glob<ExtraAllowlist>("./productAllowlist.json", {
  eager: true,
  import: "default",
});

/** The composing side's extra allowlist entries, or none. */
export const EXTRA_ALLOWLIST: ExtraAllowlist = Object.values(EXTRA_MODULES)[0] ?? {};

/** Test sources (the suite, co-located tests, the setup file) are not product code. */
export function isTestSource(path: string): boolean {
  return (
    /(^|\/)src\/tests\//.test(path) ||
    /\.(test|spec)\.tsx?$/.test(path) ||
    /(^|\/)test-setup\.ts$/.test(path)
  );
}

/** Every product `.ts` / `.tsx` file under `dir` (repo-relative), sorted. */
export function productSources(dir: string): string[] {
  const walk = (abs: string): string[] =>
    readdirSync(abs).flatMap((name) => {
      if (name === "node_modules") return [];
      const child = join(abs, name);
      if (statSync(child).isDirectory()) return walk(child);
      return /\.tsx?$/.test(name) ? [relative(REPO_ROOT, child)] : [];
    });
  return walk(join(REPO_ROOT, dir))
    .filter((path) => !isTestSource(path))
    .sort();
}

export function readSource(path: string): string {
  return readFileSync(join(REPO_ROOT, path), "utf8");
}

/** `{ file: count }` over `files`, keeping only files with a nonzero count. */
export function countPerFile(files: string[], count: (source: string) => number): Record<string, number> {
  const found: Record<string, number> = {};
  for (const file of files) {
    const n = count(readSource(file));
    if (n > 0) found[file] = n;
  }
  return found;
}

// ---------------------------------------------------------------------------
// Role branches

/** Files sharing rungs, as spelled on the wire (alkera_core.files.authz). */
export const FILES_ROLES = ["reader", "commenter", "writer", "manager", "owner"] as const;
/** Team roles (alkera_core.models._enums.TeamRole). */
export const TEAM_ROLES = ["member", "admin"] as const;
/** Platform staff roles (alkera_core.models._enums.PlatformRole). */
export const PLATFORM_ROLES = ["alkera_support", "alkera_admin"] as const;

export const ROLE_WORDS: readonly string[] = [...FILES_ROLES, ...TEAM_ROLES, ...PLATFORM_ROLES];

const ROLE_ALTERNATION = [...ROLE_WORDS].sort().join("|");
const EQ = "(?:===|!==|==|!=)";

/** An equality against a role literal (either side), or a `case "admin":` arm.
 *  Rendering a role the server sent is display; branching on it is a decision. */
const ROLE_BRANCH = new RegExp(
  `(?:${EQ}\\s*(['"\`])(${ROLE_ALTERNATION})\\1)` +
    `|(?:(['"\`])(${ROLE_ALTERNATION})\\3\\s*${EQ})` +
    `|(?:\\bcase\\s+(['"\`])(${ROLE_ALTERNATION})\\5\\s*:)`,
  "g",
);

/** Every role word `source` branches on, one entry per branch. */
export function roleBranches(source: string): string[] {
  return [...source.matchAll(ROLE_BRANCH)].map((m) => m[2] ?? m[4] ?? m[6]);
}

/** An ownership decision made in the browser: `owner_user_id` compared to a value. */
const OWNER_ID_COMPARISON = new RegExp(
  `\\bowner_user_id\\s*${EQ}|${EQ}\\s*[\\w.?!()[\\]]*\\bowner_user_id\\b`,
  "g",
);

export function ownerIdComparisons(source: string): number {
  return [...source.matchAll(OWNER_ID_COMPARISON)].length;
}

/** A `can_*` field read so that a missing answer is a yes: `x.can_delete !==
 *  false`, `false !== x.can_send`, `raw.can_switch ?? true`. */
const FAIL_OPEN_CAPABILITY = new RegExp(
  String.raw`\bcan_\w+\s*(?:!==?\s*false\b|\?\?\s*true\b)|\bfalse\s*!==?\s*[\w$.?]*\bcan_\w+`,
  "g",
);

export function failOpenCapabilities(source: string): number {
  return [...source.matchAll(FAIL_OPEN_CAPABILITY)].length;
}

// ---------------------------------------------------------------------------
// Hand-typed wire shapes

/** The names of `components["schemas"]` in the generated `schema.d.ts`. */
export function schemaNames(schemaDts: string): Set<string> {
  const start = schemaDts.indexOf("\n    schemas: {\n");
  if (start < 0) throw new Error("schema.d.ts has no components.schemas block");
  const names = new Set<string>();
  for (const line of schemaDts.slice(start + "\n    schemas: {\n".length).split("\n")) {
    if (/^ {4}\S/.test(line)) break;
    const m = /^ {8}"?([A-Za-z_][\w.-]*)"?\??:/.exec(line);
    if (m) names.add(m[1]);
  }
  return names;
}

/** Exported type names in `source` that are named like a generated schema and
 *  are not a plain alias of that same schema (`= components["schemas"]["X"]`
 *  or a local `Schemas["X"]`). */
export function shadowedSchemaTypes(source: string, schemas: Set<string>): string[] {
  const found: string[] = [];
  const decl = /^export\s+(?:declare\s+)?(?:type|interface|enum|class)\s+([A-Za-z_]\w*)/gm;
  for (const m of source.matchAll(decl)) {
    const name = m[1];
    if (!schemas.has(name)) continue;
    const alias = new RegExp(
      `^export\\s+type\\s+${name}\\s*=\\s*(?:components\\s*\\[\\s*"schemas"\\s*\\]|Schemas)\\s*\\[\\s*"${name}"\\s*\\]\\s*;`,
    );
    if (!alias.test(source.slice(m.index))) found.push(name);
  }
  return found.sort();
}

// ---------------------------------------------------------------------------
// Named constants spelled once

// The tier ranking and the top-up floor have no owner here: the server sends the plan-change verdict
// and the floor, so any declaration of either is a second copy of a server rule.
export const OWNED_CONSTANTS = [
  "NANOS_PER_USD",
  "CREDITS_PER_USD",
  "TIER_RANK",
  "PERMISSION_MODES",
  "TOPUP_MIN_USD",
  "TOPUP_MIN_CENTS",
] as const;

/** How many times `source` multiplies or divides by a bare nano-USD factor
 *  (`1e9`, `1_000_000_000`) in code; comment lines are skipped. A conversion
 *  goes through `NANOS_PER_USD` or a converter in `@alkera/chat-model`. */
export function moneyFactorLiterals(source: string): number {
  const literal = /[\w)\]]\s*[*/]\s*(?:1e9|1_000_000_000|1000000000)\b/g;
  return source
    .split("\n")
    .filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line))
    .reduce((n, line) => n + (line.match(literal)?.length ?? 0), 0);
}

/** How many array literals in `source` spell every one of `values` as a quoted
 *  string: a hand copy of an owned list (the permission modes, say), whatever
 *  the constant holding it is called. Only innermost brackets are read, so a
 *  list of objects (`[{ value: "plan", label: "Plan" }, …]`) counts too. */
export function listLiterals(source: string, values: readonly string[]): number {
  const literals = source.match(/\[[^[\]]*\]/g) ?? [];
  return literals.filter((literal) => values.every((value) => new RegExp(`["'\`]${value}["'\`]`).test(literal))).length;
}

/** Each owned constant `source` declares, including prefixed copies such as
 *  `CLOUD_PERMISSION_MODES`. */
export function constantDeclarations(source: string): string[] {
  const names = OWNED_CONSTANTS.join("|");
  const decl = new RegExp(`\\b(?:const|let|var)\\s+([A-Z_]*(?:${names}))\\b`, "g");
  return [...source.matchAll(decl)].map((m) => m[1]).sort();
}

// ---------------------------------------------------------------------------
// Status words mapped in the browser

/** The fields a server status word arrives in. */
const STATUS_FIELDS =
  "state|status|machine_status|session_state|mirror_state|turn_state|credit_state|scan_state|sandbox_state|stop_reason|reason_code";

/** An equality between a status field and a string literal (either side). A
 *  numeric HTTP status (`error.status === 404`) is not a status word. */
const STATUS_EQUALITY = new RegExp(
  `\\b(?:${STATUS_FIELDS})\\s*${EQ}\\s*(['"\`])[a-z_]+\\1` + `|(['"\`])[a-z_]+\\2\\s*${EQ}\\s*[\\w.?!]*\\b(?:${STATUS_FIELDS})\\b`,
  "g",
);

/** A `switch` over a status field. */
const STATUS_SWITCH = new RegExp(`\\bswitch\\s*\\(\\s*[\\w.?!]*\\b(?:${STATUS_FIELDS})\\s*\\)`, "g");

/** A lookup table keyed by a status type: `Record<MachineState, ...>`. */
const STATUS_TABLE = /\bRecord<\s*(?:Exclude<\s*)?\w*(?:State|Status)\b/g;

/** Every place `source` turns a server status word into a decision the UI
 *  makes itself (copy, a tone, a control): one entry per site. Passing the
 *  word through (`data-state={status.state}`) is not one. */
export function statusBranches(source: string): string[] {
  return [
    ...[...source.matchAll(STATUS_EQUALITY)].map((m) => m[0]),
    ...[...source.matchAll(STATUS_SWITCH)].map((m) => m[0]),
    ...[...source.matchAll(STATUS_TABLE)].map((m) => m[0]),
  ];
}
