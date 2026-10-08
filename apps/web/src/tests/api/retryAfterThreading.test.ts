// Every refusal built from a real Response carries that response's headers.
//
// `queryRetryDelay` can only honour the server's `Retry-After` if the error it
// is handed still has it, and the portal builds `ApiError` in a dozen places —
// the shared `request()` plus eleven hand-rolled fetch wrappers. A wrapper that
// forgets the fourth argument does not fail: it silently falls back to the
// client's own ladder, on exactly the files-pane paths that really do get
// throttled. So this is pinned two ways — by driving the two busiest wrappers,
// and by a scan that catches the next wrapper somebody writes.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/errors";
import { retryAfterMs } from "@/api/retry";

// Vitest runs from `apps/web`.
const SRC = join(process.cwd(), "src");

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    if (entry === "node_modules" || entry === "tests") continue;
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      yield* sourceFiles(path);
      continue;
    }
    if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) yield path;
  }
}

describe("a refusal built from a response", () => {
  it("keeps the wait the server named", () => {
    const headers = new Headers({ "retry-after": "7" });
    const error = new ApiError(429, { code: "rate_limited" }, "throttled", headers);
    expect(error.retryAfter).toBe("7");
    expect(retryAfterMs(error)).toBe(7_000);
  });

  it("has no wait when the response carried none", () => {
    const error = new ApiError(429, { code: "rate_limited" }, "throttled", new Headers());
    expect(error.retryAfter).toBeNull();
    expect(retryAfterMs(error)).toBeNull();
  });

  // The fourth argument is optional, so forgetting it type-checks, passes every
  // other test, and quietly drops the server's own guidance. Fail the build
  // instead: a construction that has a Response in hand must pass its headers.
  it("is built with its headers everywhere a response is in hand", () => {
    const offenders: string[] = [];
    for (const file of sourceFiles(SRC)) {
      const text = readFileSync(file, "utf8");
      // Each `new ApiError(...)` call, up to its closing parenthesis.
      for (const match of text.matchAll(/new ApiError\(([\s\S]*?)\n?\s*\);/g)) {
        const args = match[1];
        // Only the ones reading a status off a live response: an ApiError
        // synthesised from nothing (a transport failure, status 0) has no
        // headers to carry.
        if (!/\.status\b/.test(args)) continue;
        if (/\.headers\b/.test(args)) continue;
        offenders.push(`${file.slice(SRC.length)}: ${args.trim().split("\n")[0]}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
