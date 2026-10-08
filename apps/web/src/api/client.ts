// Module-level Alkera API client for the new web portal.
//
// Same-origin by default — Vite's dev server proxies /api to the backend (see
// vite.config.ts), and the production build is served same-origin behind nginx.
// Override VITE_API_BASE_URL to point at a non-local API. The session cookie
// rides every request (the SDK defaults `credentials: "include"`), which is how
// the portal authenticates: the same HTTP-only `alkera_session` cookie the rest
// of the product uses, no token plumbing in the SPA.
//
// We default the baseUrl to `window.location.origin` (not "") because
// openapi-fetch builds URLs with `new URL(baseUrl + path)`, which throws on a
// relative path even in the browser.

import { createClient } from "@alkera/sdk";

import { ORG_HEADER, assertOrg, forgetActiveOrg, goTo, noteOrg, orgRefused } from "./activeOrg";
import { bounceThroughEdgeGate, edgeGateDeps, noteApiReachable } from "./edgeGate";
import { ApiError, failureSentence } from "./errors";
import { gatedFetch } from "./readGate";

const envBaseUrl = import.meta.env.VITE_API_BASE_URL as string | undefined;
const fallbackBaseUrl =
  typeof window !== "undefined" && window.location?.origin
    ? window.location.origin
    : "http://localhost";

export const apiBaseUrl = envBaseUrl ?? fallbackBaseUrl;

/** Every typed read in the portal goes out through the read gate, so a refusal
 *  the server hands one query holds the rest of the page's polls too (see
 *  `readGate.ts`). Writes pass straight through it. */
export const api = createClient({ baseUrl: apiBaseUrl, fetch: gatedFetch });

// --- Global 401 handling ----------------------------------------------------
//
// The app's error architecture has exactly two lanes:
//   • 401 (session expired/revoked mid-use)  → GLOBAL: drop the cached identity
//     and let the route guards bounce to /login (wired via the handler below).
//   • everything else (4xx/5xx)              → LOCAL: each page surfaces the
//     reason through its own error state.
// This keeps "you're logged out" a single, app-wide concern instead of every
// query reinventing it, while ordinary failures stay close to where they happen.

/** The `/auth/me` probe IS the "am I signed in?" check — a 401 there is the
 *  normal signed-out signal that `useCurrentUser` + `RequireAuth` already
 *  handle, so it must NOT trigger the global redirect (which would loop on the
 *  login page). Every OTHER endpoint's 401 means a mid-session expiry. */
const ME_PROBE_PATH = "/api/v1/auth/me";

let unauthorizedHandler: (() => void) | null = null;

/** Register the app's reaction to a mid-session 401 (the bridge in the app root
 *  wires this to "drop the cached identity → guards redirect to /login"). Pass
 *  null to unregister. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

/** Fire the registered mid-session-401 reaction from a transport that does not go
 *  through `api` — the server event stream and the realtime socket authenticate
 *  with the same session and must hand the user to login the same way. */
export function fireUnauthorized(): void {
  unauthorizedHandler?.();
}

// --- Silent refresh ---------------------------------------------------------
//
// The access token lives minutes; the HTTP-only refresh cookie (scoped to the
// refresh route, so it never rides an ordinary request) keeps the session. Two
// triggers renew it, both funnelled through ONE in-flight refresh so a burst of
// concurrent requests pays a single round-trip:
//   • a 401 whose code is `token_expired` — refresh, then retry that request once;
//   • the access token is within two minutes of its `exp` — refresh before the
//     request goes out, so a long-idle tab never pays a failed request first.
// Only `token_expired` invites a refresh: `session_revoked` and every other 401
// mean "signed out" and go to the global handler. A refused refresh does too.

const REFRESH_PATH = "/api/v1/auth/refresh";
/** Refresh once the access token is this close to lapsing. */
export const PROACTIVE_REFRESH_MS = 2 * 60_000;
/** How long after a refresh that failed for a transient reason to try again. */
export const RETRY_REFRESH_MS = 15_000;
export const TOKEN_EXPIRED_CODE = "token_expired";

let accessExpiresAt: number | null = null;
let inflightRefresh: Promise<boolean> | null = null;
let proactiveTimer: ReturnType<typeof setTimeout> | null = null;

/** Remember when the current access token lapses (the `expires_at` of a login /
 *  refresh answer, or `session_expires_at` of `/auth/me`) and arm the proactive
 *  refresh for two minutes before it. `null` forgets it (signed out). */
