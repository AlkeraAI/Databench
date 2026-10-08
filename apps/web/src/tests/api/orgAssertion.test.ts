// A tab names the org it rendered, and a tab whose session moved to another org never
// acts in it. Drives the real `api` client (and the event stream's fetch wrapper) with
// `fetch` stubbed and the navigation captured through the activeOrg seam.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ORG_HEADER,
  SESSION_NOTICE_KEY,
  activeOrgId,
  enterOrg,
  forgetActiveOrg,
  setOrgChangedHandler,
  setOrgNavigator,
  takeSessionNotice,
  withOrgAssertion,
} from "@/api/activeOrg";
import { CHOOSER_AFTER_SIGN_IN, api, noteSessionExpiry, refreshSession, setUnauthorizedHandler } from "@/api/client";
import { clearStorageMirror, safeSessionStorage } from "@alkera/ui/storage";

const ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const json = (body: unknown, status = 200, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });

/** An access-token expiry a few minutes out, like a real one. */
const soon = () => new Date(Date.now() + 10 * 60_000).toISOString();

const orgChanged = () =>
  json({ error: { code: "org_changed", message: "You switched organizations in another window." } }, 409);

let sent: Request[];
let navigated: string[];
let dropped: number;
let unauthorized: number;
let restoreNavigator: (url: string) => void;

function serve(handler: (request: Request) => Response) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request | string, init?: RequestInit) => {
      const request = typeof input === "string" ? new Request(input, init) : input;
      sent.push(request);
      return handler(request);
    }),
  );
}

const pathOf = (request: Request) => new URL(request.url).pathname;

beforeEach(() => {
  sent = [];
  navigated = [];
  dropped = 0;
  unauthorized = 0;
  forgetActiveOrg();
  clearStorageMirror();
  safeSessionStorage().remove(SESSION_NOTICE_KEY);
  noteSessionExpiry(null);
  restoreNavigator = setOrgNavigator((url) => navigated.push(url));
  setOrgChangedHandler(() => {
    dropped += 1;
  });
  setUnauthorizedHandler(() => {
    unauthorized += 1;
  });
});

afterEach(() => {
  setOrgNavigator(restoreNavigator);
  setOrgChangedHandler(null);
  setUnauthorizedHandler(null);
  forgetActiveOrg();
  noteSessionExpiry(null);
  vi.unstubAllGlobals();
});

describe("the org assertion", () => {
  it("sends nothing until a response names the org, then names it on every request", async () => {
    serve(() => json({ ok: true }, 200, { [ORG_HEADER]: ORG_A }));
    await api.GET("/api/v1/auth/memberships");
    await api.GET("/api/v1/auth/memberships");
    expect(sent[0].headers.get(ORG_HEADER)).toBeNull();
    expect(sent[1].headers.get(ORG_HEADER)).toBe(ORG_A);
    expect(activeOrgId()).toBe(ORG_A);
  });

  it("reloads with a notice on a 409 org_changed and never retries the request", async () => {
    enterOrg(ORG_A, "Acme");
    serve((request) => (pathOf(request) === "/api/v1/auth/memberships" ? orgChanged() : json({}, 404)));
    const refused = await api.GET("/api/v1/auth/memberships");
    expect(refused.response.status).toBe(409);
    expect(sent).toHaveLength(1);
    expect(sent[0].headers.get(ORG_HEADER)).toBe(ORG_A);
    expect(navigated).toEqual(["/"]);
    expect(dropped).toBe(1);
    expect(takeSessionNotice()).toBe("Switched organizations in another window.");
    expect(unauthorized).toBe(0);
  });

  it("handles a burst of refusals as one reload", async () => {
    enterOrg(ORG_A);
    serve(() => orgChanged());
    await Promise.all([api.GET("/api/v1/auth/memberships"), api.GET("/api/v1/auth/memberships")]);
    expect(navigated).toEqual(["/"]);
    expect(dropped).toBe(1);
  });

  it("treats a response echoing another org as the session having moved", async () => {
    enterOrg(ORG_A, "Acme");
    serve(() => json({ ok: true }, 200, { [ORG_HEADER]: ORG_B }));
    await api.GET("/api/v1/auth/memberships");
    expect(navigated).toEqual(["/"]);
  });

  it("does not take the switch route's echo of the org being entered as a move elsewhere", async () => {
    enterOrg(ORG_A);
    serve(() =>
      json({ user: { org_team_id: ORG_B }, expires_at: soon() }, 200, { [ORG_HEADER]: ORG_B }),
    );
    await api.POST("/api/v1/auth/refresh/org", { body: { org_team_id: ORG_B } });
    expect(navigated).toEqual([]);
  });
});

describe("the refresh", () => {
  it("reloads when the refresh answers for another org than the tab rendered", async () => {
    enterOrg(ORG_A, "Acme");
    serve(() => json({ user: { org_team_id: ORG_B, org_name: "Beta" }, expires_at: soon() }));
    expect(await refreshSession()).toBe(false);
    expect(navigated).toEqual(["/"]);
    expect(takeSessionNotice()).toBe("Switched to Beta in another window.");
  });

  it("carries on when the refresh answers for the same org", async () => {
    enterOrg(ORG_A);
    serve(() => json({ user: { org_team_id: ORG_A }, expires_at: soon() }));
    expect(await refreshSession()).toBe(true);
    expect(navigated).toEqual([]);
  });

  it("sends an SSO step-up to the org's sign-in instead of the login page", async () => {
    const loginUrl = "http://api.test/api/v1/auth/sso/x/login";
    serve(() =>
      json(
        { error: { code: "sso_required", message: "SSO", details: { login_url: loginUrl } } },
        401,
      ),
    );
    expect(await refreshSession()).toBe(false);
    expect(navigated).toEqual([loginUrl]);
    expect(unauthorized).toBe(0);
  });

  it("sends a session whose org membership ended through sign-in to the chooser", async () => {
    enterOrg(ORG_A);
    serve(() => json({ error: { code: "session_org_revoked", message: "gone" } }, 401));
    expect(await refreshSession()).toBe(false);
    expect(navigated).toEqual([CHOOSER_AFTER_SIGN_IN]);
    expect(activeOrgId()).toBeNull();
    expect(unauthorized).toBe(0);
  });

  it("hands any other refusal to the plain sign-out", async () => {
    serve(() => json({ error: { code: "session_revoked", message: "gone" } }, 401));
    expect(await refreshSession()).toBe(false);
    expect(navigated).toEqual([]);
    expect(unauthorized).toBe(1);
  });
});

describe("the event stream's fetch", () => {
  it("names the org and turns a 409 org_changed into the reload", async () => {
    enterOrg(ORG_A);
    const inner = vi.fn(async (input: Request) => {
      sent.push(input);
      return orgChanged();
    });
    const wrapped = withOrgAssertion(inner);
    const response = await wrapped(new Request("http://api.test/api/v1/events"));
    expect(response.status).toBe(409);
    expect(sent[0].headers.get(ORG_HEADER)).toBe(ORG_A);
    expect(navigated).toEqual(["/"]);
  });
});
