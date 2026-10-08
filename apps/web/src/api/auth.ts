// React Query hooks for the portal's session. Mirrors apps/web's auth layer:
// the HTTP-only `alkera_session` cookie is the source of truth, so there is no
// token plumbing — every request rides the cookie (the SDK defaults
// `credentials: "include"`).
//
//   GET  /api/v1/auth/me      → the current user, or null when signed out (401)
//   POST /api/v1/auth/login   → sets the session cookie, returns the user
//   POST /api/v1/auth/logout  → clears the session cookie
//
// The guard (RequireAuth) reads `useCurrentUser`: a 401 is the normal signed-out
// signal mapped to `null`, NOT an error, so the guard redirects to /login
// instead of rendering an error surface.

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import type { components } from "@alkera/sdk";

import { enterOrg, forgetActiveOrg, goTo, leaveTo, noteOrg } from "./activeOrg";
import { chatListChanged } from "./cloudChat/transport";
import { ApiError, stepUpLoginUrl } from "./errors";
import { api, apiBaseUrl, noteSessionExpiry, request } from "./client";
import { keys } from "./keys";
import { retryAfterMs } from "./retry";
import { announceSession, type SessionMessage } from "./sessionChannel";
import { useLimits } from "@/lib/limits";
import { userScope, type AccountScope } from "@/lib/accountScope";

export type CurrentUser = components["schemas"]["MeRead"];
export type LoginPayload = components["schemas"]["LoginRequest"];
export type SignupPayload = components["schemas"]["SignupRequest"];
export type CompleteProfilePayload = components["schemas"]["CompleteProfileRequest"];
export type PasswordResetPayload = components["schemas"]["PasswordResetRequest"];
export type OAuthRegisterContext = components["schemas"]["OAuthRegisterContext"];
export type OAuthRegisterPayload = components["schemas"]["OAuthRegisterRequest"];
export type InvitationPreview = components["schemas"]["InvitationPublicRead"];
export type Membership = components["schemas"]["MembershipRead"];

/**
 * True while the signed-in account still owes its name — the immediate gate right
 * after a minimal (email + password) signup. The backend leaves the names empty as
 * the "profile incomplete" sentinel; the guard sends such accounts to
 * `/complete-profile` before any product surface. Mirrors apps/web's RequireAuth.
 */
export function isProfileIncomplete(user: CurrentUser | null | undefined): boolean {
  if (!user) return false;
  return !user.first_name.trim() || !user.last_name.trim();
}

/** The identity query key — an alias of `keys.auth.me` kept for its existing importers
 *  (the shell's unauthorized bridge and the email-verification pages). */
export const meKey = keys.auth.me;

/**
 * True once an account's email-verification grace window has lapsed and it's still
 * unverified — the point at which the app shows the full-screen gate and the API +
 * gateway start refusing requests. The backend is the enforcing authority (`deadline`
 * is server-computed); this only drives the UI, so it just compares the deadline to now.
 */
export function isVerificationBlocked(user: CurrentUser | null | undefined): boolean {
  if (!user?.email_verification_required || !user.email_verification_deadline) return false;
  const due = new Date(user.email_verification_deadline);
  return !Number.isNaN(due.getTime()) && Date.now() >= due.getTime();
}

/**
 * The current user, or `null` when signed out. A 401 is the expected
 * signed-out signal and resolves to `null` (the guard reads `null` as
 * "redirect to login"); any other failure throws.
 *
 * The distinction is the whole contract. A 401 is an ANSWER — the transport has
 * already spent its refresh on a `token_expired` before one reaches here — and
 * settles at once, with no retry to delay it. Everything else (a rate limit, a
 * restarting API, a dropped connection) is a failure to ASK, and is asked again
 * on a short ladder that honours a `Retry-After` when the server sent one. Only
 * when the ladder is spent does the failure reach the guard, which says so
 * rather than treating it as a sign-out.
 */
/** Who is signed in and in which org, the same object across renders while
 *  they do not change: what anything this tab keeps for the person is filed
 *  under. Null until the user is known. */
