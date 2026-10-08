// The sanitizer every error sink runs its strings through.
//
// Each case is a credential that reached a log somewhere, or the shape of one:
// a reset token in a path, an invite in a query, an OAuth response in a
// fragment, a bearer token in a message, a JWT in a stack frame. The negative
// cases matter as much — a report that redacts the route it happened on is a
// report nobody can act on.

import { describe, expect, it } from "vitest";

import { REDACTED, scrubText, scrubUrl } from "@/app/boot/scrub";

describe("scrubUrl", () => {
  it.each([
    ["a bare route survives whole", "/plugins", "/plugins"],
    ["a benign query survives", "/usage?scope=org&page=2", "/usage?scope=org&page=2"],
    [
      "an absolute URL keeps its origin",
      "https://app.example.com/files?parent=abc",
      "https://app.example.com/files?parent=abc",
    ],
    [
      "a reset token in the path goes, the page stays",
      "/reset-password/8f2c-secret-token",
      `/reset-password/${REDACTED}`,
    ],
    ["a verify-email token in the path goes", "/verify-email/abc123", `/verify-email/${REDACTED}`],
    ["the bare page with no token is not a credential", "/reset-password", "/reset-password"],
    ["an invite in the query goes", "/signup?invite=abc123", `/signup?invite=${REDACTED}`],
    ["a device user_code goes", "/device?user_code=WDJB-MJHT", `/device?user_code=${REDACTED}`],
    [
      "an OAuth code and state both go",
      "/callback?code=4%2F0Ab&state=xyz",
      `/callback?code=${REDACTED}&state=${REDACTED}`,
    ],
    [
      "a redirect_uri goes, the route it is on stays",
      "/cli-login?redirect_uri=http%3A%2F%2F127.0.0.1%3A5000%2Fcallback&state=s",
      `/cli-login?redirect_uri=${REDACTED}&state=${REDACTED}`,
    ],
    [
      "a nested return_to goes rather than being followed",
      "/login?return_to=%2Fsignup%3Finvite%3Dabc",
      `/login?return_to=${REDACTED}`,
    ],
    [
      "the whole fragment goes — an implicit-grant token lives there",
      "/#access_token=abc123&token_type=bearer",
      `/#${REDACTED}`,
    ],
    [
      "a router-style fragment goes too",
      "/app?page=2#/route?invite=abc",
      `/app?page=2#${REDACTED}`,
    ],
    [
      "userinfo in the authority goes",
      "https://someone:hunter2@app.example.com/files",
      "https://app.example.com/files",
    ],
    [
      "a benign param keeps its value beside a redacted one",
      "/usage?scope=org&oauth_ticket=t",
      `/usage?scope=org&oauth_ticket=${REDACTED}`,
    ],
    [
      "a repeated credential param is redacted in every place",
      "/a?token=1&token=2",
      `/a?token=${REDACTED}`,
    ],
    ["a string that will not parse as a URL is redacted whole", "http://", REDACTED],
    // A scheme this function has no page structure for is handed back intact,
    // not resolved against the stand-in origin (which turns `about:blank` into "").
    ["another scheme survives whole", "about:blank", "about:blank"],
    [
      "a blob URL survives whole",
      "blob:http://app.example.com/8f2c-1",
      "blob:http://app.example.com/8f2c-1",
    ],
    [
      "a webview URL survives whole",
      "vscode-webview://abc/index.html?id=1",
      "vscode-webview://abc/index.html?id=1",
    ],
    [
      "a protocol-relative href keeps its host rather than being resolved away",
      "//evil.example/steal?x=1",
      "//evil.example/steal?x=1",
    ],
  ])("%s", (_label, href, expected) => {
    expect(scrubUrl(href)).toBe(expected);
  });

  it("still sweeps an unknown scheme for token shapes", () => {
    expect(scrubUrl("chrome-extension://abc/x?token=LIVE-TOKEN")).not.toContain("LIVE-TOKEN");
    expect(scrubUrl("//evil.example/x?password=LIVE-TOKEN")).not.toContain("LIVE-TOKEN");
  });

  // The structural sweep knows the routes and parameters that carry a
  // credential today. A route nobody has written yet carries one in a path
  // segment, and those shapes are already redacted inside a message — so the
  // href gets the text sweep on top, which is what `reportClientError` sends.
  it.each([
    ["an invite token in an unlisted path", "/invite/s3cr3tT0kenValue0123456789abcdefghijklmn"],
    ["a magic-link token in an unlisted path", "/magic/abcdefghijklmnopqrstuvwxyz0123456789ABCD"],
    ["a JWT as a path segment", "/x/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sIgNaTuRe123"],
  ])("the text sweep catches %s that the route list does not know", (_label, href) => {
    // Structure alone leaves it whole — there is no rule for a route that does
    // not exist yet…
    const structural = scrubUrl(href);
    // …and the shape sweep is what catches it, exactly as it would in a message.
    expect(scrubText(structural)).toBe(scrubText(href));
    expect(scrubText(structural)).toContain(REDACTED);
  });

  it("never leaves a known credential value behind", () => {
    const hrefs = [
      "/reset-password/LIVE-TOKEN",
      "/signup?invite=LIVE-TOKEN",
      "/#access_token=LIVE-TOKEN",
      "https://u:LIVE-TOKEN@app.example.com/",
      "/login?return_to=%2Fverify-email%2FLIVE-TOKEN",
    ];
    for (const href of hrefs) expect(scrubUrl(href)).not.toContain("LIVE-TOKEN");
  });
});

