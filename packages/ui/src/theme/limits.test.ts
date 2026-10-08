import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { defaultRetryDelayMs, UPLOAD_ATTEMPTS } from "../chat/composer/uploads";
import {
  UPLOAD_ATTEMPTS as LIMIT_UPLOAD_ATTEMPTS,
  UPLOAD_RETRY_BASE_MS,
} from "./limits";

// `theme/limits.ts` is where the UI library writes down the numbers it picks for
// itself. These cases hold both halves of that: the files it was lifted out of
// carry no replacement literal, and the decisions genuinely turn on the values
// here.

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");

/** The files this module emptied of bare numbers. */
const DE_MAGICKED = ["preview/defaults.ts", "chat/composer/uploads.ts"];

/** 0, 1, -1 and 2 are counts and steps; 100 is a percentage; 1000 and 1024 are
 *  the two byte units a size is rendered in. None of them is a choice anyone
 *  makes. A priority band is an ordering, not a limit, and is named where it is
 *  declared. */
function isAllowed(literal: string): boolean {
  return [0, 1, 2, 100, 1000, 1024].includes(Number(literal.replace(/_/g, "")));
}

function bareNumbers(text: string, extra: RegExp[] = []): string[] {
  let stripped = text
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1 ")
    .replace(/`(?:[^`\\]|\\.)*`/g, '""')
    .replace(/'(?:[^'\\\n]|\\.)*'/g, '""')
    .replace(/"(?:[^"\\\n]|\\.)*"/g, '""');
  for (const pattern of extra) stripped = stripped.replace(pattern, " ");
  return (stripped.match(/(?<![\w.$])\d[\d_]*(?![\w.])/g) ?? []).filter((n) => !isAllowed(n));
}

/** The renderer-priority table: an ordering among renderers, which has nothing to
 *  tune and no meaning outside this file. */
const PRIORITY_TABLE = /const PRIORITY = \{[\s\S]*?\} as const;/;

describe("the de-magicked files keep no numbers of their own", () => {
  it("preview/defaults.ts", () => {
    const source = readFileSync(join(SRC, "preview/defaults.ts"), "utf8");
    expect(bareNumbers(source, [PRIORITY_TABLE])).toEqual([]);
  });

  it("chat/composer/uploads.ts", () => {
    expect(bareNumbers(readFileSync(join(SRC, "chat/composer/uploads.ts"), "utf8"))).toEqual([]);
  });

  it("the scan would catch a number put back", () => {
    expect(bareNumbers("const TEXT_MAX_BYTES = 2_097_152;")).toEqual(["2_097_152"]);
    expect(bareNumbers("return 400 * 2 ** (attempt - 2);")).toEqual(["400"]);
    expect(bareNumbers("// 64 MiB of it\nconst x = LIMIT;")).toEqual([]);
  });

  it("every file it claims to cover exists", () => {
    for (const relPath of DE_MAGICKED) expect(readFileSync(join(SRC, relPath), "utf8")).toBeTruthy();
  });
});

describe("the upload ladder is the one the limits state", () => {
  it("the retry delay doubles from the stated base", () => {
    expect(defaultRetryDelayMs(2)).toBe(UPLOAD_RETRY_BASE_MS);
    expect(defaultRetryDelayMs(3)).toBe(UPLOAD_RETRY_BASE_MS * 2);
    expect(defaultRetryDelayMs(4)).toBe(UPLOAD_RETRY_BASE_MS * 4);
  });

  it("the composer's attempt count is the limit, not a second copy of it", () => {
    expect(UPLOAD_ATTEMPTS).toBe(LIMIT_UPLOAD_ATTEMPTS);
  });
});