export function noteSessionExpiry(iso: string | null | undefined): void {
  if (proactiveTimer !== null) {
    clearTimeout(proactiveTimer);
    proactiveTimer = null;
  }
  const at = iso ? new Date(iso).getTime() : NaN;
  accessExpiresAt = Number.isFinite(at) ? at : null;
  if (accessExpiresAt === null) return;
  const delay = Math.max(0, accessExpiresAt - PROACTIVE_REFRESH_MS - Date.now());
  proactiveTimer = setTimeout(() => {
    proactiveTimer = null;
    void refreshSession();
  }, delay);
}

/** True when the access token is known to lapse within the proactive window. */
function nearExpiry(): boolean {
  return accessExpiresAt !== null && Date.now() >= accessExpiresAt - PROACTIVE_REFRESH_MS;
}

/**
 * A refresh failed for a reason that is not a refusal (a 5xx, the refresh route's own
 * throttle, a dropped connection). The timer that fired has already cleared itself, so
 * without re-arming it the ONLY remaining trigger is a request going out through `api` —
 * and the transports that do not (the event stream reconnecting on its own deadline) would
 * then be the first thing to meet a lapsed cookie. Re-arm inside the window so the attempt
 * repeats while the current access token is still good, and stop once it isn't: past `exp`
 * the next request's `token_expired` is the trigger.
 */
function retry(): false {
  if (accessExpiresAt === null || proactiveTimer !== null) return false;
  if (Date.now() >= accessExpiresAt) return false;
  proactiveTimer = setTimeout(() => {
    proactiveTimer = null;
    // Re-checked on the way out, not only on the way in: the wait may have carried us past
    // `exp`, and past it the trigger is the next request's `token_expired` — or, on the
    // transport that makes no requests, the stream's own single refresh.
    if (accessExpiresAt !== null && Date.now() < accessExpiresAt) void refreshSession();
  }, RETRY_REFRESH_MS);
  return false;
}

/**
 * Exchange the refresh cookie for a fresh pair. Single-flight: every caller
 * during one refresh awaits the same promise. Resolves `true` on success; on
 * refusal (the session was revoked, expired, or never existed) forgets the
 * expiry, fires the global unauthorized reaction, and resolves `false`.
 */
export function refreshSession(): Promise<boolean> {
  if (inflightRefresh) return inflightRefresh;
  inflightRefresh = (async () => {
    try {
      const response = await fetch(
        new Request(`${apiBaseUrl}${REFRESH_PATH}`, {
          method: "POST",
          credentials: "include",
          headers: { accept: "application/json" },
        }),
      );
      if (response.status === 401) {
        // The server refused the refresh credential: the session is over, or the
        // org it is in wants a sign-in its own way first.
        noteSessionExpiry(null);
        if (routeRefusedRefresh(await refusal(response))) return false;
        fireUnauthorized();
        return false;
      }
      if (!response.ok) return retry(); // transient: come back before `exp`, not after it
      const body = (await response.json()) as {
        expires_at?: string;
        user?: { org_team_id?: string; org_name?: string };
      };
      // A refresh answers for the org the session is in now. Another org than the
      // one this tab rendered means a switch happened elsewhere: never carry on
      // in it silently.
      if (!noteOrg(body.user?.org_team_id, body.user?.org_name ?? null)) return false;
      noteSessionExpiry(body.expires_at ?? null);
      return true;
    } catch {
      // A network failure is not a refusal: leave the session as it was and try
      // again while the access token is still good.
      return retry();
    } finally {
      inflightRefresh = null;
    }
  })();
  return inflightRefresh;
}

async function bodyCode(response: Response): Promise<string | null> {
  return (await refusal(response)).code;
}

interface Refusal {
  code: string | null;
  loginUrl: string | null;
}

async function refusal(response: Response): Promise<Refusal> {
  try {
    const body = (await response.clone().json()) as {
      error?: { code?: unknown; details?: { login_url?: unknown } };
    };
    const code = body?.error?.code;
    const loginUrl = body?.error?.details?.login_url;
    return {
      code: typeof code === "string" ? code : null,
      loginUrl: typeof loginUrl === "string" && loginUrl ? loginUrl : null,
    };
  } catch {
    return { code: null, loginUrl: null };
  }
}

/** The refresh codes that send the browser somewhere other than the plain sign-in. */
export const SESSION_ORG_REVOKED_CODE = "session_org_revoked";
export const SSO_REQUIRED_CODE = "sso_required";
/** Where a session whose org membership ended goes: a fresh sign-in, then the
 *  chooser among the orgs the person still has. */
