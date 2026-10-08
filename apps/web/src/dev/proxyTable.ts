// The dev server's backend proxy table.
//
// Spelled here rather than inline in `vite.config.ts` so a test can hold it to
// the rule Vite itself applies: a plain string key matches by PREFIX, not by
// path segment. A bare `"/c"` therefore captures every client route whose path
// merely starts with those characters — `/chat`, `/connections`,
// `/complete-profile` — and a deep link, a refresh or the signup flow's landing
// is answered with the backend's JSON 404 instead of the app. Client-side
// navigation never notices, which is exactly why it survives a click-through.
//
// A key whose prefix could swallow a neighbouring client route is written as an
// anchored regex carrying its own boundary (`"^/c/"`). The other three keys are
// left as prefixes because no client route begins with them — a fact the
// companion test re-checks against the real route table on every run.

import type { ProxyOptions } from "vite";

export type ProxyTable = Record<string, string | ProxyOptions>;

/** The backend paths the dev server forwards to FastAPI, keyed as Vite matches them. */
export function devProxyTable(apiTarget: string): ProxyTable {
  return {
    "/api": { target: apiTarget, ws: true },
    // The platform-staff admin API is mounted at the root, not under /api, so it
    // needs its own rule — matched as `/admin/v1`, NOT bare `/admin`, which is a
    // family of client routes (/admin, /admin/orgs, …) the history fallback serves.
    "/admin/v1": apiTarget,
    "/health": apiTarget,
    // Same-origin content in dev: a signed download link is `/c/<token>`. The
    // trailing slash is load-bearing — see the header comment.
    "^/c/": apiTarget,
  };
}

/**
 * Whether Vite would route `path` through the proxy entry `key`.
 *
 * Mirrors Vite's own rule: a key beginning with `^` is a regular expression,
 * anything else matches by prefix.
 */
export function proxyCaptures(key: string, path: string): boolean {
  return key.startsWith("^") ? new RegExp(key).test(path) : path.startsWith(key);
}
