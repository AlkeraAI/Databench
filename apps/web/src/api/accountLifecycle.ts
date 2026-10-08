// React Query hooks for the person's own deletion request:
//
//   GET    /api/v1/me/account/deletion/plan → what deleting the account would do
//   GET    /api/v1/me/account/deletion      → the scheduled deletion, if any
//   POST   /api/v1/me/account/deletion      → schedule it (typed email + a current factor)
//   DELETE /api/v1/me/account/deletion      → cancel it

import { useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "./client";
import { keys } from "./keys";

export type DeletionPlan = components["schemas"]["DeletionPlanRead"];
export type DeletionStatus = components["schemas"]["DeletionStatusRead"];
export type DeletionRequestBody = components["schemas"]["DeletionRequestBody"];

/** What deleting the account would do now. Read only while the confirmation is open. */
export function useDeletionPlan(enabled: boolean) {
  return useQuery({
    queryKey: keys.account.deletionPlan,
    queryFn: () => request(api.GET("/api/v1/me/account/deletion/plan"), "could not load what deleting your account does"),
    enabled,
    staleTime: 0,
  });
}

/** The scheduled deletion, or null. */
export function useDeletionState() {
  return useQuery({
    queryKey: keys.account.deletion,
    queryFn: () => request(api.GET("/api/v1/me/account/deletion"), "could not load your account's deletion state"),
    select: (d) => d.request ?? null,
  });
}

export function useRequestDeletion() {
  return useMutation({
    mutationFn: (body: DeletionRequestBody) =>
      request(api.POST("/api/v1/me/account/deletion", { body }), "could not schedule your account's deletion"),
    meta: { invalidates: [keys.account.deletion, keys.account.deletionPlan, keys.auth.sessions] },
  });
}

export function useCancelDeletion() {
  return useMutation({
    mutationFn: () => request(api.DELETE("/api/v1/me/account/deletion"), "could not cancel the deletion"),
    meta: { invalidates: [keys.account.deletion] },
  });
}
