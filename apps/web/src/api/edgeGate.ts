// The one visit an edge sign-in gate needs.
//
// An environment may put a sign-in gate in front of the API host itself
// (staging's Okta action at the load balancer). Such a gate sets its session
// cookie only on a top-level navigation. The app's own requests are cross-origin
// fetches, so until that visit every one of them meets the gate's redirect,
// which the browser refuses to follow: a TypeError, no status, no body — the
// same shape as an outage. The app cannot tell the two apart, so it sends the
// browser through the API host once: the gate runs on the way in, the API
// answers with a redirect back to the path the app was on, and the requests
// work from then on.
//
// Once per page load. If the visit did not help, the failure is a real one and
// the pages show it; where the browser refuses to keep session storage there is
// no way to remember the visit across the navigation, so the app never bounces
// at all rather than risk a loop.

import { type SafeStorage, safeSessionStorage } from "@alkera/ui/storage";

/** The API route that sends the browser back to the app. */
export const GATE_RETURN_PATH = "/api/v1/auth/gate/return";

const BOUNCED_KEY = "alk.edgeGate.bounced";

/** The only request shapes that may leave the page: a read the app will simply
 *  make again once it is back. A write is the person's own action. */
const BOUNCING_METHODS: ReadonlySet<string> = new Set(["GET", "HEAD"]);

export interface EdgeGateDeps {
  apiBaseUrl: string;
  location: Pick<Location, "origin" | "pathname" | "search" | "hash" | "assign">;
  /** Where the visit is remembered. `set` answers whether the browser kept it:
   *  a refused write would only be remembered in memory, which a navigation
   *  loses, so such a page never bounces. */
  storage: Pick<SafeStorage, "get" | "set" | "remove">;
}

/** The browser's own location and session storage. */
export function edgeGateDeps(apiBaseUrl: string): EdgeGateDeps {
  return { apiBaseUrl, location: window.location, storage: safeSessionStorage() };
}

function originOf(base: string): string | null {
  try {
    return new URL(base).origin;
  } catch {
    return null;
  }
}

/** Whether the API lives on another origin than the page (a gate at the API's
 *  edge only ever matters then: same-origin, the page itself went through it). */
export function apiIsCrossOrigin(deps: Pick<EdgeGateDeps, "apiBaseUrl" | "location">): boolean {
  const origin = originOf(deps.apiBaseUrl);
  return origin !== null && origin !== deps.location.origin;
}


/**
 * A request to the API failed before any response arrived. Send the browser
 * through the API host once, so an edge gate can set its cookie, and return
 * `true`; `false` when nothing is done — the API is same-origin, the request
 * was a write, this page load already bounced, or the browser would not keep
 * the memory of the visit.
 */
export function bounceThroughEdgeGate(request: Request, deps: EdgeGateDeps): boolean {
  if (!BOUNCING_METHODS.has(request.method.toUpperCase())) return false;
  if (!apiIsCrossOrigin(deps)) return false;
  if (deps.storage.get(BOUNCED_KEY) !== null) return false;
  if (!deps.storage.set(BOUNCED_KEY, new Date().toISOString())) {
    deps.storage.remove(BOUNCED_KEY);
    return false;
  }
  const back = `${deps.location.pathname}${deps.location.search}${deps.location.hash}`;
  deps.location.assign(`${deps.apiBaseUrl}${GATE_RETURN_PATH}?next=${encodeURIComponent(back)}`);
  return true;
}

/** A request reached the API: the gate, if there is one, is open, and a later
 *  failure (its cookie lapsing days from now) may bounce once more. */
export function noteApiReachable(deps: Pick<EdgeGateDeps, "storage">): void {
  if (deps.storage.get(BOUNCED_KEY) !== null) deps.storage.remove(BOUNCED_KEY);
}