export function useUserScope(): AccountScope | null {
  const user = useCurrentUser().data;
  const id = user?.id;
  const org = user?.org_team_id;
  return useMemo(() => userScope(id ? { id, org_team_id: org } : null), [id, org]);
}

export function useCurrentUser() {
  const { sessionRetryAttempts, sessionRetryFloorMs, sessionRetryCapMs } = useLimits();
  return useQuery({
    queryKey: meKey,
    queryFn: async (): Promise<CurrentUser | null> => {
      const result = await api.GET("/api/v1/auth/me");
      if (result.response.status === 401) return null;
      if (!result.response.ok || !result.data) {
        throw new ApiError(
          result.response.status,
          result.error,
          "could not load your account",
          result.response.headers,
        );
      }
      noteSessionExpiry(result.data.session_expires_at);
      noteOrg(result.data.org_team_id, result.data.org_name);
      return result.data;
    },
    retry: (failureCount, error) =>
      failureCount < sessionRetryAttempts &&
      isTransient(error) &&
      // A wait longer than the shell is willing to sit through is not something to sit through
      // quietly: the reader is told what the server asked for and holds the Retry themselves.
      (retryAfterMs(error) ?? 0) <= sessionRetryCapMs,
    retryDelay: (failureCount, error) => {
      // The server's own wait wins when it named one, floored the way every other wait in the
      // portal is floored — a header of nothing, of zero, or of a moment this clock has already
      // passed is not permission to re-send the refused request at once.
      const asked = retryAfterMs(error);
      const wait =
        asked === null
          ? sessionRetryFloorMs * 2 ** failureCount
          : Math.max(asked, sessionRetryFloorMs);
      return Math.min(wait, sessionRetryCapMs);
    },
  });
}

/**
 * True when a failed read never got an answer out of the server: a rate limit, any 5xx, or a
 * transport failure (no `ApiError` at all — `fetch` rejected, so nothing was ever served). A 4xx
 * other than 429 IS an answer and asking again gets the same one.
 */
export function isTransient(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return error.status === 429 || error.status >= 500 || error.status === 0;
}

/**
 * Sign in with email + password. On success the session cookie is set; we seed
 * the cached user so the guard re-renders authed without a round-trip (the
 * shared invalidation policy refetches everything else for the new seat).
 */
export function useLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: LoginPayload) =>
      request(api.POST("/api/v1/auth/login", { body: payload }), "login failed"),
    onSuccess: (data) => {
      noteSessionExpiry(data.expires_at);
      enterOrg(data.user.org_team_id, data.user.org_name);
      qc.setQueryData(meKey, data.user);
    },
  });
}

/**
 * What a session leaves in the tab beyond the query cache (the live drafts,
 * files and notebooks held open, and the edits stashed for the next page), dropped whenever a
 * session ends, by sign-out or by expiry, so none of it reaches whoever signs
 * in next. Loaded on demand: the live lane stays out of the first page's bundle.
 */
export function forgetSessionState(): void {
  void import("./realtime/crdt/liveDraft").then((m) => m.forgetLiveDrafts()).catch(() => undefined);
  void import("./realtime/crdt/liveFile").then((m) => m.closeAllLiveFiles()).catch(() => undefined);
  void import("@/pages/workspace/chat/workspace/notebook/liveNotebook").then((m) => m.closeAllLiveNotebooks()).catch(() => undefined);
}

/** What the tab is in once its session ended: no org at all (signed out, or left its
 *  last org), the org it just entered, or whatever the session moved to elsewhere (the
 *  tab is about to reload and reads it fresh). */
export type TabSessionOrg = "none" | "moved" | { id: string; name?: string | null };

export interface EndTabSession {
  qc: QueryClient;
  org: TabSessionOrg;
  /** What the other tabs of this browser are told, when they did not already hear it. */
  announce?: SessionMessage;
  /** Where the tab goes next; it stays (and the route guards redirect) when omitted. */
  to?: string;
}

