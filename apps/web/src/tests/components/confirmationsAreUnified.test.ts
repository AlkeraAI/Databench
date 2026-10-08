import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// ConfirmDialog is the one surface the product uses to ask "are you sure", so every prompt has
// the same tone, the same keys, and guards an irreversible answer the same way. A Modal dressed
// up as a confirmation skipped the primitive, and a new one added anywhere in the app fails here.
//
// It reads source text on purpose. How any single dialog behaves is pinned by its own page's test;
// what no page's test can catch is one more chrome nobody noticed.

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "../..");

function tsxFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) return name === "tests" ? [] : tsxFiles(full);
    return name.endsWith(".tsx") ? [full] : [];
  });
}

/**
 * The props of every `<Modal …>` a file renders. Walks to the tag's own closing `>` while
 * tracking the braces and strings of the expressions inside it, so a prop holding `{a > b}` or
 * `{() => x}` does not end the tag early — a naive `<Modal[^>]*>` cuts at the first of those and
 * would read a confirmation's props as a form's.
 */
function modalProps(source: string): string[] {
  const out: string[] = [];
  const tag = /<Modal[\s\n]/g;
  let hit: RegExpExecArray | null;
  while ((hit = tag.exec(source)) !== null) {
    let depth = 0;
    let quote = "";
    let i = hit.index + hit[0].length;
    for (; i < source.length; i += 1) {
      const c = source[i];
      if (quote) {
        if (c === "\\") i += 1;
        else if (c === quote) quote = "";
        continue;
      }
      if (c === '"' || c === "'" || c === "`") quote = c;
      else if (c === "{") depth += 1;
      else if (c === "}") depth -= 1;
      else if (c === ">" && depth === 0) break;
    }
    out.push(source.slice(hit.index, i));
  }
  return out;
}

/** A Modal styled as a refusal or a deletion — the shape every hand-built confirmation had. */
const TONED = /confirm(?:Variant|Fill)\s*=/;

/** A Modal whose primary action takes something away. A form's action saves, creates or sends;
 *  these verbs belong to a question, and a question belongs in the primitive. */
const TAKES_SOMETHING_AWAY =
  /confirmLabel\s*=\s*[{"'`]\s*[`"']?(?:Delete|Remove|Revoke|Ban|Disconnect|Deactivate|Unenroll|Discard|Empty|Leave)\b/;

/** A local re-implementation of the primitive, by the names the app used before it existed. */
const LOCAL_CONFIRM = /function\s+(?:Confirm(?:Modal|Dialog)|DeleteConfirm|ConfirmBox)\s*\(/;

/** The browser's own modal, which carries the origin in its title and can be switched off per tab. */
const BROWSER_CONFIRM = /(?:^|[^.\w])(?:window\.)?confirm\s*\(/;

const FILES = tsxFiles(SRC);

/** Which files trip a probe, by their path relative to `src/` — the failure names the offender. */
const offenders = (probe: (source: string, path: string) => boolean): string[] =>
  FILES.filter((f) => probe(readFileSync(f, "utf8"), f)).map((f) => relative(SRC, f));

describe("every confirmation goes through the shared dialog", () => {
  it("finds the app's source to scan", () => {
    expect(FILES.length).toBeGreaterThan(100);
    // The walker must actually be finding tags, or every probe below passes vacuously.
    expect(FILES.flatMap((f) => modalProps(readFileSync(f, "utf8"))).length).toBeGreaterThan(10);
  });

  it("no page gives a Modal a confirmation's colours", () => {
    expect(offenders((s) => modalProps(s).some((p) => TONED.test(p)))).toEqual([]);
  });

  it("no Modal's primary action takes something away", () => {
    expect(offenders((s) => modalProps(s).some((p) => TAKES_SOMETHING_AWAY.test(p)))).toEqual([]);
  });

  it("no page re-implements the primitive under its own name", () => {
    expect(offenders((s) => LOCAL_CONFIRM.test(s))).toEqual([]);
  });

  it("nothing asks through the browser's own confirm", () => {
    // Comments explain WHY the browser's dialog is refused; only live code counts.
    const code = (s: string) =>
      s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|\n)\s*\/\/[^\n]*/g, "$1");
    expect(offenders((s) => BROWSER_CONFIRM.test(code(s)))).toEqual([]);
  });
});
