import { StrictMode, isValidElement, type ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The emailed entry links carry a LIVE single-use credential in the URL: the
// reset / verification token as a path segment, the invitation token and the
// OAuth register ticket as a query param. Hosted telemetry copies the full href
// to a third party (GA4's page_location, Sentry's request.url), where anyone
// with read access to the property can spend the token before it expires. The
// pages need the token in the URL, so main.tsx can't strip it — instead it holds
// telemetry back until the address bar is credential-free. These pin that: the
// sinks are never even invoked while a credential is in the URL, and whatever
// href they DO see once they start carries none of it.

const { initSentry, initAnalytics, seenHrefs, rendered } = vi.hoisted(() => {
  const seen: string[] = [];
  const record = (): Promise<void> => {
    seen.push(window.location.href);
    return Promise.resolve();
  };
  return {
    seenHrefs: seen,
    rendered: [] as unknown[],
    initSentry: vi.fn(record),
    initAnalytics: vi.fn(record),
  };
});

vi.mock("@/app/boot/globalHandlers", () => ({ installGlobalErrorHandlers: vi.fn() }));
vi.mock("@/app/boot/RootErrorBoundary", () => ({ RootErrorBoundary: () => null }));
vi.mock("@/App", () => ({ App: () => null }));
vi.mock("react-dom/client", () => ({
  createRoot: () => ({
    render: (element: unknown) => rendered.push(element),
    unmount: vi.fn(),
  }),
}));

// The boot wraps history.pushState/replaceState while it waits; restore the
// pristine ones between cases so a previous boot can't observe the next setUrl.
const pristinePushState = window.history.pushState.bind(window.history);
const pristineReplaceState = window.history.replaceState.bind(window.history);

const RESET_TOKEN = "reset-token-do-not-leak";
const INVITE_TOKEN = "invite-token-do-not-leak";
const TICKET = "oauth-ticket-do-not-leak";

let bootCount = 0;

/** Load the portal boot as the browser would, on `url`. */
async function boot(url: string): Promise<void> {
  pristineReplaceState({}, "", url);
  vi.resetModules();
  const { startPortal } = await import("@/app/boot/startPortal");
  const { PORTAL_TELEMETRY } = await import("@/app/extensions/portal");
  // Two sinks registered the way a product registers its own; the open build has none.
  bootCount += 1;
  startPortal([
    {
      name: `test.telemetry.${bootCount}`,
      install: () => {
        PORTAL_TELEMETRY.register({ key: "sentry", start: initSentry });
        PORTAL_TELEMETRY.register({ key: "analytics", start: initAnalytics });
      },
    },
  ]);
}

function telemetryStarted(): boolean {
  return initSentry.mock.calls.length > 0 || initAnalytics.mock.calls.length > 0;
}

beforeEach(() => {
  window.history.pushState = pristinePushState;
  window.history.replaceState = pristineReplaceState;
  document.body.innerHTML = '<div id="root"></div>';
  seenHrefs.length = 0;
  rendered.length = 0;
  initSentry.mockClear();
  initAnalytics.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  pristineReplaceState({}, "", "/");
});

describe("telemetry boot gate", () => {
  it.each([
    ["the app root", "/"],
    ["a signed-in page", "/dashboard"],
    ["a login carrying a parameter", "/login?via=pro"],
    // ASYMMETRY: the credential is the token SEGMENT, not the route. A bare
    // /reset-password (the expired-link page) and a plain /signup carry nothing,
    // and an over-eager prefix match would blind analytics on every sign-up.
    ["the expired-link page (no token segment)", "/reset-password"],
    ["the bare verify route", "/verify-email"],
    ["plain signup", "/signup"],
    ["signup with only carried params", "/signup?via=pro&allow_personal=1"],
    ["the device page with no code", "/device"],
    // ASYMMETRY: a return_to naming the invites TAB is not an invite token, and
    // "invites" is not the `invite` param — neither may blind analytics.
    [
      "a return_to to the invites tab",
      `/login?return_to=${encodeURIComponent("/teams?tab=invites")}`,
    ],
  ])("starts telemetry immediately on %s", async (_label, url) => {
    await boot(url);
    expect(initSentry).toHaveBeenCalledTimes(1);
    expect(initAnalytics).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["a password-reset link", `/reset-password/${RESET_TOKEN}`, RESET_TOKEN],
    ["an email-verification link", `/verify-email/${RESET_TOKEN}`, RESET_TOKEN],
    ["an invitation link", `/signup?invite=${INVITE_TOKEN}`, INVITE_TOKEN],
    ["an OAuth register ticket", `/signup?oauth_ticket=${TICKET}`, TICKET],
    ["a ticket alongside other params", `/signup?via=pro&oauth_ticket=${TICKET}`, TICKET],
    ["a device user code", "/device?user_code=WDJB-MJHT", "WDJB-MJHT"],
    // A fragment never reaches the server but is fully readable by in-page JS,
    // so a telemetry sink would copy it just the same.
    ["a credential in the hash fragment", `/signup#invite=${INVITE_TOKEN}`, INVITE_TOKEN],
    [
      "a credential after a router-style fragment",
      `/x#/signup?invite=${INVITE_TOKEN}`,
      INVITE_TOKEN,
    ],
    // The auth guard folds the current location into /login?return_to=<encoded
    // URL>, so the credential rides one level down — still in the href.
    [
      "a credential nested in a return_to",
      `/login?return_to=${encodeURIComponent("/device?user_code=WDJB-MJHT")}`,
      "user_code",
    ],
    [
      "a reset token nested in a return_to",
      `/login?return_to=${encodeURIComponent(`/reset-password/${RESET_TOKEN}`)}`,
      RESET_TOKEN,
    ],
  ])("never starts a telemetry sink on %s", async (_label, url, secret) => {
    await boot(url);
    expect(telemetryStarted()).toBe(false);
    expect(seenHrefs).toEqual([]);
    expect(window.location.href).toContain(secret); // the flow still has its token
  });

  it("starts telemetry on the first credential-free navigation, never seeing the token", async () => {
    await boot(`/reset-password/${RESET_TOKEN}`);
    expect(telemetryStarted()).toBe(false);

    // The reset page navigates to /login once the password is set — react-router
    // moves through history.pushState.
    window.history.pushState({}, "", "/login");

    expect(initSentry).toHaveBeenCalledTimes(1);
    expect(initAnalytics).toHaveBeenCalledTimes(1);
    expect(seenHrefs).toHaveLength(2);
    for (const href of seenHrefs) {
      expect(href).not.toContain(RESET_TOKEN);
      expect(href).toContain("/login");
    }
  });

  it("keeps waiting while one credential URL replaces another", async () => {
    // The OAuth signup hops ?invite= → ?oauth_ticket=; neither may reach a sink.
    await boot(`/signup?invite=${INVITE_TOKEN}`);
    window.history.pushState({}, "", `/signup?oauth_ticket=${TICKET}`);
    expect(telemetryStarted()).toBe(false);

    window.history.replaceState({}, "", "/complete-profile");

    expect(telemetryStarted()).toBe(true);
    for (const href of seenHrefs) {
      expect(href).not.toContain(INVITE_TOKEN);
      expect(href).not.toContain(TICKET);
    }
  });

  it("starts only once, not again on every later navigation", async () => {
    await boot(`/verify-email/${RESET_TOKEN}`);
    window.history.pushState({}, "", "/");
    window.history.pushState({}, "", "/dashboard");
    window.history.replaceState({}, "", "/teams");

    expect(initSentry).toHaveBeenCalledTimes(1);
    expect(initAnalytics).toHaveBeenCalledTimes(1);
  });

  // Patching history is a crowded pattern — Sentry's navigation breadcrumbs,
  // GA4 enhanced measurement and router/analytics shims all do it, and they
  // install AFTER boot, on top of the gate's own wrapper. Handing telemetry over
  // must not take their patch down with it.
  it("leaves a later library's history patch installed when it starts telemetry", async () => {
    await boot(`/reset-password/${RESET_TOKEN}`);

    // A library wraps whatever is currently there — i.e. the gate's wrapper.
    const wrapped = window.history.pushState;
    const libraryUrls: string[] = [];
    window.history.pushState = function libraryPushState(
      ...args: Parameters<History["pushState"]>
    ): void {
      libraryUrls.push(String(args[2]));
      wrapped.apply(window.history, args);
    };

    window.history.pushState({}, "", "/login");
    expect(telemetryStarted()).toBe(true);

    // The library must still see every later navigation, and the URL must still
    // actually change (the chain stays intact end to end).
    window.history.pushState({}, "", "/dashboard");
    expect(libraryUrls).toEqual(["/login", "/dashboard"]);
    expect(window.location.pathname).toBe("/dashboard");
  });

  it("starts telemetry only once when its wrapper has to stay in the chain", async () => {
    await boot(`/signup?invite=${INVITE_TOKEN}`);
    const wrapped = window.history.pushState;
    window.history.pushState = function libraryPushState(
      ...args: Parameters<History["pushState"]>
    ): void {
      wrapped.apply(window.history, args);
    };

    window.history.pushState({}, "", "/dashboard");
    window.history.pushState({}, "", "/teams");
    window.history.pushState({}, "", "/settings");

    expect(initSentry).toHaveBeenCalledTimes(1);
    expect(initAnalytics).toHaveBeenCalledTimes(1);
  });

  it("leaves window.history as it found it, not shadowing a later prototype patch", async () => {
    // pushState/replaceState live on History.prototype; putting the captured
    // original back as an OWN property on window.history would shadow anything
    // that patches the prototype afterwards.
    delete (window.history as Partial<History>).pushState;
    await boot(`/verify-email/${RESET_TOKEN}`);
    window.history.pushState({}, "", "/login");
    expect(telemetryStarted()).toBe(true);

    const protoPushState = History.prototype.pushState;
    const protoUrls: string[] = [];
    History.prototype.pushState = function patchedPrototype(
      this: History,
      ...args: Parameters<History["pushState"]>
    ): void {
      protoUrls.push(String(args[2]));
      protoPushState.apply(this, args);
    };
    try {
      window.history.pushState({}, "", "/dashboard");
      expect(protoUrls).toEqual(["/dashboard"]);
      expect(window.location.pathname).toBe("/dashboard");
    } finally {
      History.prototype.pushState = protoPushState;
    }
  });

  it("fails CLOSED on a URL it cannot parse", async () => {
    // Anything that makes the check itself fail must hold telemetry back, not
    // wave it through — the unreadable URL is exactly the one we can't clear.
    // Only the gate's own parse of the address bar fails: the module runner
    // that loads main.tsx builds URLs through this same global and has to keep
    // working for the boot to reach the check at all.
    const RealURL = URL;
    vi.stubGlobal(
      "URL",
      class extends RealURL {
        constructor(input: string | URL, base?: string | URL) {
          if (String(input) === window.location.href) throw new TypeError("unparseable");
          super(input, base);
        }
      },
    );
    await boot("/dashboard");
    expect(telemetryStarted()).toBe(false);
  });
});

// StrictMode's dev remount is not a lint: it re-attaches a subtree's effects
// without re-rendering the parent that owns them, which is the same shape as a
// Suspense re-reveal, an error-boundary reset and a keyed remount in
// production. Every chat address in the dev build crashed on exactly that,
// because the runtime a layout installed in render was cleared by its effect
// cleanup — and the tests that would have caught it rendered without this
// wrapper. Dropping it here to quiet a crash would hide the next one in
// production, so it stays pinned.
describe("the lifetime the app runs its tree under", () => {
  it("renders the app inside StrictMode", async () => {
    await boot("/dashboard");

    expect(rendered).toHaveLength(1);
    const root = rendered[0];
    expect(isValidElement(root)).toBe(true);
    expect((root as ReactElement).type).toBe(StrictMode);
  });

  it("puts the whole app under it, not an empty wrapper", async () => {
    const { App } = await import("@/App");
    await boot("/dashboard");

    const types: unknown[] = [];
    const walk = (node: unknown): void => {
      if (Array.isArray(node)) {
        node.forEach(walk);
        return;
      }
      if (!isValidElement(node)) return;
      types.push(node.type);
      walk((node.props as { children?: unknown }).children);
    };
    walk(rendered[0]);
    // The mocked App stands in for the real one; what matters is that it is
    // BELOW StrictMode rather than beside it.
    expect(types).toContain(App);
    expect(types.indexOf(StrictMode)).toBeLessThan(types.indexOf(App));
  });
});
