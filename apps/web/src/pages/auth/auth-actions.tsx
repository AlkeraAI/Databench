import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { apiBaseUrl } from "../../api/client";
import { ApiError, GENERIC_FAILURE, refusalSentence } from "../../api/errors";
import { NO_ACTIVE_MEMBERSHIP_CODE, NO_ORGANIZATION_PATH } from "../../api/orgs";
import {
  shouldChooseOrg,
  useCompleteProfile,
  useLogin,
  useOAuthRegister,
  useRequestPasswordReset,
  useResetPassword,
  useSignup,
} from "../../api/auth";
import { CARRIED_AUTH_PARAMS } from "../../app/extensions/portal";

/** The API routes that answer a browser with a page of their own, and so are
 *  valid places to return to after signing in: a person reaches them by clicking
 *  a link from outside the portal (the account link the Slack bot sends, Slack's
 *  redirect back after "Add to Slack"), and when that browser is signed out the
 *  server sends it to `/login?return_to=<that URL>`. Matched exactly on the
 *  decoded path; everything else under `/api` stays refused. Mirrors
 *  `backend/auth/browser_entry.py` — a route added there is added here. */
export const SERVER_PAGES: readonly string[] = ["/api/v1/slack/link", "/api/v1/slack/oauth/callback"];

