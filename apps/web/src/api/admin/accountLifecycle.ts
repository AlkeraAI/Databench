// The support tool's side of a person's deletion request. Support staff read; platform
// admins act.

import { useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "../client";
import { keys } from "../keys";

export type AdminAccount = components["schemas"]["AdminAccountRead"];

export function useAdminAccount(userId: string | undefined) {
  return useQuery({
    queryKey: keys.admin.userAccount(userId),
    queryFn: () =>
      request(
        api.GET("/admin/v1/users/{user_id}/account", { params: { path: { user_id: userId! } } }),
        "could not load this person's account requests",
      ),
    enabled: !!userId,
  });
}

export function useAdminScheduleDeletion(userId: string) {
  return useMutation({
    mutationFn: (immediate: boolean) =>
      request(
        api.POST("/admin/v1/users/{user_id}/account/deletion", {
          params: { path: { user_id: userId } },
          body: { immediate },
        }),
        "could not schedule the deletion",
      ),
    meta: { invalidates: [keys.admin.userAccount(userId), keys.admin.users] },
  });
}

export function useAdminCancelDeletion(userId: string) {
  return useMutation({
    mutationFn: () =>
      request(
        api.DELETE("/admin/v1/users/{user_id}/account/deletion", { params: { path: { user_id: userId } } }),
        "could not cancel the deletion",
      ),
    meta: { invalidates: [keys.admin.userAccount(userId)] },
  });
}