/**
 * Drop everything the tab holds for the session that just ended: the cached data, the live
 * drafts and files, any chat-list walk on the wire, and, for a tab left in no org, the org
 * it named and the renewal armed for it (a refresh would end the org-less sign-in the
 * landing acts on). Then tell the other tabs and leave for `to`. Every way a session ends
 * in this tab (sign-out, a refused credential, leaving an org, a switch, another tab's
 * announcement, the server saying the session moved) goes through here.
 */
export function endTabSession({ qc, org, announce, to }: EndTabSession): void {
  if (org === "none") {
    noteSessionExpiry(null);
    forgetActiveOrg();
  } else if (org !== "moved") {
    enterOrg(org.id, org.name);
  }
  qc.clear();
  if (org === "none") qc.setQueryData(meKey, null);
  forgetSessionState();
  // `qc.clear()` empties the cache but says nothing about a request already on the wire.
  chatListChanged();
  if (announce) announceSession(announce);
  if (to !== undefined) leaveTo(to);
}

/** A switch or join the org wants its own sign-in for first answers 409 with where that
 *  sign-in starts; the tab goes there. Any other failure stays with the caller. */
export function onStepUp(error: Error): void {
  const url = stepUpLoginUrl(error);
  if (error instanceof ApiError && error.status === 409 && url) goTo(url);
}

/**
 * Sign out. Clears the session cookie server-side; we drop the cached user to
 * `null` (so the guard redirects to /login) and clear the rest of the cache so
 * no signed-in data lingers for the next account. Opted out of the shared
 * invalidation policy: the cookie is already dead, so refetching would just be
 * a 401 storm that holds the logout pending — `clear()` is the whole story.
 */
export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => request(api.POST("/api/v1/auth/logout"), "could not log out"),
    meta: { invalidates: "none" },
    // The other tabs of this browser share the session that just ended.
    onSuccess: () => endTabSession({ qc, org: "none", announce: { type: "logged_out" } }),
  });
}

/**
 * Create an account. The backend signs the new user in (sets the session cookie
 * and returns the user), so we seed the cache the same way login does — the
 * caller then lands in the app.
 */
export function useSignup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: SignupPayload) =>
      request(api.POST("/api/v1/auth/signup", { body: payload }), "signup failed"),
    onSuccess: (data) => {
      noteSessionExpiry(data.expires_at);
      enterOrg(data.user.org_team_id, data.user.org_name);
      qc.setQueryData(meKey, data.user);
    },
  });
}

/**
 * Complete an OAuth-initiated registration (`/signup?oauth_ticket=…`). The signed
 * ticket carries the provider-verified email; the client supplies only the name and
 * org choice. The backend signs the new user in exactly like /signup, so the cache
 * is seeded the same way.
 */
export function useOAuthRegister() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: OAuthRegisterPayload) =>
      request(api.POST("/api/v1/auth/oauth/register", { body: payload }), "could not complete registration"),
    onSuccess: (data) => {
      qc.setQueryData(meKey, data.user);
    },
  });
}

/**
 * The no-auth invitation preview behind `/signup?invite=<token>` — enough to show
 * "Joining {team} in {org}" before the account exists, without leaking the inviter's
 * email. `retry: false` so an invalid/expired token settles immediately.
 */
export function useInvitationByToken(token: string | undefined) {
  return useQuery({
    queryKey: keys.invitations.byToken(token),
    enabled: Boolean(token),
    retry: false,
    queryFn: () =>
      request(
        api.GET("/api/v1/invitations/by-token/{token}", {
          params: { path: { token: token as string } },
        }),
        "this invitation is invalid or expired",
      ),
  });
}

/**
 * Finish a profile provisioned without one (after a minimal signup): set the name,
 * and for a new-org admin, name the organization. Seeds the cached user so the
 * profile gate lifts and the caller lands in the app.
 */
export function useCompleteProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: CompleteProfilePayload) =>
      request(api.POST("/api/v1/auth/complete-profile", { body: payload }), "could not save your profile"),
    onSuccess: (user) => {
      qc.setQueryData(meKey, user);
    },
  });
}

