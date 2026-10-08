import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// The self-hosted image's default Content-Security-Policy is spelled once, in
// docker-entrypoint.sh, and rendered into every response by nginx. It is a strict
// same-origin policy — with two additions: `connect-src` must admit `ws:`/`wss:`, or a
// browser refuses the portal's websocket even though it targets the same host
// (CSP3's `'self'` covers websocket schemes only in some engines; the explicit scheme
// sources make it deterministic), and a deployment serving Alkera Files names its
// content origin in the three directives a preview crosses. Every other directive is
// pinned to its literal value so a widening elsewhere is a deliberate, reviewed change.
//
// The policy is READ BY RUNNING the entrypoint, not by parsing it: what a browser
// enforces is what the shell computes from the deployment's environment.

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const entrypointPath = resolve(webRoot, "docker-entrypoint.sh");
const entrypoint = readFileSync(entrypointPath, "utf8");

/**
 * The ALKERA_CSP the entrypoint assigns under `env`. Everything above the
 * `envsubst` line — which writes into the image's nginx tree and so cannot run
 * here — is executed under `sh`, then the resulting value is printed.
 */
function renderCsp(env: Record<string, string> = {}): string {
  const cut = entrypoint.search(/^envsubst /m);
  if (cut < 0) throw new Error("docker-entrypoint.sh no longer renders with envsubst");
  const script = `${entrypoint.slice(0, cut)}\nprintf '%s' "$ALKERA_CSP"`;
  return execFileSync("sh", ["-c", script], {
    encoding: "utf8",
    env: {
      PATH: process.env.PATH ?? "",
      ALKERA_BACKEND_URL: "http://backend:8000",
      ...env,
    },
  });
}

/** `name → value` for each `name value…;` directive of a CSP string. */
function directives(csp: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const raw of csp.split(";")) {
    const part = raw.trim();
    if (!part) continue;
    const [name, ...values] = part.split(/\s+/);
    if (out.has(name)) throw new Error(`directive repeated: ${name}`);
    out.set(name, values.join(" "));
  }
  return out;
}

const PINNED: Record<string, string> = {
  "default-src": "'self'",
  "base-uri": "'self'",
  "object-src": "'none'",
  "frame-ancestors": "'none'",
  "form-action": "'self'",
  "img-src": "'self' data: blob:",
  "media-src": "'self' blob:",
  "frame-src": "'self'",
  "font-src": "'self' data:",
  "style-src": "'self' 'unsafe-inline'",
  "script-src": "'self' 'wasm-unsafe-eval'",
};

/** The directives a Files preview crosses: CORS fetch, framed document, streamed media. */
const CONTENT_DIRECTIVES = ["connect-src", "frame-src", "media-src"] as const;

const CONTENT_ORIGIN = "https://c.acme-content.example";