function decodedPath(path: string): string | null {
  const withoutQuery = path.split(/[?#]/)[0] ?? "";
  try {
    return decodeURIComponent(withoutQuery);
  } catch {
    return null;
  }
}

/** True when `path` is one of the API's own pages ({@link SERVER_PAGES}): the
 *  router cannot render it, so reaching it takes a full page load. */
export function isServerPage(path: string): boolean {
  const decoded = decodedPath(path);
  return decoded !== null && SERVER_PAGES.includes(decoded);
}

/** A same-origin path that is nevertheless not a page.
 *
 *  `/api/…` serves JSON and file bytes, so a `return_to` naming it would hand a
 *  browser that has just signed in to a route nobody meant to navigate to — a
 *  download, or a refusal where the app should be. The match is made on the
 *  DECODED path because the server decodes percent-escapes before it routes, so
 *  `/%61pi/v1/…` reaches the same place `/api/v1/…` does; a path that cannot be
 *  decoded is refused rather than guessed at. */
function notAPage(path: string): boolean {
  const decoded = decodedPath(path);
  if (decoded === null) return true;
  if (SERVER_PAGES.includes(decoded)) return false;
  const lower = decoded.toLowerCase();
  return lower === "/api" || lower.startsWith("/api/");
}

/** Same-origin relative path only (the open-redirect guard on every `?return_to=`),
 *  else null so the caller falls through to its own default. Rejects an absolute URL
 *  (no leading `/`), protocol-relative `//…`, any backslash — browsers normalize
 *  `\` to `/`, so `/\evil.com` would become protocol-relative — and the API, which is
 *  same-origin but is not a page — except the few API routes that are pages
 *  ({@link SERVER_PAGES}). Mirrors the backend's `_safe_return_to`. */
export function safeReturnTo(path: string | null): string | null {
  return path &&
    path.startsWith("/") &&
    !path.startsWith("//") &&
    !path.includes("\\") &&
    !notAPage(path)
    ? path
    : null;
}

/** The invitation landing for `token`: for a signed-in person it offers to accept. */
export function invitationPath(token: string): string {
  return `/signup?invite=${encodeURIComponent(token)}`;
}

/** Where a sign-in that found no org to enter goes, keeping the invitation it came with. */
export function noOrganizationPath(invite: string | null): string {
  return invite ? `${NO_ORGANIZATION_PATH}?invite=${encodeURIComponent(invite)}` : NO_ORGANIZATION_PATH;
}

/** Where to land after login / signup / complete-profile, read from the auth page's
 *  own query string: a sanitized `?return_to=` deep link (set by the route guards and
 *  the OAuth callback) wins, then an invitation carried through sign-in (`?invite=`,
 *  back to its landing to accept it), then a parameter an extension carries
 *  (CARRIED_AUTH_PARAMS), then home. */
export function postAuthDestination(search: string): string {
  const params = new URLSearchParams(search);
  const returnTo = safeReturnTo(params.get("return_to"));
  if (returnTo) return returnTo;
  const invite = params.get("invite");
  if (invite) return invitationPath(invite);
  for (const carried of CARRIED_AUTH_PARAMS.items()) {
    const value = params.get(carried.key);
    if (value) return carried.landing(value);
  }
  return "/";
}

/** `path` with the extension-carried parameters the auth page's query holds, so the
 *  sign-in and sign-up cross-links keep them. */
export function withCarriedParams(path: string, params: URLSearchParams): string {
  const carried = new URLSearchParams();
  for (const { key } of CARRIED_AUTH_PARAMS.items()) {
    const value = params.get(key);
    if (value) carried.set(key, value);
  }
  const query = carried.toString();
  return query ? `${path}?${query}` : path;
}

/** The org chooser, carrying where to land once an org is picked. */
export function chooserPath(dest: string): string {
  return dest === "/" ? "/choose-org" : `/choose-org?return_to=${encodeURIComponent(dest)}`;
}

/** Where one of the API's own pages ({@link SERVER_PAGES}) is loaded from: the
 *  API's origin, not the portal's. The hosted portal is static files on its own
 *  origin and serves no `/api`, so `/api/v1/slack/link?code=…` on the portal host
 *  is the portal's "page doesn't exist"; same-origin deployments are unchanged
 *  because the API base is then the portal's own origin. */
export function serverPageUrl(path: string, base: string = apiBaseUrl): string {
  return `${base.replace(/\/+$/, "")}${path}`;
}

/** Go to a post-auth destination, replacing the auth page in history. A portal
 *  route is a router navigation; one of the API's own pages is a full page load,
 *  since the router has no route for it and would render "not found". */
export function useGoTo(): (to: string) => void {
  const navigate = useNavigate();
  return useCallback(
    (to: string) => {
      if (isServerPage(to)) window.location.replace(serverPageUrl(to));
      else void navigate(to, { replace: true });
    },
    [navigate],
  );
}

/** `<Navigate replace>` for a post-auth destination, which may be one of the
 *  API's own pages (see {@link useGoTo}). */
export function GoTo({ to }: { to: string }) {
  const server = isServerPage(to);
  useEffect(() => {
    if (server) window.location.replace(serverPageUrl(to));
  }, [server, to]);
  return server ? null : <Navigate to={to} replace />;
}

// The auth surface talks to the backend through this seam, not directly. The app
// provides the REAL implementation (RealAuthActionsProvider) — it runs the React
// Query mutations and navigates into the product on success; the pages don't
// change. The seam stays injectable: the design lab provides `simulatedAuthActions`
// to force the loading / error states with no backend.

export interface LoginInput {
  email: string;
  password: string;
  /** A 6-digit TOTP or backup code, sent on the second attempt once the first
   *  401s with `mfa_required` (an MFA-protected account). Absent otherwise. */
  mfaCode?: string;
  /** Cloudflare Turnstile token, present only when a site key is configured. */
  turnstileToken?: string;
}

export interface SignupInput {
  email: string;
  password: string;
  /** Present when joining via an invite link; absent for a new-org signup. */
  inviteToken?: string;
  /** True only when the user explicitly chose to keep a personal email — via
   *  `?allow_personal=1` or the "continue anyway" affordance after the backend's
   *  business-email gate refused the address. Defaults to false (gated). */
  allowPersonalEmail?: boolean;
  /** Cloudflare Turnstile token, present only when a site key is configured. */
  turnstileToken?: string;
}

export interface OAuthRegisterInput {
  /** The signed ticket minted by the backend OAuth callback (`?oauth_ticket=`).
   *  Carries the provider-verified email/subject — the client never sends those. */
  ticket: string;
  firstName: string;
  lastName: string;
  /** Names the new org; omitted when the ticket carries an invite (the invite wins). */
  orgName?: string;
  /** Same explicit personal-email opt-in as {@link SignupInput.allowPersonalEmail}. */
  allowPersonalEmail?: boolean;
}

export interface CompleteProfileInput {
  firstName: string;
  lastName: string;
  /** A new-org admin names their org here; omitted for an invited member. */
  orgName?: string;
}

export interface RequestResetInput {
  email: string;
}

export interface ResetPasswordInput {
  token: string;
  password: string;
}

export interface AuthActions {
  login(input: LoginInput): Promise<void>;
  signup(input: SignupInput): Promise<void>;
  oauthRegister(input: OAuthRegisterInput): Promise<void>;
  completeProfile(input: CompleteProfileInput): Promise<void>;
  requestPasswordReset(input: RequestResetInput): Promise<void>;
  resetPassword(input: ResetPasswordInput): Promise<void>;
}

/** A failure with a message safe to show the user. Carries the backend error
 *  `code` (e.g. `mfa_required`, `mfa_invalid`) so a page can branch on it — the
 *  MFA challenge reveals its code field when login fails with an MFA code. */
export class AuthError extends Error {
  readonly code: string | null;
  /** The envelope's `details`, e.g. the `next` an `account_exists` refusal names. */
  readonly details: Readonly<Record<string, unknown>> | null;
  constructor(
    message: string,
    code: string | null = null,
    details: Readonly<Record<string, unknown>> | null = null,
  ) {
    super(message);
    this.name = "AuthError";
    this.code = code;
    this.details = details;
  }
}

const DELAY_MS = 900;
const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

// Stand-in until the real network layer exists. A couple of sentinel inputs resolve
// to errors so the failure states are reachable with no backend:
//   • sign in with the password "wrong"        → invalid credentials
//   • sign up with an email containing "taken"  → email already registered
export const simulatedAuthActions: AuthActions = {
  async login({ password }) {
    await wait(DELAY_MS);
    if (password === "wrong") {
      throw new AuthError("That email and password don't match. Try again.");
    }
  },
  async signup({ email }) {
    await wait(DELAY_MS);
    if (email.includes("taken")) {
      throw new AuthError("An account with that email already exists. Sign in instead.");
    }
  },
  async oauthRegister() {
    await wait(DELAY_MS);
  },
  async completeProfile() {
    await wait(DELAY_MS);
  },
  async requestPasswordReset() {
    await wait(DELAY_MS);
  },
  async resetPassword() {
    await wait(DELAY_MS);
  },
};

const AuthActionsContext = createContext<AuthActions>(simulatedAuthActions);

export function AuthActionsProvider({ actions, children }: { actions?: AuthActions; children: ReactNode }) {
  return (
    <AuthActionsContext.Provider value={actions ?? simulatedAuthActions}>{children}</AuthActionsContext.Provider>
  );
}

export function useAuthActions(): AuthActions {
  return useContext(AuthActionsContext);
}

/** Surface a failed request as a user-safe AuthError so the auth pages render the
 *  backend's sentence inline. Anything the server did not explain (a network drop,
 *  a 5xx) becomes the generic sentence, never the client's own diagnostic. */
function toAuthError(err: unknown): never {
  if (err instanceof ApiError) throw new AuthError(refusalSentence(err), err.code, err.details);
  throw new AuthError(GENERIC_FAILURE);
}

/** The real auth actions, backed by the React Query mutations + the router. Login and
 *  signup sign the session in (the cookie is set server-side, the cached user is seeded)
 *  and then navigate into the product; the guard re-renders authed. Must be mounted INSIDE
 *  the router (it reads the auth page's `?return_to=` / `?plan=`) and a QueryClientProvider. */
export function RealAuthActionsProvider({ children }: { children: ReactNode }) {
  const login = useLogin();
  const signup = useSignup();
  const oauthRegister = useOAuthRegister();
  const completeProfile = useCompleteProfile();
  const requestReset = useRequestPasswordReset();
  const resetPassword = useResetPassword();
  const goTo = useGoTo();
  const location = useLocation();
  // Where to land after the action: the sanitized deep link the guard (or the OAuth
  // callback) put in the query string, else an extension's carried landing, else home.
  const dest = postAuthDestination(location.search);
  const invite = new URLSearchParams(location.search).get("invite");

  const actions = useMemo<AuthActions>(
    () => ({
      async login({ email, password, mfaCode, turnstileToken }) {
        let answer: Awaited<ReturnType<typeof login.mutateAsync>>;
        try {
          answer = await login.mutateAsync({
            email,
            password,
            // null (not undefined) so the SDK serializes the field when empty —
            // the backend reads its absence the same either way.
            mfa_code: mfaCode ?? null,
            turnstile_token: turnstileToken ?? null,
          });
        } catch (err) {
          // Signed in, but into no org: every org they belonged to is behind them. The
          // landing offers to create one or accept an invitation.
          if (err instanceof ApiError && err.code === NO_ACTIVE_MEMBERSHIP_CODE) {
            goTo(noOrganizationPath(invite));
            return;
          }
          toAuthError(err);
        }
        // A person in several orgs whose last-used org wants a step-up first picks
        // where to go; the destination rides along to the chooser.
        goTo(shouldChooseOrg(answer) ? chooserPath(dest) : dest);
      },
      async signup({ email, password, inviteToken, allowPersonalEmail, turnstileToken }) {
        // Minimal signup: email + password only. The new account has no name yet, so
        // wherever it lands the profile gate bounces it to /complete-profile — carrying
        // the destination as `?return_to=`, so a carried landing survives the detour.
        try {
          await signup.mutateAsync({
            email,
            password,
            // Empty names are the "profile incomplete" sentinel — collected on the
            // complete-profile step. (The SDK types these as required since they
            // carry a server default, so they're sent explicitly.)
            first_name: "",
            last_name: "",
            invite_token: inviteToken ?? null,
            allow_personal_email: allowPersonalEmail ?? false,
            turnstile_token: turnstileToken ?? null,
          });
        } catch (err) {
          toAuthError(err);
        }
        goTo(dest);
      },
      async oauthRegister({ ticket, firstName, lastName, orgName, allowPersonalEmail }) {
        // The OAuth register collects the name up front (the backend requires it),
        // so the new account is complete — no profile-gate hop on the way to `dest`.
        try {
          await oauthRegister.mutateAsync({
            oauth_ticket: ticket,
            first_name: firstName,
            last_name: lastName,
            // null when the ticket carries an invite (the invite wins server-side).
            org_name: orgName ?? null,
            allow_personal_email: allowPersonalEmail ?? false,
          });
        } catch (err) {
          toAuthError(err);
        }
        goTo(dest);
      },
      async completeProfile({ firstName, lastName, orgName }) {
        try {
          await completeProfile.mutateAsync({
            first_name: firstName,
            last_name: lastName,
            // null for an invited member (no org to name); the backend ignores it.
            org_name: orgName ?? null,
          });
        } catch (err) {
          toAuthError(err);
        }
        goTo(dest);
      },
      async requestPasswordReset({ email }) {
        try {
          await requestReset.mutateAsync({ email });
        } catch (err) {
          toAuthError(err);
        }
      },
      async resetPassword({ token, password }) {
        try {
          await resetPassword.mutateAsync({ token, password });
        } catch (err) {
          toAuthError(err);
        }
      },
    }),
    [login, signup, oauthRegister, completeProfile, requestReset, resetPassword, goTo, dest, invite],
  );

  return <AuthActionsContext.Provider value={actions}>{children}</AuthActionsContext.Provider>;
}
