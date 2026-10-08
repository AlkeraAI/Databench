// The chat's palette IS the shell's palette.
//
// Every ground, ink and hairline reads @alkera/ui's own token layer, so the
// chat never reads as a different product inside the portal, and a scheme
// change moves both at once.
//
// jsdom resolves neither a stylesheet's cascade nor `color-mix`, so this suite
// does what a browser would do instead: it reads BOTH token files, builds the
// variable table each scheme actually presents to `.chat-root`, and resolves the
// chat's names through it. What it asserts is therefore the resolved colour a
// reader sees — not the spelling of a declaration.

import { readFileSync, readdirSync } from "node:fs";
// `resolve` below is this suite's var() expander, so the path helper is aliased.
import { join, resolve as resolvePath } from "node:path";

import { describe, expect, it } from "vitest";

/** The package root. Vitest runs from it, and the suite reads the REAL files —
 *  a token table pasted into a test proves nothing about the one the browser
 *  loads. (A `?raw` glob cannot serve here: Vite hands CSS to its own pipeline
 *  and the raw text comes back empty.) */
const SRC = resolvePath(process.cwd(), "src");

/** A stylesheet with its comments removed. Prose about a colour is not a
 *  colour, and a selector that trails a comment is still that selector. */
function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

function sheet(relative: string): string {
  return stripComments(readFileSync(join(SRC, relative), "utf8"));
}

/** Every plain stylesheet under a directory, as (path, source). */
function sheetsUnder(relative: string): [string, string][] {
  const root = join(SRC, relative);
  const out: [string, string][] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (entry.name.endsWith(".css")) out.push([path, readFileSync(path, "utf8")]);
    }
  };
  walk(root);
  return out;
}

type Vars = Record<string, string>;

/** Every top-level rule in a stylesheet, as (selector, body) — nested at-rules
 *  are skipped whole, which is what we want: the chat's only nested block is the
 *  reduced-motion override, which declares no colour. */
function topLevelRules(css: string): { selector: string; body: string }[] {
  const out: { selector: string; body: string }[] = [];
  let depth = 0;
  let start = 0;
  let selector = "";
  for (let i = 0; i < css.length; i += 1) {
    const ch = css[i];
    if (ch === "{") {
      if (depth === 0) {
        selector = css.slice(start, i).trim();
        start = i + 1;
      }
      depth += 1;
    } else if (ch === "}") {
      depth -= 1;
      if (depth === 0) {
        out.push({ selector, body: css.slice(start, i) });
        start = i + 1;
      }
    }
  }
  return out;
}

/** The custom properties a rule declares. Values keep their `var()` calls; the
 *  resolver below expands them. */
