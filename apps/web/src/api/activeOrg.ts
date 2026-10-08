// The org this tab is in, and what happens when it learns it is in another one.
//
// A person can belong to several orgs and switch between them. The browser holds
// one session, so a switch in one tab moves every tab of this browser to the new
// org. A tab still showing the old org must never act in the new one: it names
// the org it rendered on every request (`X-Alkera-Org`) and the server refuses a
// request whose credential is in another org with a 409 `org_changed`, before the
// route runs. That refusal, and any other sign that the session moved (a response
// echoing another org, a refresh answering for another org), lands here: the tab
// leaves a one-line notice for itself and reloads at the overview. Nothing is
// retried, so a write meant for the old org never reaches the new one.

import { safeSessionStorage } from "@alkera/ui/storage";

/** The header a request names its org in, and a response echoes its org in. */
export const ORG_HEADER = "X-Alkera-Org";
/** The 409 code the server answers a request asserting another org with. */
export const ORG_CHANGED_CODE = "org_changed";
/** Where the tab leaves itself the notice it shows after the reload. */
export const SESSION_NOTICE_KEY = "alkera.session.notice";

interface ActiveOrg {
  id: string;
  name: string | null;
}

let active: ActiveOrg | null = null;
let leaving = false;

/** The page a tab reloads at after its session moved. Injectable so a test can
 *  watch the navigation instead of losing its document to it. */
let navigate: (url: string) => void = (url) => window.location.assign(url);

/** Replace how the tab navigates away (tests). Returns the previous one. */
export function setOrgNavigator(next: (url: string) => void): (url: string) => void {
  const previous = navigate;
  navigate = next;
  return previous;
}

/** The org this tab rendered, or null before the first answer named one. */
export function activeOrgId(): string | null {
  return active?.id ?? null;
}

/**
 * Take what the server said the session's org is.
 *
 * The first answer is adopted. A later answer naming the same org refreshes the
 * name. A later answer naming ANOTHER org means the session moved under this tab
 * (a switch in another window): returns false after handling it as
 * {@link orgChanged}, so the caller stops what it was doing.
 */
export function noteOrg(id: string | null | undefined, name?: string | null): boolean {
  if (!id) return true;
  if (active === null || active.id === id) {
    active = { id, name: name ?? active?.name ?? null };
    return true;
  }
  orgChanged(name ?? null);
  return false;
}

/** The tab itself moved to `id` (a switch it made, a fresh sign-in): adopt it
 *  without treating it as a change from elsewhere. */
export function enterOrg(id: string | null | undefined, name?: string | null): void {
  if (!id) return;
  active = { id, name: name ?? null };
  leaving = false;
}

/** Forget the org (signed out). */
export function forgetActiveOrg(): void {
  active = null;
  leaving = false;
}

/** Name the org this tab rendered on a request's headers, once the tab knows
 *  one. A header the caller already set is left alone. */
export function assertOrg(headers: Headers): void {
  if (active && !headers.has(ORG_HEADER)) headers.set(ORG_HEADER, active.id);
}

/** Whether the server refused a request because the session is in another org
 *  than the one this tab rendered (409 `org_changed`). A refusal sends the tab
 *  through {@link orgChanged}; the request is never retried. */
export async function orgRefused(response: Response): Promise<boolean> {
  if (response?.status !== 409 || (await refusalCode(response)) !== ORG_CHANGED_CODE) return false;
  orgChanged();
  return true;
}

let changedHandler: (() => void) | null = null;

/** Register what else the app does when the session moved under this tab (the
 *  shell drops its cached data). Pass null to unregister. */
export function setOrgChangedHandler(handler: (() => void) | null): void {
  changedHandler = handler;
}

/** The notice the tab shows after its session moved under it. */
export function orgChangedNotice(name: string | null): string {
  return name ? `Switched to ${name} in another window.` : "Switched organizations in another window.";
}

/**
 * The session is in another org than the one this tab rendered: leave a notice,
 * let the app drop what it holds, and reload at the overview. Runs once per page,
 * however many requests come back refused.
 */
export function orgChanged(name: string | null = null): void {
  if (leaving) return;
  leaving = true;
  safeSessionStorage().set(SESSION_NOTICE_KEY, orgChangedNotice(name));
  changedHandler?.();
  navigate("/");
}

/** The notice left for this tab before a reload, read once. */
export function takeSessionNotice(): string | null {
  const store = safeSessionStorage();
  const notice = store.get(SESSION_NOTICE_KEY);
  if (notice !== null) store.remove(SESSION_NOTICE_KEY);
  return notice && notice.trim() ? notice : null;
}

/**
 * A fetch for a transport that does not go through the typed client (the event
 * stream): it names the tab's org on the request, and a 409 `org_changed` sends
 * the tab through {@link orgChanged} instead of being retried as a failure.
 */
export function withOrgAssertion(
  fetchImpl: (input: Request, init?: RequestInit) => Promise<Response>,
): (input: Request, init?: RequestInit) => Promise<Response> {
  return async (input, init) => {
    assertOrg(input.headers);
    const response = await fetchImpl(input, init);
    await orgRefused(response);
    return response;
  };
}

async function refusalCode(response: Response): Promise<string | null> {
  try {
    const body = (await response.clone().json()) as { error?: { code?: unknown } };
    return typeof body?.error?.code === "string" ? body.error.code : null;
  } catch {
    return null;
  }
}

/**
 * The tab itself is leaving for `url` after moving the session (its own switch). Anything
 * still answering for the old org from here on is the tab's own doing, not a switch from
 * elsewhere, so it leaves no "another window" notice and triggers no second reload.
 */
export function leaveTo(url: string): void {
  leaving = true;
  navigate(url);
}

/** Navigate the tab away through the injectable navigator. */
export function goTo(url: string): void {
  navigate(url);
}
