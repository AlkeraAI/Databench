// The platform's ban registers — accounts and email domains that may no longer
// reach the product. Every shape is the SDK's generated type, so a page never
// hand-types a response.
//
// A ban or a lift changes two things a reader can see: the ban register itself,
// and the `banned` flag the users register carries. Both mutations declare that
// pair in `meta.invalidates` so the users list re-greys (or un-greys) a row
// without any hand-wired `invalidateQueries` — the shared MutationCache policy
// (api/queryClient.ts) does the refetch and the page awaits it.
//
// The routes are platform-ADMIN gated server-side: support staff get a 403 with
// the message "Platform admin role required", which arrives here as an ApiError
// the page surfaces verbatim.

import { useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "../client";
import { keys } from "../keys";

export type UserBan = components["schemas"]["UserBanRead"];
export type DomainBan = components["schemas"]["DomainBanRead"];

/** Both registers refresh together with the users list: a ban changes the users
 *  register's `banned` column as well as the ban register's own rows. */
const BAN_SLOTS = [keys.admin.userBans, keys.admin.domainBans, keys.admin.users] as const;

// ---- user bans ------------------------------------------------------------

/** Every account ban — active and lifted, newest first. */
export function useUserBans(enabled = true) {
  return useQuery({
    queryKey: keys.admin.userBans,
    queryFn: () => request(api.GET("/admin/v1/bans/users"), "could not load banned users"),
    enabled,
  });
}

/** Ban one account. The server refuses a self-ban and a staff account with a 422
 *  `ban_refused`, an already-banned account with a 409. */
export function useBanUser() {
  return useMutation({
    mutationFn: ({ userId, reason }: { userId: string; reason?: string }) =>
      request(
        api.POST("/admin/v1/bans/users", { body: { user_id: userId, reason: reason ?? "" } }),
        "could not ban the user",
      ),
    meta: { invalidates: BAN_SLOTS },
  });
}

/** Lift an account's active ban. 404 when there is none. */
export function useLiftUserBan() {
  return useMutation({
    mutationFn: (userId: string) =>
      request(
        api.DELETE("/admin/v1/bans/users/{user_id}", { params: { path: { user_id: userId } } }),
        "could not lift the ban",
      ),
    meta: { invalidates: BAN_SLOTS },
  });
}

// ---- domain bans ----------------------------------------------------------

/** Every email-domain ban — active and lifted, newest first. */
export function useDomainBans(enabled = true) {
  return useQuery({
    queryKey: keys.admin.domainBans,
    queryFn: () => request(api.GET("/admin/v1/bans/domains"), "could not load banned domains"),
    enabled,
  });
}

/** Ban an email domain. The domain is normalized server-side (lower-cased, a
 *  leading `@` and surrounding whitespace dropped) and refused with a 422 when
 *  what remains is not a bare hostname — so the field is sent as typed. */
export function useBanDomain() {
  return useMutation({
    mutationFn: ({ domain, reason }: { domain: string; reason?: string }) =>
      request(
        api.POST("/admin/v1/bans/domains", { body: { domain, reason: reason ?? "" } }),
        "could not ban the domain",
      ),
    meta: { invalidates: BAN_SLOTS },
  });
}

/** Lift a domain's active ban. 404 when there is none. */
export function useLiftDomainBan() {
  return useMutation({
    mutationFn: (domain: string) =>
      request(
        api.DELETE("/admin/v1/bans/domains/{domain}", { params: { path: { domain } } }),
        "could not lift the ban",
      ),
    meta: { invalidates: BAN_SLOTS },
  });
}