function customProps(body: string): Vars {
  const vars: Vars = {};
  // Declarations are separated by `;` at paren depth 0 — a color-mix() carries
  // commas and its own parens, so a naive split would cut one in half.
  let depth = 0;
  let start = 0;
  const parts: string[] = [];
  for (let i = 0; i < body.length; i += 1) {
    const ch = body[i];
    if (ch === "(") depth += 1;
    else if (ch === ")") depth -= 1;
    else if (ch === ";" && depth === 0) {
      parts.push(body.slice(start, i));
      start = i + 1;
    }
  }
  parts.push(body.slice(start));
  for (const raw of parts) {
    const decl = raw.replace(/\/\*[\s\S]*?\*\//g, "").trim();
    if (!decl.startsWith("--")) continue;
    const colon = decl.indexOf(":");
    if (colon < 0) continue;
    vars[decl.slice(0, colon).trim()] = decl.slice(colon + 1).trim();
  }
  return vars;
}

function ruleVars(css: string, matches: (selector: string) => boolean): Vars {
  const vars: Vars = {};
  for (const rule of topLevelRules(css)) {
    if (matches(rule.selector)) Object.assign(vars, customProps(rule.body));
  }
  return vars;
}

const shellCss = sheet("theme/tokens.css");
const chatCss = sheet("chat/theme/tokens.css");

/** The shell's dark scheme is its `:root` base; light is that base with the
 *  light block's overrides on top — exactly how the cascade presents it. */
const shellDark = ruleVars(shellCss, (s) => s.includes(":root") && !s.includes('="light"'));
const shellLight = {
  ...shellDark,
  ...ruleVars(shellCss, (s) => s.includes('data-alkera-color-scheme="light"')),
};

const chatBase = ruleVars(chatCss, (s) => s === ".chat-root");
const chatDark = ruleVars(chatCss, (s) => s === '.chat-root[data-theme="dark"]');

const table = {
  /** What `.chat-root[data-theme="dark"]` inside a dark portal presents. */
  dark: { ...shellDark, ...chatBase, ...chatDark } as Vars,
  /** What a bare `.chat-root` inside a light portal presents. */
  light: { ...shellLight, ...chatBase } as Vars,
};

/** Expand `var(--name, fallback)` against a variable table, the way the browser
 *  substitutes at computed-value time. An undeclared name falls back; an
 *  undeclared name with no fallback is an error the test should see. */
function resolve(value: string, vars: Vars, seen = new Set<string>()): string {
  const at = value.indexOf("var(");
  if (at < 0) return value.trim();
  let depth = 0;
  let end = -1;
  for (let i = at + 3; i < value.length; i += 1) {
    if (value[i] === "(") depth += 1;
    else if (value[i] === ")") {
      depth -= 1;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  if (end < 0) throw new Error(`unbalanced var() in ${value}`);
  const inner = value.slice(at + 4, end);
  let split = -1;
  depth = 0;
  for (let i = 0; i < inner.length; i += 1) {
    if (inner[i] === "(") depth += 1;
    else if (inner[i] === ")") depth -= 1;
    else if (inner[i] === "," && depth === 0) {
      split = i;
      break;
    }
  }
  const name = (split < 0 ? inner : inner.slice(0, split)).trim();
  const fallback = split < 0 ? null : inner.slice(split + 1).trim();
  if (seen.has(name)) throw new Error(`${name} resolves through itself`);
  const declared = vars[name];
  let substituted: string;
  if (declared !== undefined) {
    substituted = resolve(declared, vars, new Set([...seen, name]));
  } else if (fallback !== null) {
    substituted = resolve(fallback, vars, seen);
  } else {
    throw new Error(`${name} is not declared and carries no fallback`);
  }
  return resolve(value.slice(0, at) + substituted + value.slice(end + 1), vars, seen);
}

const read = (scheme: "dark" | "light", name: string): string =>
  resolve(`var(${name})`, table[scheme]);

/** Every chat name that must land on a shell name, and the shell name it must
 *  land on. This is the mapping the surface's belonging rests on. */
const GROUND: [chat: string, shell: string][] = [
  ["--chat-paper", "--alkPageBg"],
  ["--chat-paper-raised", "--alkCardBg"],
  ["--chat-composer-fill", "--alkControlBg"],
  ["--chat-ink", "--alkPrimaryText"],
  ["--chat-ink-hover", "--alkPrimaryText"],
  ["--chat-ink-2", "--alkSecondaryText"],
  ["--chat-ink-4", "--alkPlaceholderText"],
  ["--chat-hairline", "--alkBorder"],
  ["--chat-hairline-strong", "--alkBorderStrong"],
  ["--chat-wash", "--alkHoverBg"],
  ["--chat-forest", "--alkBrandText"],
  ["--chat-forest-bright", "--alkBrand"],
  ["--chat-blue", "--alkInfoText"],
  ["--chat-done", "--alkInfo"],
  ["--chat-danger", "--alkDangerText"],
  ["--chat-warn", "--alkWarningText"],
];

/** Literals of a separate chat palette the chat must not ship. None may appear
 *  in any sheet in the package. */
const RETIRED = [
  "#101910",
  "#18231a",
  "#0b120b",
  "#212e23",
  "#e7ece6",
  "#14201a",
  "#1a2118",
  "#3f5f42",
  "#7ccf81",
];

describe.each(["dark", "light"] as const)("the chat's %s palette", (scheme) => {
  it.each(GROUND)("%s is the shell's %s", (chatName, shellName) => {
    expect(read(scheme, chatName)).toBe(read(scheme, shellName));
  });

  it("paints the page ground the shell paints, not a green of its own", () => {
    expect(read(scheme, "--chat-paper")).toBe(scheme === "dark" ? "#1b1a17" : "#f4f3f0");
  });

  it("inks the surface with the shell's own primary text", () => {
    expect(read(scheme, "--chat-ink")).toBe(scheme === "dark" ? "#ece9e2" : "#1b1a17");
  });

  it("steps the inset and the fence off the ground they sit on", () => {
    // Both are washes of the ink over their own ground, so they cannot be a
    // colour of their own — whatever the shell's ground is, they follow it.
    expect(read(scheme, "--chat-paper-inset")).toContain(read(scheme, "--chat-paper"));
    expect(read(scheme, "--chat-fence-ground")).toContain(read(scheme, "--chat-paper-raised"));
  });

  it("carries a third ink tier the shell does not name, built from its second", () => {
    const ink3 = read(scheme, "--chat-ink-3");
    expect(ink3).toContain(read(scheme, "--alkSecondaryText"));
    expect(ink3).not.toBe(read(scheme, "--alkSecondaryText"));
  });
});

describe("what the dark attribute is still allowed to carry", () => {
  it("forks no ground and no ink — the shell's layer already did", () => {
    const grounds = GROUND.map(([chatName]) => chatName);
    expect(Object.keys(chatDark).filter((name) => grounds.includes(name))).toEqual([]);
  });

  it("forks only the presences a dark ground measures differently", () => {
    expect(Object.keys(chatDark).sort()).toEqual(["--chat-edge", "--chat-hover"]);
  });
});

describe("the retired green palette", () => {
  it.each(RETIRED)("%s appears in no chat stylesheet", (literal) => {
    // The package's own sheets, read as one document: a literal that came back
    // in a component sheet would be just as wrong as one here.
    // Every sheet the chat concern ships, read as one document — a literal that
    // came back in a component sheet would be just as wrong as one in the token
    // file. The count is asserted so the net can never silently empty.
    const scanned = sheetsUnder("chat");
    expect(scanned.length).toBeGreaterThan(30);
    const hits = scanned
      .filter(([, css]) => stripComments(css).toLowerCase().includes(literal))
      .map(([path]) => path.slice(SRC.length + 1));
    expect(hits).toEqual([]);
  });
});