export const CHOOSER_AFTER_SIGN_IN = `/login?return_to=${encodeURIComponent("/choose-org")}`;

/**
 * A refresh the server refused for a reason with somewhere better to go than the
 * plain sign-in: the org's single sign-on when the org wants it (the session
 * stands, and its IdP steps it up), or the chooser when the membership in the
 * session's org ended. True when the refusal was routed here.
 */
function routeRefusedRefresh({ code, loginUrl }: Refusal): boolean {
  if (code === SSO_REQUIRED_CODE && loginUrl) {
    goTo(loginUrl);
    return true;
  }
  if (code === SESSION_ORG_REVOKED_CODE) {
    forgetActiveOrg();
    goTo(CHOOSER_AFTER_SIGN_IN);
    return true;
  }
  return false;
}

/** The route that moves the session into another org. Its answer names the org
 *  being entered, which is the point of it, so its echo is not a sign that the
 *  session moved elsewhere; the hook that called it adopts the new org. */
export const SWITCH_ORG_PATH = "/api/v1/auth/refresh/org";

// --- The session around every request ----------------------------------------
//
// Two senders share these steps: the typed client's middleware below, and
// `withSession` for the requests that do not go through the typed client (the
// chat transport, the upload, the hand-written routes). Whichever one a request
// leaves through, it names the tab's org, renews a token about to lapse, and
// has its answer read the same way.

/** The two session routes are themselves the renewal, so they neither wait for
 *  one nor are retried after one. */
function renewsSession(path: string): boolean {
  return path === REFRESH_PATH || path === SWITCH_ORG_PATH;
}

/** Before a request leaves, besides naming the org (`assertOrg`): renew an
 *  access token about to lapse, so the request does not spend itself on a
 *  `token_expired`. Returns the renewal to wait for, or null when there is
 *  none: a request with nothing to wait for leaves in the tick it was made, as
 *  a plain `fetch` would. */
function renewal(path: string): Promise<boolean> | null {
  return !renewsSession(path) && nearExpiry() ? refreshSession() : null;
}

/**
 * Read an answer for what it says about the session. True when the request
 * should be sent once more: it was refused with `token_expired`, it may be
 * retried, and the refresh that followed succeeded.
 *
 * A 409 `org_changed` sends the tab through the org-changed reload and is never
 * retried, because the request was meant for the org this tab rendered. Any
 * other 401 (away from the sign-in probe) is the mid-session sign-out.
 */
async function afterAnswer(response: Response, path: string, retryable: boolean): Promise<boolean> {
  noteApiReachable(edgeGateDeps(apiBaseUrl));
  if (await orgRefused(response)) return false;
  // `headers` is read defensively: a `fetch` handed in from outside (a test
  // double, a host bridge) may answer with a Response-like that carries none,
  // and an answer that names no org says nothing about the session.
  const headers = response.headers as Headers | undefined;
  if (path !== SWITCH_ORG_PATH) noteOrg(headers?.get(ORG_HEADER));
  if (response.status !== 401 || path === REFRESH_PATH) return false;
  if (retryable && (await bodyCode(response)) === TOKEN_EXPIRED_CODE) return refreshSession();
  if (path !== ME_PROBE_PATH) unauthorizedHandler?.();
  return false;
}

/** A fetch that failed before any response arrived: an outage, or an edge
 *  sign-in gate answering a cross-origin request with a redirect the browser
 *  refused. The one visit that tells them apart happens at most once per page
 *  load (see edgeGate.ts). */
function failedInTransit(error: unknown, request: Request): void {
  if (error instanceof TypeError) bounceThroughEdgeGate(request, edgeGateDeps(apiBaseUrl));
}

/** The request as it was sent, kept so a `token_expired` answer can be retried
 *  once after the refresh — the sent Request's body is already consumed. */
const retryable = new Map<string, Request>();

api.use({
  async onRequest({ request, id, schemaPath }) {
    // Every request names the org this tab rendered, once it knows one. The
    // server refuses it (409 `org_changed`) if the session is in another org.
    assertOrg(request.headers);
    const renewing = renewal(schemaPath);
    if (renewing) await renewing;
    if (!renewsSession(schemaPath)) retryable.set(id, request.clone());
    return request;
  },
  async onResponse({ response, schemaPath, id }) {
    const original = retryable.get(id);
    retryable.delete(id);
    const again = await afterAnswer(response, schemaPath, original !== undefined);
    return again && original ? fetch(original) : undefined;
  },
  onError({ id, error, request }) {
    retryable.delete(id);
    failedInTransit(error, request);
    return undefined;
  },
});

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