describe("docker-entrypoint.sh — the default Content-Security-Policy", () => {
  const csp = directives(renderCsp());

  it("an EMPTY ALKERA_CSP is backfilled, never rendered as an empty header", () => {
    expect(renderCsp({ ALKERA_CSP: "" })).toBe(renderCsp());
  });

  it("connect-src admits same-origin fetches plus websocket schemes, and nothing else", () => {
    expect(csp.get("connect-src")).toBe("'self' ws: wss:");
  });

  it.each(Object.entries(PINNED))("%s stays exactly `%s`", (name, value) => {
    expect(csp.get(name)).toBe(value);
  });

  it("declares exactly the pinned directives plus connect-src", () => {
    expect(new Set(csp.keys())).toEqual(new Set([...Object.keys(PINNED), "connect-src"]));
  });

  it.each(["frame-src", "script-src", "style-src"])(
    "%s never admits blob:, which would run in the app's own origin",
    (name) => {
      expect(csp.get(name)).not.toContain("blob:");
    },
  );

  it("never admits an arbitrary host, inline script, or eval", () => {
    const flat = renderCsp();
    expect(flat).not.toMatch(/https?:\/\//);
    expect(flat).not.toContain("'unsafe-eval'");
    expect(flat).not.toMatch(/script-src[^;]*'unsafe-inline'/);
    expect(flat).not.toMatch(/(^|\s)\*(\s|;|$)/);
  });

  it("is what nginx renders on every response", () => {
    const template = readFileSync(resolve(webRoot, "nginx.conf.template"), "utf8");
    // Server level plus the /assets/ re-declaration (a location's own add_header
    // suppresses the inherited set), each with `always` so error pages carry it too.
    expect(template.match(/add_header Content-Security-Policy "\$\{ALKERA_CSP\}" always;/g)).toHaveLength(2);
    expect(entrypoint).toMatch(/^export ALKERA_BACKEND_URL ALKERA_CSP$/m);
  });
});

describe("docker-entrypoint.sh — the Alkera Files content origin", () => {
  // A self-hosted deployment cannot serve Files from the portal's host: the app
  // validator refuses a content origin on it, because a cookie is scoped by
  // hostname and would ride along with attacker-uploaded bytes. So every preview
  // is cross-origin, and the default policy has to name the one origin the
  // deployment configured or the feature ships dead in the image's own default.
  const withOrigin = directives(renderCsp({ FILES_CONTENT_BASE_URL: CONTENT_ORIGIN }));

  it.each(CONTENT_DIRECTIVES)("%s admits the configured content origin", (name) => {
    expect(withOrigin.get(name)?.split(" ")).toContain(CONTENT_ORIGIN);
  });

  it("keeps everything the strict policy already granted", () => {
    const base = directives(renderCsp());
    expect(new Set(withOrigin.keys())).toEqual(new Set(base.keys()));
    for (const [name, value] of base) {
      for (const source of value.split(" ")) {
        expect(withOrigin.get(name)?.split(" ")).toContain(source);
      }
    }
  });

  it("reaches no directive a preview does not cross", () => {
    for (const [name, value] of withOrigin) {
      if ((CONTENT_DIRECTIVES as readonly string[]).includes(name)) continue;
      expect(value, name).not.toContain("c.acme-content.example");
    }
  });

  it("admits that origin and no other host", () => {
    const sources = [...withOrigin.values()].flatMap((value) => value.split(" "));
    expect(new Set(sources.filter((s) => /^https?:\/\//.test(s)))).toEqual(
      new Set([CONTENT_ORIGIN]),
    );
  });

  it.each([
    ["a trailing slash", `${CONTENT_ORIGIN}/`, CONTENT_ORIGIN],
    ["a path", `${CONTENT_ORIGIN}/files/`, CONTENT_ORIGIN],
    ["a port", "https://c.acme-content.example:8443", "https://c.acme-content.example:8443"],
    ["plain http", "http://c.acme-content.example", "http://c.acme-content.example"],
  ])("%s is reduced to the bare origin a CSP source can match", (_case, base, origin) => {
    // A CSP host-source carrying a path or a trailing slash matches nothing, so
    // the whole point of naming the origin would be lost silently.
    const rendered = directives(renderCsp({ FILES_CONTENT_BASE_URL: base }));
    for (const name of CONTENT_DIRECTIVES) {
      expect(rendered.get(name)?.split(" "), name).toContain(origin);
    }
  });

  it.each([
    ["unset", undefined],
    ["empty", ""],
    ["no scheme", "c.acme-content.example"],
    ["not a URL", "yes"],
  ])("%s leaves the strict same-origin policy exactly as it was", (_case, value) => {
    // Unset and empty are both shapes a deployment produces: the chart omits the
    // variable while Files is off, the compose file passes it through empty.
    const env: Record<string, string> = {};
    if (value !== undefined) env.FILES_CONTENT_BASE_URL = value;
    expect(renderCsp(env)).toBe(renderCsp());
  });

  it("an operator's own ALKERA_CSP still wins whole", () => {
    // Documented in deploy/INSTALL.md (the Files section): overriding the policy
    // means owning it, the content origin included. The image must not graft
    // sources onto it.
    const own = "default-src 'self'; connect-src 'self'";
    expect(renderCsp({ ALKERA_CSP: own, FILES_CONTENT_BASE_URL: CONTENT_ORIGIN })).toBe(own);
  });
});
