import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// The prod proxy (nginx.conf.template) must forward ONLY the backend's real
// prefixes (`/api`, `/admin/v1`, `/health`) and must NOT shadow the SPA's
// client-side routes. The backend admin API is mounted at `/admin/v1`, while the
// SPA owns `/admin`, `/admin/orgs`, `/admin/users`, … — so a broad `/admin` rule
// sends those hard-refreshes to FastAPI, which 404s. This test locks the
// contract so a future re-broadening fails CI.
//
// Four regex locations serve the backend: two narrow Alkera Files ones carrying the
// upload and content bodies, a narrow chats one carrying the typed turn, and the general
// API one. nginx serves a request from the FIRST regex location that matches, so the
// narrow blocks have to come first — the general pattern matches /api/v1/files/… and
// /api/v1/chats/… as well — and the routing table below has to be checked against the
// block that actually serves each path rather than against whichever regex happens to
// appear first in the file. The narrow blocks' own directives are pinned in
// `src/tests/nginx-streaming.test.ts`.
//
// The narrow patterns are narrow on purpose: they are the twins of the backend's
// pre-buffer guard (GUARDED_PREFIXES in apps/backend/backend/api/body_limit.py), which is
// what makes their raised body cap safe. Every other Files and chats route — and there are
// several that take a body — has no such guard, so it must stay on the general location
// and nginx's default cap.
//
// The DEV proxy is the same contract against a different artifact, and lives in
// `src/tests/app/devProxy.test.ts`: its table is a real module now
// (`src/dev/proxyTable.ts`), so it is checked by calling it rather than by
// scraping vite.config.ts for quoted keys.

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const template = readFileSync(resolve(webRoot, "nginx.conf.template"), "utf8");

