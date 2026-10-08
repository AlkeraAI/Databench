import { afterEach, describe, expect, it } from "vitest";

import { captureAndStripGithubCallback, takeGithubCallback } from "@/app/boot/githubCallback";

// The GitHub install callback carries the OAuth code (and signed state + installation
// id). If any of it survives in the URL when analytics boots, Google Analytics copies
// it to a third party via page_location -- and a leaked callback completes the
// crafted-install-link attack. These pin: the boot strip lifts the secrets off the
// URL, holds them for the page, and leaves nothing for telemetry to read.

const CALLBACK = "/org/integration?installation_id=777&state=signed-state&code=oauth-secret-code";

function setUrl(pathAndQuery: string): void {
  window.history.replaceState({}, "", pathAndQuery);
}

afterEach(() => {
  takeGithubCallback(); // drain any captured value between cases
  setUrl("/");
});

describe("captureAndStripGithubCallback", () => {
  it("lifts the callback params off the URL and holds them for the page", () => {
    setUrl(CALLBACK);
    captureAndStripGithubCallback();

    // The URL no longer carries any of the secrets...
    expect(window.location.search).toBe("");
    expect(window.location.href).not.toContain("oauth-secret-code");
    expect(window.location.href).not.toContain("signed-state");
    expect(window.location.href).not.toContain("777");

    // ...but the flow still has them, exactly once.
    const captured = takeGithubCallback();
    expect(captured).toEqual({
      installation_id: "777",
      state: "signed-state",
      code: "oauth-secret-code",
      error: null,
    });
    expect(takeGithubCallback()).toBeNull(); // consumed once
  });

  it("captures a declined-authorization error and strips GitHub's full error triple", () => {
    // GitHub's OAuth error callback is error + error_description + error_uri; all
    // three must be stripped, not just `error`, or error_uri survives into GA /
    // return_to.
    setUrl(
      "/org/integration?error=access_denied&error_description=nope&" +
        "error_uri=https%3A%2F%2Fdocs.github.com%2Ferr&state=signed-state",
    );
    captureAndStripGithubCallback();
    expect(window.location.search).toBe("");
    expect(window.location.href).not.toContain("error_uri");
    expect(window.location.href).not.toContain("docs.github.com");
    expect(takeGithubCallback()?.error).toBe("access_denied");
  });

  it("preserves unrelated query params while dropping the callback ones", () => {
    setUrl("/org/integration?tab=activity&code=oauth-secret-code&state=s&installation_id=1");
    captureAndStripGithubCallback();
    expect(window.location.search).toBe("?tab=activity");
    expect(window.location.href).not.toContain("oauth-secret-code");
  });

  it.each([
    ["state alone", "/org/integration?state=lonely-signed-state", "state", "lonely-signed-state"],
    ["setup_action alone", "/org/integration?setup_action=update", null, null],
  ])("captures on ANY callback param, not just the code (%s)", (_label, url, key, value) => {
    // A `?state=` (or `?setup_action=`) with no code must STILL be stripped -- it
    // would otherwise linger in the URL and reach analytics / a return_to.
    setUrl(url);
    captureAndStripGithubCallback();
    expect(window.location.search).toBe("");
    const captured = takeGithubCallback();
    expect(captured).not.toBeNull();
    if (key === "state") expect(captured?.state).toBe(value);
  });

  it("strips callback params from the HASH FRAGMENT, not just the query", () => {
    // A fragment (#code=...) never reaches the server but is fully readable by
    // in-page JS, so telemetry / a return_to would still leak it.
    setUrl("/org/integration#code=oauth-secret-code&state=s&installation_id=9");
    captureAndStripGithubCallback();
    expect(window.location.hash).toBe("");
    expect(window.location.href).not.toContain("oauth-secret-code");
    const captured = takeGithubCallback();
    expect(captured?.code).toBe("oauth-secret-code");
    expect(captured?.installation_id).toBe("9");
  });

  it.each([
    [
      "router-path fragment",
      "/org/integration#/org/integration?code=oauth-secret-code&state=s",
      "#/org/integration",
      "s",
    ],
    ["anchor before the ?", "/org/integration#anchor?code=oauth-secret-code", "#anchor", null],
  ])(
    "strips only the params after a `?` in the fragment, keeping the prefix (%s)",
    (_label, url, hash, state) => {
      // A HashRouter-style fragment (#/org/integration?code=...) is not itself a
      // param list: parsed whole, the key becomes `/org/integration?code` and the
      // code is missed entirely -- it stays in the URL for telemetry / a
      // return_to, and the claim flow never starts.
      setUrl(url);
      captureAndStripGithubCallback();
      expect(window.location.hash).toBe(hash);
      expect(window.location.href).not.toContain("oauth-secret-code");
      const captured = takeGithubCallback();
      expect(captured?.code).toBe("oauth-secret-code");
      expect(captured?.state).toBe(state);
    },
  );

  it.each(["#section-two", "#/plain/route"])(
    "leaves a callback-free fragment untouched (%s)",
    (hash) => {
      setUrl(`/org/integration${hash}`);
      captureAndStripGithubCallback();
      expect(window.location.hash).toBe(hash);
      expect(takeGithubCallback()).toBeNull();
    },
  );

  it.each([
    "/org/integration",
    "/dashboard/gate/github",
    "/org/gate/integration",
    "/org/gate/github",
    "/some/future/gate/callback",
  ])("captures + strips on ANY path GitHub lands on, canonical or alias (%s)", (path) => {
    // GitHub's post-install redirect can land on the canonical path or a legacy
    // alias that React-Router later redirects to it. Capture runs once at boot,
    // BEFORE the redirect, so it must key on the PARAMS, not the path -- otherwise
    // the alias leaks AND the redirect arrives with nothing left to capture, so
    // the preview never starts.
    setUrl(`${path}?installation_id=777&state=s&code=oauth-secret-code`);
    captureAndStripGithubCallback();
    expect(window.location.search).toBe("");
    expect(window.location.pathname).toBe(path); // path preserved; only params stripped
    const captured = takeGithubCallback();
    expect(captured?.code).toBe("oauth-secret-code");
    expect(captured?.installation_id).toBe("777");
  });

  it("is a no-op on a path with no callback param", () => {
    setUrl("/org/integration");
    captureAndStripGithubCallback();
    expect(takeGithubCallback()).toBeNull();

    setUrl("/knowledge?dataset=orders&tab=lineage");
    captureAndStripGithubCallback();
    expect(takeGithubCallback()).toBeNull();
    expect(window.location.search).toBe("?dataset=orders&tab=lineage"); // untouched
  });
});

