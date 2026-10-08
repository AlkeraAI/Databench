// A person's own orgs beyond the one this browser is in: joining another by an
// invitation link or a pending membership, founding one, leaving one, linking an
// org's single sign-on to the account, and the sign-in landing of a person who
// belongs to no org at all.
//
//   POST /api/v1/invitations/by-token/{token}/accept  → accept a link while signed in
//   POST /api/v1/orgs                                 → found an org (the session stays put)
//   POST /api/v1/orgs/current/leave                   → leave the org the session is in
//   POST /api/v1/auth/memberships/join                → accept a pending membership
//   POST /api/v1/auth/refresh/org/new | /join         → the no-organization landing
//   GET|POST /api/v1/auth/sso-link[/confirm|/cancel]  → link an org's single sign-on
//
// Every one of these exists only while the server runs with several orgs per
// person; with that off each answers 404. The public config says which
// (`multi_org_enabled`), and the portal draws none of these doors without it.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { forgetActiveOrg } from "./activeOrg";
import { SWITCH_CSRF_HEADERS, endTabSession, onStepUp, type Membership } from "./auth";
import { api, request } from "./client";
import { usePublicConfig } from "./config";
import { keys } from "./keys";

export type InvitationAccepted = components["schemas"]["InvitationAcceptResponse"];

/** Where a person with no org left goes: create one, or accept an invitation. */
export const NO_ORGANIZATION_PATH = "/no-organization";
/** The code a sign-in answers for a person who belongs to no org. */
export const NO_ACTIVE_MEMBERSHIP_CODE = "no_active_membership";

// --- Is the feature here? ---------------------------------------------------

/**
 * Whether the server runs with several orgs per person, as its public config says.
 * Fail-closed: false until a reachable backend answers true, so every multi-org door
 * stays hidden (and asks nothing) while the answer is missing.
 */
export function useMultiOrgEnabled(): boolean {
  return usePublicConfig().data?.multi_org_enabled === true;
}

/** True when a membership row waits for the person to join it. */
export function isPendingMembership(m: Pick<Membership, "status">): boolean {
  return m.status === "pending";
}

// --- Invitation links -------------------------------------------------------

/**
 * The invitation token in what a person pasted: a whole link (`…/signup?invite=<t>`,
 * `…/login?invite=<t>`) or the token alone. Null when there is nothing to send.
 */
export function inviteTokenFrom(input: string): string | null {
  const text = input.trim();
  if (!text) return null;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text) || text.startsWith("/")) {
    try {
      const token = new URL(text, "https://placeholder.invalid").searchParams.get("invite");
      return token?.trim() || null;
    } catch {
      return null;
    }
  }
  return text;
}

/** Accept an emailed invitation link as the signed-in account. The answer names the
 *  org joined, so the page can offer to switch into it. */
export function useAcceptInvitationByToken() {
  return useMutation({
    mutationFn: (token: string) =>
      request(
        api.POST("/api/v1/invitations/by-token/{token}/accept", { params: { path: { token } } }),
        "could not accept the invitation",
      ),
    meta: { invalidates: [keys.invitations.all, keys.auth.memberships] },
  });
}

// --- Pending memberships ----------------------------------------------------

/**
 * Join an org that provisioned this person and waits for them. On success the membership
 * is active and switchable. An org that wants its own sign-in first answers with where
 * it starts, and the tab goes there, as a switch does.
 */
export function useJoinMembership() {
  return useMutation({
    mutationFn: (orgTeamId: string) =>
      request(
        api.POST("/api/v1/auth/memberships/join", { body: { org_team_id: orgTeamId } }),
        "could not join the organization",
      ),
    meta: { invalidates: [keys.auth.memberships, keys.auth.me] },
    onError: onStepUp,
  });
}

// --- Found and leave --------------------------------------------------------

/** Found an org the caller owns. The session stays where it is; the caller switches. */
export function useCreateOrg() {
  return useMutation({
    mutationFn: (name: string) =>
      request(api.POST("/api/v1/orgs", { body: { name } }), "could not create the organization"),
    // The switch that follows reloads the page; a refetch would only race it.
    meta: { invalidates: "none" },
  });
}

/**
 * Leave the org this session is in. Every credential in it ends at once, so the tab holds
 * nothing for it afterwards: the caller switches into the org the answer names, or, when it
 * names none, the tab leaves for the no-organization landing.
 */
export function useLeaveOrg() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      request(api.POST("/api/v1/orgs/current/leave"), "could not leave the organization"),
    meta: { invalidates: "none" },
    onSuccess: (data) => {
      if (data.next_org_team_id) return;
      // The access token died with the membership; nothing may try to renew it, here or in
      // the other tabs (a refresh would end the org-less sign-in the landing acts on).
      endTabSession({ qc, org: "none", announce: { type: "org_left" }, to: NO_ORGANIZATION_PATH });
    },
  });
}

// --- The no-organization landing --------------------------------------------
//
// The landing acts on the sign-in alone (the refresh cookie): there is no access
// token, and nothing here may call the plain refresh route, which would end that
// sign-in. Each action enters the org it produces and the tab reloads into it.

function enterFromLanding(
  qc: ReturnType<typeof useQueryClient>,
  entered: { user: { org_team_id: string; org_name?: string } },
) {
  // No other tab is in an org to leave: they are on the landing too, or signed out.
  endTabSession({ qc, org: { id: entered.user.org_team_id, name: entered.user.org_name }, to: "/" });
}

/** Create an org from the no-organization landing and enter it. */
export function useLandingCreateOrg() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => {
      // The answer names the org being entered; it is not a switch from elsewhere.
      forgetActiveOrg();
      return request(
        api.POST("/api/v1/auth/refresh/org/new", { body: { name }, headers: SWITCH_CSRF_HEADERS }),
        "could not create the organization",
      );
    },
    meta: { invalidates: "none" },
    onSuccess: (data) => enterFromLanding(qc, data),
  });
}

/** Accept an invitation token from the no-organization landing and enter its org. */
export function useLandingJoinOrg() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (token: string) => {
      forgetActiveOrg();
      return request(
        api.POST("/api/v1/auth/refresh/org/join", { body: { token }, headers: SWITCH_CSRF_HEADERS }),
        "could not accept the invitation",
      );
    },
    meta: { invalidates: "none" },
    onSuccess: (data) => enterFromLanding(qc, data),
    onError: onStepUp,
  });
}

// --- Linking an org's single sign-on ----------------------------------------

/** The link request an org's single sign-on parked for this browser, for the signed-in
 *  account. `retry: false`: a 404 (nothing live) or 409 (another account) is an answer. */
export function useSsoLink() {
  return useQuery({
    queryKey: keys.auth.ssoLink,
    retry: false,
    // Read once: a confirmed or cancelled request is gone, and asking again would
    // repaint the page as expired under the answer it is showing.
    staleTime: Infinity,
    queryFn: () => request(api.GET("/api/v1/auth/sso-link"), "could not load the sign-on link"),
  });
}

/** Link the parked single sign-on identity to this account (and join, when the org asked). */
export function useConfirmSsoLink() {
  return useMutation({
    mutationFn: () => request(api.POST("/api/v1/auth/sso-link/confirm"), "could not link the sign-on"),
    meta: { invalidates: [keys.auth.memberships, keys.auth.identities, keys.auth.me] },
  });
}

/** Discard the parked link request. */
export function useCancelSsoLink() {
  return useMutation({
    mutationFn: () => request(api.POST("/api/v1/auth/sso-link/cancel"), "could not cancel"),
    meta: { invalidates: "none" },
  });
}