// Every `location ~ <pattern> {` in file order — which is the order nginx tries
// them in.
const LOCATION_PATTERNS = [...template.matchAll(/location\s+~\s+(\S+)\s*\{/g)].map((m) => m[1]);

const UPLOADS = "^/api/v1/files/uploads/";
const FILE_BODIES = "^/api/v1/files/drives/.+/(content|bulk|tree|snapshots|lease/release)$";
const CHATS = "^/api/v1/chats/.+/(messages|answer)$";
const API = "^/(api|admin/v1)/";

/** The named location's regex — throws (failing the test) rather than vacuously
 *  passing if the template renamed or dropped the location entirely. */
function regexOf(pattern: string): RegExp {
  if (!LOCATION_PATTERNS.includes(pattern)) {
    throw new Error(`no \`location ~ ${pattern}\` block in the template`);
  }
  return new RegExp(pattern);
}

/** The pattern of the location nginx serves `path` from, or null when every regex
 *  location declines it and the request falls through to index.html. */
function servingLocation(path: string): string | null {
  return LOCATION_PATTERNS.find((pattern) => new RegExp(pattern).test(path)) ?? null;
}

// Top-level SPA route prefixes that must always fall through to index.html.
const SPA_PATHS = [
  "/admin",
  "/admin/orgs",
  "/admin/orgs/abc123",
  "/admin/users",
  "/admin/users/abc123",
  "/admin/catalog",
  "/dashboard",
  "/dashboard/teams",
  "/login",
  "/signup",
];

// Real backend paths that must reach FastAPI through the general API location.
const BACKEND_PATHS = ["/api/v1/auth/login", "/admin/v1/orgs", "/admin/v1/users"];

// Real backend paths that must reach FastAPI through a narrow body location — the only
// ones carrying the raised cap (and, for Files, the request-buffering setting) — paired
// with the location that has to serve each. Every entry is a pattern the backend guard
// bounds.
const GUARDED_BODY_PATHS: [string, string][] = [
  ["/api/v1/files/uploads/abc123/parts/7", UPLOADS],
  ["/api/v1/files/uploads/abc123/complete", UPLOADS],
  ["/api/v1/files/drives/d1/items/i1/content", FILE_BODIES],
  ["/api/v1/files/drives/d1/bulk", FILE_BODIES],
  ["/api/v1/files/drives/d1/tree", FILE_BODIES],
  ["/api/v1/files/drives/d1/snapshots", FILE_BODIES],
  ["/api/v1/files/drives/d1/items/i1/lease/release", FILE_BODIES],
  ["/api/v1/chats/c1/messages", CHATS],
  ["/api/v1/chats/c1/answer", CHATS],
];

// Files and chats paths that take a body but that the backend guard does NOT bound. They
// must be served by the general location, whose default cap is the only bound they have —
// a prefix-wide location would claim them and lift it.
const UNGUARDED_BODY_PATHS = [
  "/api/v1/files/uploads",
  "/api/v1/files/drives",
  "/api/v1/files/drives/d1/items/i1/children",
  "/api/v1/files/drives/d1/items/i1/lease/live",
  "/api/v1/files/drives/d1/items/i1/lease/heartbeat",
  "/api/v1/files/drives/d1/items/i1/permissions",
  "/api/v1/files/drives/d1/items/i1/copy",
  "/api/v1/files/drives/d1/items/i1/duplicate",
  "/api/v1/files/drives/d1/items/i1/content-grants",
  "/api/v1/files/drives/d1/trash/empty",
  "/api/v1/chats",
  "/api/v1/chats/spare",
  "/api/v1/chats/c1",
  "/api/v1/chats/c1/attachments",
  "/api/v1/chats/c1/promote",
  "/api/v1/chats/c1/stop",
  "/api/v1/chats/c1/permission-mode",
  "/api/v1/chats/c1/model",
  "/api/v1/chats/c1/publisher-state",
];

describe("nginx.conf.template proxy regex", () => {
  it("declares the narrow body locations ahead of the general API one", () => {
    // Order is load-bearing, not cosmetic: nginx serves from the first matching
    // regex location, so the general pattern placed first would swallow every
    // Files and chat request along with its body-size and request-buffering settings.
    expect(LOCATION_PATTERNS).toEqual([UPLOADS, FILE_BODIES, CHATS, API]);
  });

  const proxyRegex = regexOf(API);

  it.each(BACKEND_PATHS)("proxies backend path %s", (path) => {
    expect(proxyRegex.test(path)).toBe(true);
  });

  it.each(BACKEND_PATHS)("serves backend path %s from the general API location", (path) => {
    expect(servingLocation(path)).toBe(API);
  });

  it.each(SPA_PATHS)("does NOT proxy SPA route %s (falls through to index.html)", (path) => {
    expect(proxyRegex.test(path)).toBe(false);
  });

  it.each(SPA_PATHS)("no regex location claims SPA route %s", (path) => {
    expect(servingLocation(path)).toBeNull();
  });
});

describe("nginx.conf.template narrow body locations", () => {
  it.each(GUARDED_BODY_PATHS)("serves guarded path %s from %s", (path, expected) => {
    // Both the narrow pattern and the general one match the path; first-match-wins is
    // what routes it to the narrow block.
    expect(regexOf(expected).test(path)).toBe(true);
    expect(regexOf(API).test(path)).toBe(true);
    expect(servingLocation(path)).toBe(expected);
  });

  it.each(UNGUARDED_BODY_PATHS)("leaves unguarded path %s on the general location", (path) => {
    // The backend buffers a body into memory before the auth dependency runs, so the
    // default cap on this location is what stops an anonymous caller streaming an
    // unbounded body at a route nothing else bounds.
    expect(servingLocation(path)).toBe(API);
  });

  it.each([...BACKEND_PATHS, ...SPA_PATHS])("no narrow body location claims %s", (path) => {
    for (const pattern of [UPLOADS, FILE_BODIES, CHATS]) {
      expect(regexOf(pattern).test(path)).toBe(false);
    }
  });
});