/** Request a password-reset link. Always 200 (the backend never leaks whether the email exists). */
export function useRequestPasswordReset() {
  return useMutation({
    mutationFn: (payload: PasswordResetPayload) =>
      request(
        api.POST("/api/v1/auth/password-reset/request", { body: payload }),
        "could not request a reset link",
      ),
    meta: { invalidates: "none" },
  });
}

/** Set a new password from a reset token. */
export function useResetPassword() {
  return useMutation({
    mutationFn: ({ token, password }: { token: string; password: string }) =>
      request(
        api.POST("/api/v1/auth/password-reset/{token}", {
          params: { path: { token } },
          body: { password },
        }),
        "could not reset your password",
      ),
    meta: { invalidates: "none" },
  });
}

// --- Email verification ----------------------------------------------------

/**
 * Verify the account's email from a token-link (`/verify-email/:token`). On success
 * the backend returns the now-verified user, which we seed into the cache so the gate
 * lifts without a round-trip. A replayed/used token answers 409; the page treats that
 * as success (the email IS verified), so it's not surfaced as an error here.
 */
export function useVerifyEmail() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (token: string) =>
      request(
        api.POST("/api/v1/auth/verify-email/{token}", { params: { path: { token } } }),
        "could not verify your email",
      ),
    onSuccess: (user) => {
      qc.setQueryData(meKey, user);
    },
  });
}

/** Re-send the verification email to the signed-in account. */
export function useResendVerification() {
  return useMutation({
    mutationFn: () =>
      request(api.POST("/api/v1/auth/verify-email/resend"), "could not send a verification email"),
    meta: { invalidates: "none" },
  });
}

// --- Org memberships ------------------------------------------------------

/**
 * The signed-in person's own active memberships, most recently used first, and the org
 * this browser is in. Never anyone else's. While multi-org is off it is the home org
 * alone, so every surface that offers a choice stays hidden for a single-org person.
 */
export function useMemberships(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: keys.auth.memberships,
    enabled: options?.enabled ?? true,
    queryFn: () => request(api.GET("/api/v1/auth/memberships"), "could not load your organizations"),
    staleTime: 60_000,
  });
}

/**
 * Sign-in with several orgs: whether every sign-in offers the chooser when the person
 * belongs to more than one org. Off (the default): a sign-in lands in the org used last,
 * and the chooser shows only when that org needs a step-up first (the server says
 * `choose_org`). On: every sign-in of a person in several orgs goes through the chooser.
 */
export const ALWAYS_CHOOSE_ORG = false;

/** Whether a fresh sign-in goes to the org chooser before the app. */
export function shouldChooseOrg(answer: {
  choose_org?: boolean;
  user: { membership_count?: number };
}): boolean {
  if (answer.choose_org) return true;
  return ALWAYS_CHOOSE_ORG && (answer.user.membership_count ?? 1) > 1;
}

/** The header the switch route demands, which a cross-site page cannot send. */
export const SWITCH_CSRF_HEADERS = { "X-Requested-With": "alkera" } as const;

export interface SwitchOrgVariables {
  orgTeamId: string;
  /** Where to land after the switch (a path in the app); the overview when omitted. */
  target?: string;
}

/**
 * Move this browser into another of the person's orgs. On success everything the tab
 * holds for the old org is dropped, the other tabs are told (they reload), and the tab
 * reloads at `target` so nothing rendered for the old org survives. A switch the org
 * wants a sign-in for first (its single sign-on) answers with where that sign-in starts,
 * and the tab goes there. Opted out of the shared invalidation policy: the page is
 * replaced, so a refetch would only race the navigation.
 */
export function useSwitchOrg() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ orgTeamId }: SwitchOrgVariables) =>
      request(
        api.POST("/api/v1/auth/refresh/org", {
          body: { org_team_id: orgTeamId },
          headers: SWITCH_CSRF_HEADERS,
        }),
        "could not switch organizations",
      ),
    meta: { invalidates: "none" },
    onSuccess: (data, { target }) =>
      endTabSession({
        qc,
        org: { id: data.user.org_team_id, name: data.user.org_name },
        announce: { type: "org_switched", org_team_id: data.user.org_team_id },
        to: target ?? "/",
      }),
    onError: onStepUp,
  });
}

