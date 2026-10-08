import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// The self-hosted image fronts the backend with nginx (apps/web/nginx.conf.template,
// rendered by docker-entrypoint.sh). The portal's live surfaces ride long-lived
// connections through that proxy: a server-sent event stream under /api/v1/events, a
// websocket under /api/v1/ws, and Alkera Files upload + content bodies under
// /api/v1/files. Stock proxy defaults break every one of them — a websocket never
// upgrades without the Upgrade/Connection hop-by-hop headers, an event stream sits in
// nginx's response buffer and is cut by the read timeout while it idles, and a file
// body is refused past 1 MiB and spooled to disk before the backend sees a byte.
//
// Four regex locations serve those paths: one per Files body pattern, one for the chat
// turn bodies, then the general API one. nginx serves a request from the FIRST regex
// location that matches, so a request lands in exactly ONE of them — which means the
// streaming directives have to be spelled on EVERY one, and the narrow blocks have to come
// first (the general pattern matches /api/v1/files/… and /api/v1/chats/… as well). These
// tests pin every body and their order, so a template that loses streaming on a block, or
// that reorders them, fails.
//
// The raised body cap is the security-relevant part, and it is scoped rather than
// prefix-wide. The backend's pre-buffer guard (GUARDED_PREFIXES in
// apps/backend/backend/api/body_limit.py) bounds only the upload prefix and five drives
// suffixes; every other Files route reaches FastAPI, which buffers the body into memory
// BEFORE the auth dependency runs. A cap lifted across the whole /api/v1/files/ prefix
// would therefore be an unauthenticated memory-exhaustion primitive on the routes nothing
// else bounds, so these tests pin that the lift stops at the guarded patterns.

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const template = readFileSync(resolve(webRoot, "nginx.conf.template"), "utf8");
const entrypoint = readFileSync(resolve(webRoot, "docker-entrypoint.sh"), "utf8");

/** Seconds for an nginx time value (`3600s`, `60m`, `1h`; a bare number is seconds). */
function nginxSeconds(value: string): number {
  const m = value.match(/^(\d+)(ms|s|m|h|d)?$/);
  if (!m) throw new Error(`not an nginx time: ${value}`);
  const n = Number(m[1]);
  switch (m[2]) {
    case "ms":
      return n / 1000;
    case "m":
      return n * 60;
    case "h":
      return n * 3600;
    case "d":
      return n * 86400;
    default:
      return n;
  }
}

/** Bytes for an nginx size value (`0`, `512k`, `20m`, `1g`; a bare number is bytes). */
function nginxBytes(value: string): number {
  const m = value.match(/^(\d+)([kKmMgG])?$/);
  if (!m) throw new Error(`not an nginx size: ${value}`);
  const n = Number(m[1]);
  switch (m[2]?.toLowerCase()) {
    case "k":
      return n * 1024;
    case "m":
      return n * 1024 ** 2;
    case "g":
      return n * 1024 ** 3;
    default:
      return n;
  }
}

/** The body of the block whose `{` is at `open`, found by brace depth. envsubst
 *  placeholders (`${ALKERA_BACKEND_URL}`) carry a `}` of their own and are skipped. */
function blockBodyAt(source: string, open: number): string | null {
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "$" && source[i + 1] === "{") {
      i = source.indexOf("}", i);
      if (i < 0) return null;
      continue;
    }
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(open + 1, i);
    }
  }
  return null;
}

/** The body of the first block opened by `header`. */
function blockBody(source: string, header: RegExp): string | null {
  const start = source.search(header);
  if (start < 0) return null;
  return blockBodyAt(source, source.indexOf("{", start));
}

/** Every `location ~ <pattern> { … }` in the template, in file order — which is the
 *  order nginx tries them in. */
