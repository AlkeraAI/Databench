// @vitest-environment node
// The dev proxy table must not swallow a client route.
//
// Vite matches a plain proxy key by PREFIX, so `"/c"` captures `/chat`,
// `/connections` and `/complete-profile` and answers a deep link or a refresh
// with the backend's JSON 404. Client-side navigation is unaffected, so the
// break only shows on a hard load — which is why this is checked mechanically
// against the real route table rather than by clicking.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";

import { devProxyTable, proxyCaptures } from "@/dev/proxyTable";

import { devServerConfig } from "../dev/devServerConfig";

const TARGET = "http://localhost:8000";
const table = devProxyTable(TARGET);
const keys = Object.keys(table);

/** Every client route App.tsx declares, as a concrete path the browser can load. The routes an
 *  extension registers are held to the same check by the product's own tests. */
function spaRoutes(): string[] {
  // vitest runs with apps/web as its root; import.meta.url is an http URL under jsdom.
  const source = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf-8");
  const paths = [...source.matchAll(/path="([^"]+)"/g)].map((match) => match[1]);
  const concrete = paths
    .filter((path) => path.startsWith("/"))
    // `/chat/:chatId` is loaded as `/chat/<id>`; a param placeholder would not
    // exercise the prefix the proxy actually sees.
    .map((path) => path.replace(/:[A-Za-z0-9_]+\??/g, "x"));
  return [...new Set(concrete)];
}

describe("dev proxy table", () => {
  const routes = spaRoutes();

  it("reads a route table to check against", () => {
    // A regex that silently stopped matching would turn every assertion below
    // into a pass over an empty list.
    expect(routes).toEqual(expect.arrayContaining(["/chat", "/files", "/complete-profile"]));
    expect(routes.length).toBeGreaterThan(20);
  });

  it.each(keys)("key %s captures no client route", (key) => {
    const captured = routes.filter((route) => proxyCaptures(key, route));
    expect(captured).toEqual([]);
  });

  it.each([
    ["/api/v1/me", "/api"],
    ["/admin/v1/orgs", "/admin/v1"],
    ["/health/ready", "/health"],
    ["/c/signed-token", "^/c/"],
  ])("still forwards %s to the backend", (path, expected) => {
    const matched = keys.filter((key) => proxyCaptures(key, path));
    expect(matched).toEqual([expected]);
  });

  it("points every entry at the api target", () => {
    for (const entry of Object.values(table)) {
      expect(typeof entry === "string" ? entry : entry.target).toBe(TARGET);
    }
  });

  it("forwards exactly the backend's four prefixes", () => {
    // `^/c/` is the Files content mount: with no separate content origin a
    // download link is same-origin, so the dev server must hand `/c/<token>` to
    // the API rather than answer index.html — anchored, because the bare prefix
    // also matched /chat, /connections and /complete-profile.
    expect(new Set(keys)).toEqual(new Set(["/api", "/admin/v1", "/health", "^/c/"]));
  });

  it("is the table the dev server proxies with", () => {
    // Checking the table proves nothing if the config proxies with something
    // else, so the evaluated config must carry exactly this table.
    vi.stubEnv("VITE_API_PROXY_TARGET", "http://localhost:9911");
    try {
      expect(devServerConfig().proxy).toEqual(devProxyTable("http://localhost:9911"));
    } finally {
      vi.unstubAllEnvs();
    }
  });
});