function urlOf(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
}

/** The call's `init` with the tab's org named on its headers, in the shape the
 *  caller spelled them (a record stays a record, `Headers` stay `Headers`), so
 *  whatever sits below sees the call it would have seen without the session.
 *  Unchanged when the tab knows no org yet or the caller named one itself. */
function namingOrg(input: RequestInfo | URL, init: RequestInit): RequestInit {
  const given = init.headers;
  if (given === undefined && input instanceof Request) {
    assertOrg(input.headers);
    return init;
  }
  const named = new Headers(given);
  if (named.has(ORG_HEADER)) return init;
  assertOrg(named);
  const org = named.get(ORG_HEADER);
  if (org === null) return init;
  if (given instanceof Headers) return { ...init, headers: named };
  if (Array.isArray(given)) return { ...init, headers: [...given, [ORG_HEADER, org]] };
  return { ...init, headers: { ...given, [ORG_HEADER]: org } };
}

/**
 * `fetch` with the session the typed client's requests get: the org assertion,
 * the renewal of a token about to lapse, one retry after a `token_expired`, the
 * mid-session sign-out on any other 401, the org-changed reload on a 409
 * `org_changed`, and the org a response echoes noted.
 *
 * For a request that cannot go through `api`. The call reaches `base` in the
 * shape it was made — `(input, init)`, the org header added where the caller
 * put its own headers — and the response comes back untouched, so a streamed
 * body still streams. The retry re-sends the same `init`, which holds for every
 * body this portal sends (a string, a Blob, FormData); a one-shot stream body
 * is not retried.
 */
export function withSession(base: FetchLike): typeof fetch {
  return async (input, init = {}) => {
    const url = new URL(urlOf(input), apiBaseUrl);
    const path = url.pathname;
    const sent = namingOrg(input, init);
    const renewing = renewal(path);
    if (renewing) await renewing;
    const oneShotBody = typeof ReadableStream !== "undefined" && sent.body instanceof ReadableStream;
    const retry = !renewsSession(path) && !oneShotBody;
    let response: Response;
    try {
      response = await base(input, sent);
    } catch (error) {
      failedInTransit(error, new Request(url, { method: sent.method ?? "GET" }));
      throw error;
    }
    if (await afterAnswer(response, path, retry)) return base(input, sent);
    return response;
  };
}

/** The portal's raw sender: {@link withSession} in front of the read gate. Every
 *  API request that does not go through the typed client leaves through this. */
export const apiFetch: typeof fetch = withSession(gatedFetch);

/** What a failed request says when the server wrote no sentence of its own. */
export const REQUEST_FAILED = "The request didn't go through.";

/**
 * The error a refused raw request is thrown as.
 *
 * The server's own sentence when it wrote one. When it wrote none, the message
 * is a sentence for the reader: never the path the request went to (it can
 * carry an id) and never the status, both of which are for a developer and are
 * still on the error's `status` and `code`.
 */
export async function failedResponse(response: Response): Promise<ApiError> {
  const body: unknown = await response.json().catch(() => null);
  return forTheReader(new ApiError(response.status, body, REQUEST_FAILED, response.headers));
}

/** Give a refusal the server explained no further a message for the reader in
 *  place of the client's fallback-and-status. A refusal still has a kind (the
 *  limiter, or the server falling over) and is said in those words when it is
 *  one of them. The server's own sentence is left as it is. */
export function forTheReader<E extends ApiError>(error: E): E {
  if (error.serverMessage === null) error.message = readerSentence(error);
  return error;
}

/** The sentence a refusal the server explained no further is described by: the
 *  limiter's or the failing server's when it is one of those, else the general
 *  one. Never the path, never the status. */
export function readerSentence(error: ApiError): string {
  const kind = error.status === 429 || error.status >= 500 ? failureSentence(error) : undefined;
  return kind ?? REQUEST_FAILED;
}

/**
 * Await an openapi-fetch call and return its `data`, or throw {@link ApiError}
 * on any non-2xx response. The single place a failed request becomes a thrown,
 * structured error, so every hook surfaces consistent messages and React
 * Query's `error` is always an `ApiError`. `fallback` is used only when the
 * response carries no parseable error body (network / non-JSON).
 */
export async function request<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
  fallback?: string,
): Promise<T> {
  const { data, error, response } = await call;
  if (!response.ok) throw new ApiError(response.status, error, fallback, response.headers);
  return data as T;
}