function regexLocations(source: string): { pattern: string; body: string }[] {
  return [...source.matchAll(/location\s+~\s+(\S+)\s*\{/g)].map((m) => {
    const body = blockBodyAt(source, m.index + m[0].length - 1);
    if (body === null) throw new Error(`unterminated \`location ~ ${m[1]}\` block`);
    return { pattern: m[1], body };
  });
}

// The two Files body patterns, and the app-side ceiling each one twins. Both mirror
// GUARDED_PREFIXES: `/api/v1/files/uploads/` (136 MiB, a proxied upload part plus
// envelope headroom) and the drives suffixes, whose largest guarded body is the 64 MiB
// single-call content PUT / bulk batch.
const UPLOADS = "^/api/v1/files/uploads/";
const FILE_BODIES = "^/api/v1/files/drives/.+/(content|bulk|tree|snapshots|lease/release)$";
// The chat turn bodies, which the same guard bounds at 8 MiB — a typed message may run to
// chat_message_max_chars, far past nginx's 1 MiB default.
const CHATS = "^/api/v1/chats/.+/(messages|answer)$";
const API = "^/(api|admin/v1)/";

const GUARDED: [string, number][] = [
  [UPLOADS, 136 * 1024 ** 2],
  [FILE_BODIES, 64 * 1024 ** 2],
];

// Files routes that take a body but that the backend guard does NOT bound. nginx's
// default cap is the only bound they have, so they must keep it.
const UNGUARDED_FILES_PATHS = [
  "/api/v1/files/uploads",
  "/api/v1/files/drives/d1/items/i1/children",
  "/api/v1/files/drives/d1/items/i1/lease/live",
  "/api/v1/files/drives/d1/items/i1/lease/heartbeat",
  "/api/v1/files/drives/d1/items/i1/permissions",
  "/api/v1/files/drives/d1/items/i1/copy",
  "/api/v1/files/drives/d1/items/i1/duplicate",
  "/api/v1/files/drives/d1/items/i1/content-grants",
];

const locations = regexLocations(template);

/** The named block's body — throws (failing the test) rather than vacuously passing an
 *  empty string if the template dropped the location entirely. */
function bodyOf(pattern: string): string {
  const found = locations.find((l) => l.pattern === pattern);
  if (!found) throw new Error(`no \`location ~ ${pattern}\` block in the template`);
  return found.body;
}

/** The pattern of the regex location nginx serves `path` from — first match wins. */
function servingPattern(path: string): string | null {
  return locations.find((l) => new RegExp(l.pattern).test(path))?.pattern ?? null;
}

describe("nginx.conf.template — the proxy locations", () => {
  it("has exactly the four regex locations, the body ones first", () => {
    // Order is load-bearing, not cosmetic: nginx serves from the first matching regex
    // location, so the general API pattern placed first would swallow every Files and
    // chat request along with its body-size and request-buffering settings.
    expect(locations.map((l) => l.pattern)).toEqual([UPLOADS, FILE_BODIES, CHATS, API]);
  });

  it.each(["/api/v1/files/uploads/abc123/parts/7", "/api/v1/chats/c1/messages"])(
    "the general pattern matches body path %s too, so first-match-wins decides",
    (path) => {
      expect(new RegExp(API).test(path)).toBe(true);
      expect(servingPattern(path)).not.toBe(API);
    },
  );
});

// Every directive a long-lived connection needs. A request is served by exactly one
// location, so each block carries the whole set on its own.
const STREAMING: [string, RegExp][] = [
  ["proxy_pass to the backend", /proxy_pass\s+\$\{ALKERA_BACKEND_URL\};/],
  ["proxy_http_version 1.1", /proxy_http_version\s+1\.1;/],
  ["the Upgrade header", /proxy_set_header\s+Upgrade\s+\$http_upgrade;/],
  ["the mapped Connection header", /proxy_set_header\s+Connection\s+\$connection_upgrade;/],
  ["proxy_buffering off", /proxy_buffering\s+off;/],
  ["proxy_cache off", /proxy_cache\s+off;/],
  ["proxy_read_timeout", /proxy_read_timeout\s+\S+;/],
  ["proxy_send_timeout", /proxy_send_timeout\s+\S+;/],
];

for (const pattern of [UPLOADS, FILE_BODIES, CHATS, API]) {
  describe(`nginx.conf.template — \`location ~ ${pattern}\` streams`, () => {
    it.each(STREAMING)("carries %s", (_name, directive) => {
      expect(bodyOf(pattern)).toMatch(directive);
    });

    it.each(["proxy_read_timeout", "proxy_send_timeout"])("%s is at least an hour", (directive) => {
      const m = bodyOf(pattern).match(new RegExp(`${directive}\\s+(\\S+);`));
      expect(m).not.toBeNull();
      expect(nginxSeconds(m![1])).toBeGreaterThanOrEqual(3600);
    });

    it("does not repeat a timeout with a second, shorter value", () => {
      expect(bodyOf(pattern).match(/proxy_read_timeout/g)).toHaveLength(1);
      expect(bodyOf(pattern).match(/proxy_send_timeout/g)).toHaveLength(1);
    });

    it("keeps the request headers the backend relies on", () => {
      for (const header of ["Host", "X-Real-IP", "X-Forwarded-For", "X-Forwarded-Proto", "Cookie"]) {
        expect(bodyOf(pattern)).toMatch(new RegExp(`proxy_set_header\\s+${header}\\s+`));
      }
    });
  });
}

describe("nginx.conf.template — the Files bodies", () => {
  it.each(GUARDED)("`location ~ %s` raises the cap to its guarded ceiling", (pattern, appCap) => {
    const m = bodyOf(pattern).match(/client_max_body_size\s+(\S+);/);
    expect(m).not.toBeNull();
    const bytes = nginxBytes(m![1]);
    // Finite. `0` disables the check outright, so a location carrying it bounds nothing
    // the backend guard does not already bound — and that guard covers these patterns,
    // not the prefix they sit under.
    expect(bytes).toBeGreaterThan(0);
    // At or above the app's own ceiling, so the app answers a typed 4xx before nginx
    // answers an unexplained 413 …
    expect(bytes).toBeGreaterThanOrEqual(appCap);
    // … and within a part's worth of it, so the edge is still the bound it claims to be.
    expect(bytes).toBeLessThanOrEqual(appCap * 2);
  });

  it.each(GUARDED)("`location ~ %s` streams the body instead of spooling it to disk", (pattern) => {
    expect(bodyOf(pattern)).toMatch(/proxy_request_buffering\s+off;/);
  });
});

describe("nginx.conf.template — the chat turn bodies", () => {
  it("raises the cap to the guard's own chat ceiling", () => {
    // 8 MiB is what the pre-buffer guard applies to the same two patterns
    // (_CHAT_BODY_MAX_BYTES in backend/api/body_limit.py). Matching it exactly keeps the
    // app's typed refusal the one a caller ever sees: a smaller value here turns a long
    // paste into nginx's unexplained 413, a larger one lets a body past the edge that the
    // backend refuses anyway.
    const m = bodyOf(CHATS).match(/client_max_body_size\s+(\S+);/);
    expect(m).not.toBeNull();
    expect(nginxBytes(m![1])).toBe(8 * 1024 ** 2);
  });

  it("leaves request buffering on, unlike the Files bodies", () => {
    // A chat turn is a single small JSON body, not a multi-hundred-MiB part stream: nginx
    // buffering it is what keeps a slow client off an upstream worker, so this block must
    // NOT copy the Files blocks' `proxy_request_buffering off`.
    expect(bodyOf(CHATS)).not.toMatch(/proxy_request_buffering/);
  });
});

describe("nginx.conf.template — the lifted cap stops at the guarded patterns", () => {
  it("leaves the general API location on nginx's default cap", () => {
    // No client_max_body_size at all: the 1 MiB default is what bounds every route the
    // backend's pre-buffer guard does not, and the body lands in memory before any
    // credential is checked.
    expect(bodyOf(API)).not.toMatch(/client_max_body_size/);
    // Buffered too, so a body that does arrive is nginx's problem before it is the
    // backend's.
    expect(bodyOf(API)).not.toMatch(/proxy_request_buffering/);
  });

  it.each(UNGUARDED_FILES_PATHS)("unguarded Files path %s keeps that default cap", (path) => {
    expect(servingPattern(path)).toBe(API);
  });

  it.each(["/api/v1/files/uploads/abc123/parts/7", "/api/v1/files/uploads/abc123/complete"])(
    "upload path %s is served by the uploads location",
    (path) => {
      expect(servingPattern(path)).toBe(UPLOADS);
    },
  );

  it.each([
    "/api/v1/files/drives/d1/items/i1/content",
    "/api/v1/files/drives/d1/bulk",
    "/api/v1/files/drives/d1/tree",
    "/api/v1/files/drives/d1/snapshots",
    "/api/v1/files/drives/d1/items/i1/lease/release",
  ])("guarded drives path %s is served by the bodies location", (path) => {
    expect(servingPattern(path)).toBe(FILE_BODIES);
  });

  it.each(["/api/v1/chats/c1/messages", "/api/v1/chats/c1/answer"])(
    "guarded chat path %s is served by the chats location",
    (path) => {
      expect(servingPattern(path)).toBe(CHATS);
    },
  );

  it.each([
    "/api/v1/chats",
    "/api/v1/chats/spare",
    "/api/v1/chats/c1",
    "/api/v1/chats/c1/attachments",
    "/api/v1/chats/c1/promote",
    "/api/v1/chats/c1/stop",
    "/api/v1/chats/c1/permission-mode",
    "/api/v1/chats/c1/model",
    "/api/v1/chats/c1/publisher-state",
  ])("unguarded chat path %s keeps that default cap", (path) => {
    expect(servingPattern(path)).toBe(API);
  });
});

describe("nginx.conf.template — the Connection map", () => {
  // `map` is only legal in the http context. conf.d/*.conf is included inside http {},
  // so a top-level map in this file is exactly that — but it must sit OUTSIDE server {}.
  const serverStart = template.indexOf("server {");
  const mapHeader = /^map\s+\$http_upgrade\s+\$connection_upgrade\s*\{/m;

  it("maps $http_upgrade to $connection_upgrade at the top level, before server {}", () => {
    expect(serverStart).toBeGreaterThan(0);
    const at = template.search(mapHeader);
    expect(at).toBeGreaterThanOrEqual(0);
    expect(at).toBeLessThan(serverStart);
  });

  it("upgrades when the client asks and closes the hop otherwise", () => {
    const mapBody = blockBody(template, mapHeader) ?? "";
    expect(mapBody).toMatch(/default\s+upgrade;/);
    expect(mapBody).toMatch(/''\s+close;/);
  });
});

describe("docker-entrypoint.sh — rendering leaves nginx variables alone", () => {
  // envsubst is given an explicit allowlist; anything else spelled `$name` (the map's
  // variables, $host, $remote_addr, …) survives rendering. A template that spelled an
  // nginx variable as `${name}` would still survive with the allowlist, but a widened
  // allowlist (or a bare `envsubst`) would blank every nginx variable — pin both ends.
  const allowlist = entrypoint.match(/envsubst\s+'([^']*)'/)?.[1] ?? "";
  const allowed = [...allowlist.matchAll(/\$\{(\w+)\}/g)].map((m) => m[1]);

  it("substitutes exactly the two operator variables", () => {
    expect(new Set(allowed)).toEqual(new Set(["ALKERA_BACKEND_URL", "ALKERA_CSP"]));
  });

  it("every `${…}` in the template is one envsubst renders", () => {
    const referenced = [...template.matchAll(/\$\{(\w+)\}/g)].map((m) => m[1]);
    expect(referenced.length).toBeGreaterThan(0);
    for (const name of referenced) expect(allowed).toContain(name);
  });

  it("the streaming variables are plain nginx variables, not envsubst targets", () => {
    for (const name of ["http_upgrade", "connection_upgrade"]) {
      expect(template).toMatch(new RegExp(`\\$${name}\\b`));
      expect(template).not.toContain(`\${${name}}`);
      expect(allowed).not.toContain(name);
    }
  });
});
