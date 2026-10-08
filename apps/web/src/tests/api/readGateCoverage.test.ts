// The gate is only a gate if every read the portal makes crosses it.
//
// `readGate.ts` claims to be the one place every read converges. No type
// enforces that: a module can call `fetch` itself and quietly opt its reads out
// of the pause, which is exactly how the storm this fixed was built — one hook
// at a time, each reasonable on its own.
//
// So the claim is checked mechanically. Every direct call to the global `fetch`
// in the app source must be either `gatedFetch` or one of the named exemptions
// below, and an exemption has to say why that request must not wait. Adding a
// caller means adding a line here, which is a thing a reviewer can see.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

interface Exemption {
  /** Why this request must not wait behind a refused read. */
  why: string;
  /** What the exemption covers, where it is one call rather than the file's
   *  whole job — so a new API read cannot be smuggled into a module that was
   *  exempted for something else entirely. */
  calls?: RegExp;
}

/**
 * The files allowed to call the global `fetch` directly. Three kinds only:
 *
 *   - the transports themselves, which are what `gatedFetch` is built out of
 *     or what it would have to hold: a long-lived event stream is the delivery
 *     path the polls exist to replace, and parking it behind a poll's refusal
 *     would take down the thing that makes the polls unnecessary;
 *   - a request that does not address the API at all, so the API's limiter has
 *     no say over it — the content origin serves a grant's own URL;
 *   - the person's file, moving. An upload's reads are not polls: the run opens
 *     by asking what the server already holds and the completion asks again to
 *     name the parts, so holding one holds the bytes behind it, and the reader
 *     is watching a row that says "waiting" for a refusal some folder poll
 *     collected. Each of those reads already waits out a refusal it meets
 *     itself, on the server's own terms.
 */
const UNGATED: Readonly<Record<string, Exemption>> = {
  "api/readGate.ts": {
    why: "the gate itself — this is the call every other read is held in front of",
    calls: /globalThis\.fetch\(/,
  },
  "api/events/sseClient.ts": {
    why: "a long-lived stream and never a poll; it is the delivery path the polls exist to replace, and it has a reconnect ladder of its own",
    calls: /globalThis\.fetch\(/,
  },
  "api/client.ts": {
    why: "the session refresh, which is a write on its own route, and the single re-send of a request that already crossed the gate once",
  },
  "api/filesUpload.ts": {
    why: "the person's file moving: every read here serves the upload on screen, and each already waits out a refusal it meets itself",
    calls: /\(\.\.\.args\) => fetch\(\.\.\.args\)/,
  },
  "pages/workspace/files/useUploads.ts": {
    why: "the same upload transport, defaulted at the hook — see api/filesUpload.ts",
    calls: /=> fetch\(\.\.\.args\)/,
  },
  "pages/workspace/chat/data/chatFiles.ts": {
    why: "one read of a grant.url, which addresses the content origin rather than the API",
    calls: /fetch\(grant\.url/,
  },
  "pages/workspace/files/preview/folderResolver.ts": {
    why: "a grant.url read against the content origin, outside the API limiter's reach",
    calls: /fetch\(grant\.url/,
  },
  "pages/workspace/files/preview/copyContent.ts": {
    why: "one read of a blob: URL the page minted from bytes it already holds — no server answers it, so no limiter has a say",
    calls: /fetch\(url\)/,
  },
  "pages/workspace/files/preview/usePreviewContent.ts": {
    why: "grant.url reads against the content origin — the first window and each later one — outside the API limiter's reach",
    calls: /fetch\((grant|windowGrant)\.url/,
  },
};

/** Every `.ts` / `.tsx` under `src/`, excluding tests and generated trees. */
function sourceFiles(dir: string, found: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      if (name === "tests" || name === "node_modules") continue;
      sourceFiles(full, found);
      continue;
    }
    if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) found.push(full);
  }
  return found;
}

/**
 * A call of the global `fetch`.
 *
 * Both spellings, because the second is the one a transport reaches for and
 * therefore the one a new transport would copy: a bare `fetch(` that is not
 * part of a longer identifier (`prefetch(`) or a method on something else
 * (`this.fetchImpl(`), and an explicit `globalThis` / `window` / `self` one.
 */
const DIRECT_FETCH = /(^|[^.\w])fetch\s*\(|\b(?:globalThis|window|self)\.fetch\s*\(/;

/** Prose says "the underlying fetch (…)" constantly, and a comment cannot issue
 *  a request. Only code lines count. */
function codeLines(source: string): string[] {
  return source.split("\n").filter((line) => {
    const text = line.trimStart();
    return !text.startsWith("//") && !text.startsWith("*") && !text.startsWith("/*");
  });
}

const FILES = sourceFiles(SRC).map((file) => ({
  file,
  rel: relative(SRC, file).split(sep).join("/"),
  calls: codeLines(readFileSync(file, "utf8")).filter((line) => DIRECT_FETCH.test(line)),
}));

describe("every read crosses the gate", () => {
  it("finds no module calling fetch directly outside the named exemptions", () => {
    // A failure here is not "add yourself to the list". It is: does this read
    // belong behind the pause? Almost always yes — route it through
    // `gatedFetch`. The list is for a request the API's limiter has no say
    // over, a transport, and the person's own file.
    const offenders = FILES.filter((f) => f.calls.length > 0 && !(f.rel in UNGATED)).map(
      (f) => f.rel,
    );
    expect(offenders).toEqual([]);
  });

  it("keeps each exemption to the call it was granted for", () => {
    // An exempt file is exempt for a REASON, not for ever after: a new read of
    // the API added to one of these must still meet the gate.
    const strays: string[] = [];
    for (const [rel, { calls }] of Object.entries(UNGATED)) {
      if (!calls) continue;
      const found = FILES.find((f) => f.rel === rel);
      for (const line of found?.calls ?? []) {
        if (!calls.test(line)) strays.push(`${rel}: ${line.trim()}`);
      }
    }
    expect(strays).toEqual([]);
  });

  it("names a reason for every exemption, and lists no file that is gone", () => {
    // An allow-list nobody prunes is an allow-list that stops meaning anything.
    for (const [rel, { why }] of Object.entries(UNGATED)) {
      expect(why.length, `${rel} has no reason`).toBeGreaterThan(20);
      expect(() => statSync(join(SRC, rel)), `${rel} no longer exists`).not.toThrow();
      expect(FILES.find((f) => f.rel === rel)?.calls.length ?? 0, `${rel} no longer calls fetch`)
        .toBeGreaterThan(0);
    }
  });
});