// --- Device Authorization Grant (RFC 8628) consent ------------------------

/**
 * The consent-screen data for a pending device authorization, looked up by its
 * `user_code`. A definite 404/410 means the code is invalid or expired; any other
 * failure is transient and must not block an otherwise-valid approval, so the page
 * distinguishes them by `ApiError.status`. `retry: false` so an invalid code settles
 * at once instead of retrying the 404.
 */
export function useDeviceInfo(userCode: string) {
  return useQuery({
    queryKey: keys.auth.deviceInfo(userCode),
    enabled: userCode.length > 0,
    retry: false,
    queryFn: () =>
      request(
        api.GET("/api/v1/auth/device/info", { params: { query: { user_code: userCode } } }),
        "this device code is invalid or expired",
      ),
  });
}

/**
 * Approve a pending device authorization, binding it to the signed-in account and to one
 * of its orgs: `orgTeamId` when given (one of the caller's own memberships, checked by the
 * server), else the org this browser session is in.
 */
export function useApproveDevice() {
  return useMutation({
    mutationFn: ({ userCode, orgTeamId }: { userCode: string; orgTeamId?: string }) =>
      request(
        api.POST("/api/v1/auth/device/approve", {
          body: orgTeamId ? { user_code: userCode, org_team_id: orgTeamId } : { user_code: userCode },
        }),
        "could not approve this device",
      ),
    meta: { invalidates: "none" },
  });
}

/** Deny a pending device authorization. */
export function useDenyDevice() {
  return useMutation({
    mutationFn: (userCode: string) =>
      request(
        api.POST("/api/v1/auth/device/deny", { body: { user_code: userCode } }),
        "could not deny this device",
      ),
    meta: { invalidates: "none" },
  });
}

// --- External login (OAuth) ------------------------------------------------

/**
 * Build the absolute URL that starts a provider sign-in. This is a full-page
 * navigation (`window.location.href = …`), NOT a fetch — the browser must be
 * redirected to the provider and bounced back through the backend callback,
 * which sets the session cookie. The SPA isn't involved again until it returns.
 */
export function oauthStartUrl(
  provider: string,
  opts?: { intent?: "login" | "signup"; inviteToken?: string; returnTo?: string },
): string {
  const params = new URLSearchParams();
  if (opts?.intent) params.set("intent", opts.intent);
  if (opts?.inviteToken) params.set("invite_token", opts.inviteToken);
  if (opts?.returnTo) params.set("return_to", opts.returnTo);
  const qs = params.toString();
  return `${apiBaseUrl}/api/v1/auth/oauth/${provider}/start${qs ? `?${qs}` : ""}`;
}

/**
 * The external-login providers the platform has credentials for, used to decide
 * which sign-in buttons to render. Non-critical: a failure just hides the
 * buttons (returns `[]`), it never surfaces an error. Cached 5 minutes.
 */
export function useOAuthProviders() {
  return useQuery({
    queryKey: keys.oauth.providers,
    queryFn: async (): Promise<string[]> => {
      const result = await api.GET("/api/v1/auth/oauth/providers");
      if (!result.response.ok || !result.data) return [];
      return result.data.providers;
    },
    staleTime: 5 * 60_000,
  });
}

/**
 * Display-only prefill for the OAuth business-email page, derived from a signed
 * ticket. The backend trusts the ticket (not the client) for the email/provider;
 * these are hints for the copy. A definite failure means the ticket expired.
 */
export function useOAuthRegisterContext(ticket: string | undefined) {
  return useQuery({
    queryKey: keys.oauth.registerContext(ticket),
    enabled: Boolean(ticket),
    retry: false,
    queryFn: () =>
      request(
        api.GET("/api/v1/auth/oauth/register/context", {
          params: { query: { ticket: ticket as string } },
        }),
        "this sign-up link is invalid or expired",
      ),
  });
}
