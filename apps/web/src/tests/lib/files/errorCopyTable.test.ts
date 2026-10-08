/**
 * Every code the Files routes can put on the wire has a sentence a person can read.
 *
 * The catalogue is read from the server's own `KNOWN_CODES` rather than copied here,
 * so a change that adds a refusal code to the library and no row to the browser's table
 * fails this pin instead of shipping a blank screen. The second half is the harder
 * rule: a row that exists but repeats the machine's vocabulary is not copy — a person
 * cannot act on "etag", "epoch" or a uuid — so each sentence is read for jargon.
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/errors";
import { filesErrorCopy } from "@/lib/files/errors";

const RELATIVE = join("packages", "api-core", "alkera_core", "files", "errors.py");

/** The library's errors module, found by walking up from wherever vitest was started. */
function errorsModulePath(): string {
  let dir = process.cwd();
  for (;;) {
    const candidate = join(dir, RELATIVE);
    if (existsSync(candidate)) return candidate;
    const parent = dirname(dir);
    if (parent === dir) throw new Error(`could not find ${RELATIVE} above ${process.cwd()}`);
    dir = parent;
  }
}

const ERRORS_PY = errorsModulePath();

/** The `files.*` literals inside the catalogue block of the library's errors module. */
function knownCodes(): string[] {
  const source = readFileSync(ERRORS_PY, "utf8");
  const start = source.indexOf("CONFLICT_CODES: Final");
  const end = source.indexOf("KNOWN_CODE_PREFIXES");
  expect(start).toBeGreaterThan(-1);
  expect(end).toBeGreaterThan(start);
  const block = source.slice(start, end);
  const codes = [...block.matchAll(/"(files\.[a-z0-9_.]+)"/g)].map((match) => match[1]);
  return [...new Set(codes)].sort();
}

/** Words that belong to the wire, never to a sentence a customer reads. */
const JARGON = /\b(etag|epoch|idempotenc\w*|fenced|uuid|409|412|507|null|nul\b)/i;
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

const CODES = knownCodes();

/** The status each family answers with, so the copy is asked for the way it arrives. */
function statusFor(code: string): number {
  if (code.startsWith("files.quota")) return 507;
  if (code === "files.not_found") return 404;
  if (code === "files.forbidden") return 403;
  if (code === "files.precondition_failed") return 412;
  return 409;
}

describe("the Files refusal copy table", () => {
  it("reads a non-trivial catalogue out of the library", () => {
    expect(CODES.length).toBeGreaterThan(50);
    expect(CODES).toContain("files.leased");
    expect(CODES).toContain("files.frozen");
  });

  it.each(CODES)("%s has a sentence a person can act on", (code) => {
    const copy = filesErrorCopy(new ApiError(statusFor(code), { error: { code, message: "" } }));

    // A row that is missing falls through to the envelope's own message — which, for a
    // body carrying no sentence, is the client's synthesized "Request failed (409)" and
    // carries no detail at all. That pair is exactly what a missing row looks like.
    expect(copy.title).not.toMatch(/Request failed/);
    expect(copy.title).not.toBe("");
    // Copy, not a code: a sentence never contains the machine-readable reason.
    expect(copy.title).not.toContain(code);
    expect(copy.title).toMatch(/[.!?]$/);
    expect(copy.detail).toBeTruthy();

    const sentence = `${copy.title} ${copy.detail ?? ""}`;
    expect(sentence).not.toMatch(JARGON);
    expect(sentence).not.toMatch(UUID);
  });

  it("keeps the server's own code available for support", () => {
    const copy = filesErrorCopy(
      new ApiError(409, { error: { code: "files.frozen", message: "drive 3f2a is over_quota" } }),
    );
    expect(copy.code).toBe("files.frozen");
    expect(copy.title).toBe("This drive is over its storage limit.");
    expect(copy.detail).toContain("Free up space");
  });

  it("tells someone whose job never started that it never started", () => {
    // Not a refusal the library raises, so the catalogue above does not reach
    // it: `files.runner_lost` is written onto an operation row by the recovery
    // that gives up on it. Without a row of its own it would fall through to
    // the envelope's own "Request failed", which tells a person nothing about
    // an operation that sat queued and was abandoned.
    const copy = filesErrorCopy(
      new ApiError(409, {
        error: { code: "files.runner_lost", message: "no runner ever started this operation" },
      }),
    );
    expect(copy.code).toBe("files.runner_lost");
    expect(copy.title).not.toMatch(/Request failed/);
    expect(copy.title).toBe("That job never started.");
    expect(copy.retryable).toBe(true);
  });

  it("turns both 412 wordings into the same reload sentence", () => {
    for (const message of ["node 3f2a is not at etag 7", "node 3f2a moved on"]) {
      const copy = filesErrorCopy(
        new ApiError(412, { error: { code: "files.precondition_failed", message } }),
      );
      expect(copy.title).toBe("Someone changed this while you were working.");
      expect(copy.detail).toBe("Reload and try again.");
      expect(copy.retryable).toBe(true);
    }
  });

  it("tells a paced caller to wait, keeping the class's own sentence when it sent one", () => {
    // The platform's 429 carries the class's message; the row keeps it and marks
    // the refusal worth retrying — a pace is a wait, never a dead end.
    const said = "Too many uploads started at once. Please wait a moment and try again.";
    const paced = filesErrorCopy(
      new ApiError(429, { error: { code: "rate_limited", message: said } }),
    );
    expect(paced.code).toBe("rate_limited");
    expect(paced.title).toBe("You are going a little fast.");
    expect(paced.detail).toBe(said);
    expect(paced.retryable).toBe(true);
    expect(paced.title).not.toMatch(JARGON);
    // With no sentence from the server the advice still stands on its own.
    const bare = filesErrorCopy(new ApiError(429, { error: { code: "rate_limited", message: "" } }));
    expect(bare.detail).toBe("Wait a moment, then try again.");
    expect(bare.retryable).toBe(true);
  });
});
