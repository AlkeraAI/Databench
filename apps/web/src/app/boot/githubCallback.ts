// The GitHub App install callback carries secrets in its query string — the OAuth
// `code`, the signed `state`, the `installation_id`. GitHub delivers them to this
// SPA's fixed callback (/org/integration), but "same-origin" only
// holds if they STAY here. Left in the URL they leak two ways: a telemetry sink a
// product registers (analytics sends `page_location` = the full href on boot) copies
// them to a third party, and the auth guard folds `location.search` into `/login?return_to=`,
// preserving them in a URL that gets logged and reloaded. A leaked callback is
// enough to complete the crafted-install-link attack (the mailed link carries the
// attacker's state + PKCE challenge, so the victim's code is bound to a verifier
// the attacker already holds).
//
// So the params are lifted off the URL SYNCHRONOUSLY at boot, before analytics or
// the router reads `window.location`, and held in memory for the integration page
// to consume. Nothing persistent, nothing third-party, nothing in a stored URL.
//
// The trigger is the PRESENCE of a callback param, on ANY path -- NOT a fixed
// callback path. GitHub's post-install redirect can land on the canonical
// `/org/integration` OR a legacy alias (`/dashboard/gate/github`,
// `/org/gate/integration`, `/org/gate/github`) that React-Router redirects to it;
// gating on one path skipped the aliases, leaking the secrets AND (since capture
// runs once at boot, before the redirect) leaving `takeGithubCallback()` empty so
// the preview never started. Keying on the params instead closes every alias and
// any future callback route. It is safe on unrelated paths because no other SPA
// route reads `code`/`state`/`installation_id`/`setup_action`/`error` from the URL.

export interface GithubInstallCallback {
  installation_id: string | null;
  state: string | null;
  code: string | null;
  error: string | null;
}

// Everything GitHub appends to the callback. `code`/`state`/`installation_id` are
// the sensitive trio; `setup_action` and GitHub's full OAuth error triple
// (`error`/`error_description`/`error_uri`) ride along and are stripped too, so
// nothing GitHub-flow-specific is left to leak or confuse a return_to.
const CALLBACK_PARAMS = [
  "code",
  "state",
  "installation_id",
  "setup_action",
  "error",
  "error_description",
  "error_uri",
] as const;

let captured: GithubInstallCallback | null = null;

/** Reassemble the hash after the strip: a bare param fragment keeps its shape,
 *  a router-style fragment keeps its route/anchor prefix, and nothing surviving
 *  means no hash at all. */
function rebuildHash(prefix: string, hadPrefix: boolean, remaining: string): string {
  if (!hadPrefix) return remaining ? `#${remaining}` : "";
  return `#${prefix}${remaining ? `?${remaining}` : ""}`;
}

/** Lift the GitHub callback params off `window.location` (any path) and rewrite
 *  the URL to drop them. Idempotent and cheap on every other page load (returns
 *  immediately when no callback param is present in the query or fragment). MUST
 *  run before analytics init and before the router reads the location. */
export function captureAndStripGithubCallback(): void {
  let url: URL;
  try {
    url = new URL(window.location.href);
  } catch {
    return;
  }
  const query = url.searchParams;
  // The callback params can ride in the QUERY or the HASH FRAGMENT -- a fragment
  // (#code=...) is parsed by the browser but never sent to the server, yet it is
  // fully readable by in-page JS, so telemetry / a return_to would still leak it.
  // Parse both; rewrite the fragment only when it actually carried a callback
  // param, so a normal anchor (#section) is left byte-for-byte alone.
  const fragmentRaw = url.hash.startsWith("#") ? url.hash.slice(1) : "";
  // A router-style fragment (#/org/integration?code=...) is not itself a param
  // list -- parsed whole, the key becomes `/org/integration?code` and the code is
  // missed. Only the part after the first `?` is params; the prefix (the route or
  // anchor) must survive the strip. A bare `#code=...` has no `?` and parses whole.
  const splitAt = fragmentRaw.indexOf("?");
  const fragmentPrefix = splitAt === -1 ? "" : fragmentRaw.slice(0, splitAt);
  const fragment = new URLSearchParams(
    splitAt === -1 ? fragmentRaw : fragmentRaw.slice(splitAt + 1),
  );
  const queryHas = CALLBACK_PARAMS.some((key) => query.has(key));
  const fragmentHas = CALLBACK_PARAMS.some((key) => fragment.has(key));
  // Strip whenever ANY callback param is present, not just the code -- a bare
  // `?state=` / `#state=` / `?setup_action=` would otherwise stay in the URL and
  // reach analytics / a return_to. A plain visit to the page is a no-op.
  if (!queryHas && !fragmentHas) return;

  const pick = (key: string): string | null => query.get(key) ?? fragment.get(key);
  captured = {
    installation_id: pick("installation_id"),
    state: pick("state"),
    code: pick("code"),
    error: pick("error"),
  };
  for (const key of CALLBACK_PARAMS) {
    query.delete(key);
    fragment.delete(key);
  }
  const newQuery = query.toString();
  const remaining = fragment.toString();
  const newHash = fragmentHas ? rebuildHash(fragmentPrefix, splitAt !== -1, remaining) : url.hash;
  window.history.replaceState(
    window.history.state,
    "",
    url.pathname + (newQuery ? `?${newQuery}` : "") + newHash,
  );
}

/** The captured callback, once. Cleared on read so a re-render (or a second
 *  surface) never re-fires the flow. Returns null when there was no callback. */
export function takeGithubCallback(): GithubInstallCallback | null {
  const value = captured;
  captured = null;
  return value;
}