describe("scrubText", () => {
  it.each([
    [
      "ordinary prose is untouched",
      "Cannot read properties of undefined (reading 'name')",
      "Cannot read properties of undefined (reading 'name')",
    ],
    [
      "a stack frame keeps its module and loses its query",
      "at fetchPage (https://app.example.com/assets/index-a1b2c3d4.js:2:41)",
      "at fetchPage (https://app.example.com/assets/index-a1b2c3d4.js:2:41)",
    ],
    [
      "an ApiError message keeps the path and loses the token",
      "the request to https://app.example.com/api/v1/auth/reset?token=abc123 failed (404)",
      `the request to https://app.example.com/api/v1/auth/reset?token=${REDACTED} failed (404)`,
    ],
    [
      "a bearer token goes, the header name stays",
      "request failed with Authorization: Bearer abcdefghijklmnop",
      `request failed with Authorization: Bearer ${REDACTED}`,
    ],
    [
      "a JWT anywhere goes",
      "decode failed for eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
      `decode failed for ${REDACTED}`,
    ],
    [
      "a prefixed provider key goes",
      "upstream refused sk-proj-AbCdEfGhIjKlMnOp",
      `upstream refused ${REDACTED}`,
    ],
    [
      "a labelled secret keeps its label",
      'failed with api_key="0123456789abcdef"',
      `failed with api_key=${REDACTED}`,
    ],
    [
      "a long opaque run goes",
      "mismatch at 8f14e45fceea167a5a36dedd4bea2543f14e45fceea167a",
      `mismatch at ${REDACTED}`,
    ],
    [
      "a short build hash is not a secret",
      "chunk index-a1b2c3d4.js failed to load",
      "chunk index-a1b2c3d4.js failed to load",
    ],
  ])("%s", (_label, text, expected) => {
    expect(scrubText(text)).toBe(expected);
  });

  it("scrubs every credential in a message that carries several", () => {
    const text =
      "POST https://app.example.com/x?invite=LIVE-ONE failed; Bearer LIVE-TWO-abcdefgh; token=LIVE-THREE";
    const out = scrubText(text);
    expect(out).not.toContain("LIVE-ONE");
    expect(out).not.toContain("LIVE-TWO");
    expect(out).not.toContain("LIVE-THREE");
  });
});
