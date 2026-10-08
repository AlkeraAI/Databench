import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "@alkera/ui/fonts";
import "@alkera/ui/styles";
import "../../styles/portal.css";

import { captureAndStripGithubCallback } from "./githubCallback";
import { RootErrorBoundary } from "./RootErrorBoundary";
import { installGlobalErrorHandlers } from "./globalHandlers";
import { App } from "../../App";
import { installExtensions, type WebExtension } from "@alkera/ui/extensions";
import { PORTAL_TELEMETRY } from "../extensions/portal";

// FIRST executable statement, before ANY telemetry or the router reads
// window.location: lift the GitHub install callback's secrets
// (code/state/installation_id) off the URL. Analytics sends page_location on boot
// and the auth guard folds location.search into a return_to — either would leak
// the callback. Held in memory for the integration page; the URL is left clean.
//
// CAVEAT — this runs AFTER the static imports above. ES modules fully evaluate
// each import (its top-level code) before this file's own statements, so this
// call cannot protect a module that reads window.location.href/.search AT MODULE
// SCOPE. The imported boot modules (analytics, sentry, error handlers) read only
// location.hostname, and only inside their init functions (called below, after
// the strip) — a guard test (githubCallback.test.ts) pins that none of them
// reads the code-bearing href/search. Any new boot module must keep that
// property, or move URL reads into a function invoked after this line.
captureAndStripGithubCallback();

// The emailed entry links carry a LIVE single-use credential in the URL itself:
// the password-reset and email-verification tokens as a path segment
// (/reset-password/<token>, /verify-email/<token>), the invitation token and the
// OAuth register ticket as a query param (/signup?invite=…, ?oauth_ticket=…),
// the device grant's user code (/device?user_code=…). Unlike the GitHub callback
// above they CANNOT be lifted off the URL here — the router resolves the route,
// and the pages read the token, from window.location, so stripping it would
// break the flow.
//
// What we can do is keep the hosted telemetry away from them. GA4's `config`
// reports page_location = the full href, and Sentry attaches request.url (plus
// navigation breadcrumbs) to every event — either copies a live credential to a
// third party, where anyone with read access to the property can spend it before
// it expires. So telemetry does NOT start while such a URL is in the address
// bar; it starts on the first navigation to a credential-free one, which is
// where the session actually goes once the token is spent. FAIL CLOSED: a URL we
// can't parse counts as credential-bearing.
//
// SCOPE — this gates the SINKS, it does not strip the token, so it is not the
// whole fix. The credential still rides in the URL (browser history, the Referer
// on any cross-origin subresource) and the client-error reporter still POSTs
// `window.location.href` to our own /api/v1/errors/events. And the gate is a
// one-shot BOOT decision: once a sink is armed, a credential-bearing URL reached
// LATER — the device page appending ?user_code=, a guard folding a return_to —
// is no longer held back, because GA4 keeps reporting page_location on every
// history change. Closing those needs the token lifted off the URL at boot (the
// captureAndStripGithubCallback pattern, with the auth pages reading a
// module-local holder) plus a shared href sanitizer applied inside each sink.
//
// The converse is deliberate too: a session that never navigates off the
// credential URL (approve a device code, close the tab) never arms the
// third-party sinks at all. Errors there are not lost — installGlobalErrorHandlers
// runs unconditionally and reportClientError POSTs them to our OWN
// /api/v1/errors/events — they just never reach Sentry, which is the point.
const CREDENTIAL_QUERY_PARAMS = ["invite", "oauth_ticket", "user_code"];
const CREDENTIAL_PATH_PREFIXES = ["/reset-password/", "/verify-email/"];
// Params whose VALUE is itself a URL: the auth guard folds the current location
// into `/login?return_to=<encoded URL>`, so a credential rides one level down
// and still sits in the href a telemetry sink reads.
const NESTED_URL_PARAMS = ["return_to", "redirect_uri"];
const NESTED_URL_DEPTH = 2;

function urlCarriesCredential(): boolean {
  try {
    return locationCarriesCredential(new URL(window.location.href), NESTED_URL_DEPTH);
  } catch {
    return true;
  }
}

function locationCarriesCredential(url: URL, depth: number): boolean {
  // A bare /reset-password (no token segment) is the expired-link page, not a
  // credential — the prefix must be FOLLOWED by something.
  if (
    CREDENTIAL_PATH_PREFIXES.some(
      (prefix) => url.pathname.startsWith(prefix) && url.pathname.length > prefix.length,
    )
  ) {
    return true;
  }
  // A credential can ride in the query or the hash fragment. A fragment never
  // reaches the server but is fully readable by in-page JS, so telemetry would
  // copy it just the same. Only the part after the first `?` is a param list
  // (a router-style `#/route?invite=…` parses whole otherwise).
  const fragmentRaw = url.hash.startsWith("#") ? url.hash.slice(1) : "";
  const fragment = new URLSearchParams(fragmentRaw.slice(fragmentRaw.indexOf("?") + 1));
  for (const params of [url.searchParams, fragment]) {
    for (const [key, value] of params) {
      if (CREDENTIAL_QUERY_PARAMS.includes(key)) return true;
      if (depth <= 0 || !NESTED_URL_PARAMS.includes(key)) continue;
      try {
        if (locationCarriesCredential(new URL(value, url.origin), depth - 1)) return true;
      } catch {
        return true;
      }
    }
  }
  return false;
}