describe("the callback never reaches analytics", () => {
  it("a telemetry sink run in the real boot order never receives the callback URL", () => {
    // The honest model: telemetry (GA's page_location, Sentry's request URL) reads
    // window.location when it runs. main.tsx runs the strip FIRST, then telemetry.
    // A spy standing in for a telemetry sink records location.href when invoked --
    // BEFORE the strip it sees the code (proving the spy is not vacuous), AFTER the
    // strip it must not. (A prior "GA loads and appends a <script>" test was
    // dropped: jsdom never executes gtag.js, so element existence proved nothing;
    // this spy IS the real invariant -- whatever a sink reads is code-free.)
    setUrl(CALLBACK);
    const seen: string[] = [];
    const telemetrySink = (): void => void seen.push(window.location.href);

    telemetrySink(); // sink runs before the strip -> WOULD leak (sanity)
    expect(seen[0]).toContain("oauth-secret-code");

    captureAndStripGithubCallback(); // main.tsx's first statement
    telemetrySink(); // sink runs after the strip, as in the real boot order

    expect(seen[1]).not.toContain("oauth-secret-code");
    expect(seen[1]).not.toContain("signed-state");
    expect(seen[1]).not.toContain("777");
  });

  // WHY the runtime invariant ("the strip runs before anything reads the
  // code-bearing URL") holds, and how these two tests pin it. The chosen approach
  // is DOCUMENT + PLACEMENT + a broadened direct-module scan (a full transitive
  // static-import scan is infeasible to do reliably in a unit test -- it would
  // have to resolve every re-export, alias, and node_modules edge). The runtime
  // guarantee rests on:
  //   1. main.tsx's strip is its FIRST executable statement -- nothing runs
  //      between the imports and it (placement test below).
  //   2. ES modules evaluate imports before that statement, so the only code that
  //      could read the URL first is a statically-imported module's MODULE-SCOPE
  //      code. The boot modules that run then (analytics/sentry/error handlers)
  //      read only location.hostname, and only inside their init FUNCTIONS, which
  //      main.tsx calls AFTER the strip -- pinned by the scan below.
  //   3. The URL-reading telemetry (the GA + Sentry SDKs) reads location at
  //      RUNTIME inside those init functions, never at import; the App/router
  //      mounts last (createRoot). So no path reads the query before the strip.
  it("NOTHING executes in main.tsx before the strip (not just the named boot calls)", async () => {
    const main = await import("@/app/boot/startPortal.tsx?raw").then((m) => m.default as string);
    // The executable body starts after the LAST import statement.
    const body = main.slice(main.lastIndexOf("\nimport "));
    const strip = body.indexOf("captureAndStripGithubCallback()");
    expect(strip).toBeGreaterThanOrEqual(0);

    // Everything between the end of the last import line and the strip call must
    // be ONLY comments and whitespace -- no executable statement (e.g. a
    // `sendTelemetry(location.href)` sneaked in before the strip) may run first.
    // This is stronger than checking a fixed list of named calls.
    const importEnd = body.indexOf("\n", 1);
    const between = body.slice(importEnd, strip);
    const executable = between
      .replace(/\/\*[\s\S]*?\*\//g, "") // block comments
      .replace(/\/\/[^\n]*/g, "") // line comments
      .replace(/\s+/g, ""); // whitespace
    expect(executable, "no code may run between the imports and the strip").toBe("");
    // And no URL read of any form (in CODE, not prose) precedes the strip: strip
    // comments first so the docstring's mention of location.search doesn't trip it.
    const preStripCode = main
      .slice(0, main.indexOf("captureAndStripGithubCallback()"))
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/\/\/[^\n]*/g, "");
    expect(preStripCode, "no boot code may read the URL before the strip").not.toMatch(
      /location\s*\.\s*(href|search)|location\s*\[|(?:document|window)\s*\.\s*URL/,
    );
  });

  it("no statically-imported boot module reads the code-bearing URL at module scope", async () => {
    // The strip runs AFTER main.tsx's static imports (ESM evaluates them first),
    // so a boot module reading the query-bearing URL at module scope would see the
    // un-stripped URL. Guard the boot modules that run at import/init time: they
    // may read location.hostname (host gate), never href/search/document.URL --
    // in any dotted, bracket, aliased, or destructured form. The product's
    // telemetry sinks carry the same check beside them (beside the hosted telemetry modules).
    const sources: Record<string, string> = {
      globalHandlers: (await import("@/app/boot/globalHandlers.ts?raw")).default,
    };
    const forbidden: [RegExp, string][] = [
      [/location\s*\.\s*(href|search)\b/, "location.href/.search"],
      [/location\s*\[\s*['"`](href|search)['"`]\s*\]/, "location['href'|'search']"],
      [/\b(document|window)\s*\.\s*URL\b/, "document.URL/window.URL"],
      [/\{[^}]*\b(href|search)\b[^}]*\}\s*=\s*.*location/, "destructured href/search from location"],
    ];
    for (const [name, src] of Object.entries(sources)) {
      for (const [pattern, label] of forbidden) {
        expect(src, `${name} must not read ${label} before the callback is stripped`).not.toMatch(
          pattern,
        );
      }
    }
  });
});