// Observability: the telemetry sinks the product registers (none in the open
// build), the global handlers for uncaught errors and unhandled rejections, and
// the root error boundary. We do NOT funnel every React Query failure to the
// reporter: expected API 4xx are surfaced by each page, and backend 5xx are
// already captured server-side (with a trace_id). Only likely client-side bugs
// are reported: uncaught exceptions, unhandled rejections and render crashes.
// Fire-and-forget: each sink's start is async and confirms against the backend's
// public config that telemetry is allowed, staying off on a self-hosted
// deployment.
function startTelemetry(): void {
  for (const sink of PORTAL_TELEMETRY.items()) void sink.start();
}

/** Uninstall our history wrapper, leaving `window.history` exactly as we found
 *  it — but ONLY while that wrapper is still the function sitting there.
 *
 *  Both halves matter. Patching history is a crowded pattern (Sentry's
 *  navigation breadcrumbs, GA4 enhanced measurement, router/analytics shims all
 *  do it), and anything installed after us wrapped OUR wrapper: blindly
 *  assigning the captured original back would silently uninstall theirs, so we
 *  leave the chain alone and the caller keeps our wrapper as an inert
 *  pass-through instead. And when nothing wrapped us, restore the way we found
 *  it — these methods normally live on History.prototype, so assigning the
 *  original back would leave an own property on `window.history` shadowing any
 *  LATER prototype patch. Deleting exposes the prototype again. */
function uninstallHistoryWrapper<K extends "pushState" | "replaceState">(
  key: K,
  wrapper: History[K],
  original: History[K],
  wasOwnProperty: boolean,
): void {
  if (window.history[key] !== wrapper) return;
  if (wasOwnProperty) window.history[key] = original;
  else delete (window.history as Partial<History>)[key];
}

/** Start telemetry now, or — when the entry URL carries a credential — on the
 *  first navigation that no longer does. React Router navigates through
 *  history.pushState / replaceState, so wrapping those (plus back/forward and
 *  hash changes) sees every in-app move; the wrappers are removed once
 *  telemetry has started (or neutered in place when removing them would clobber
 *  someone else's patch — see uninstallHistoryWrapper). */
function startTelemetryOnCredentialFreeUrl(): void {
  if (!urlCarriesCredential()) {
    startTelemetry();
    return;
  }
  const { pushState, replaceState } = window.history;
  const hasOwn = Object.prototype.hasOwnProperty;
  const ownPushState = hasOwn.call(window.history, "pushState");
  const ownReplaceState = hasOwn.call(window.history, "replaceState");
  // Latched, because a wrapper we could not remove stays in the chain and keeps
  // calling settle() on every later navigation — telemetry must still start
  // exactly once.
  let settled = false;
  const settle = (): void => {
    if (settled || urlCarriesCredential()) return;
    settled = true;
    uninstallHistoryWrapper("pushState", patchedPushState, pushState, ownPushState);
    uninstallHistoryWrapper("replaceState", patchedReplaceState, replaceState, ownReplaceState);
    window.removeEventListener("popstate", settle);
    window.removeEventListener("hashchange", settle);
    startTelemetry();
  };
  function patchedPushState(...args: Parameters<History["pushState"]>): void {
    pushState.apply(window.history, args);
    settle();
  }
  function patchedReplaceState(...args: Parameters<History["replaceState"]>): void {
    replaceState.apply(window.history, args);
    settle();
  }
  window.history.pushState = patchedPushState;
  window.history.replaceState = patchedReplaceState;
  window.addEventListener("popstate", settle);
  window.addEventListener("hashchange", settle);
}

/** Boot the portal: the extensions this build ships, their telemetry (held back while a
 *  credential is in the URL), the global error handlers, then the app. The GitHub callback strip above ran
 *  when this module loaded, so an entry point imports this module before anything else. */
export function startPortal(extensions: readonly WebExtension[]): void {
  installExtensions(extensions);
  startTelemetryOnCredentialFreeUrl();
  installGlobalErrorHandlers();

  const root = document.getElementById("root");
  if (!root) throw new Error("#root element not found");

  createRoot(root).render(
    <StrictMode>
      <RootErrorBoundary>
        <App />
      </RootErrorBoundary>
    </StrictMode>,
  );
}
