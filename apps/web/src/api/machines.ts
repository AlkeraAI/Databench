// Machines an organization holds: the machines it has, who may use each, and
// which machine a workspace runs on.
//
//   GET    /api/v1/org/machines                            → OrgMachineRead[]
//   GET    /api/v1/org/machines/buying                     → MachineBuyingRead
//   POST   /api/v1/org/machines                            → 202 OrgMachineRead
//   POST   /api/v1/org/machines/ssh/test                   → SshMachineTestRead
//   POST   /api/v1/org/machines/ssh                        → 202 OrgMachineRead
//   GET    /api/v1/org/machines/{id}                       → OrgMachineDetail
//   PATCH  /api/v1/org/machines/{id}                       If-Match → OrgMachineRead
//   PUT    /api/v1/org/machines/{id}/audience              If-Match → OrgMachineRead
//   POST   /api/v1/org/machines/{id}/{start,stop,replace}  If-Match → 202 OrgMachineRead
//   DELETE /api/v1/org/machines/{id}                       If-Match → 202
//   GET    /api/v1/workspaces/{id}/machine                 → WorkspaceMachineRead
//   POST   /api/v1/workspaces/{id}/machine                 → 202 WorkspaceMachineMoveRead
//   POST   /api/v1/workspaces/{id}/machine/moves/{m}/cancel → 202
//   GET    /api/v1/org/usage/machines                      → MachineUsageRead
//   GET    /api/v1/org/compute/settings                    → OrgComputeSettingsRead
//   PUT    /api/v1/org/compute/settings                    If-Match → OrgComputeSettingsRead
//
// Every write on a machine names the version it was built on. One built on a
// reading another admin has since moved answers 409; the mutation policy then
// refreshes the machine reads, and the editor steps aside to show the current
// figures instead of overwriting them.

import { useMutation, useQuery } from "@tanstack/react-query";
import type { components } from "@alkera/sdk";

import { api, request } from "./client";
import { ApiError } from "./errors";
import { keys } from "./keys";

type Schemas = components["schemas"];

export type MachineCard = Schemas["MachineCard"];
export type MachineSpec = Schemas["MachineSpec"];
export type GpuSpec = Schemas["GpuSpec"];
export type AudienceGrant = Schemas["AudienceGrant"];
export type AudienceEntry = Schemas["AudienceEntry"];
export type OrgMachineRead = Schemas["OrgMachineRead"];
export type OrgMachineDetail = Schemas["OrgMachineDetail"];
export type OrgMachineUpdate = Schemas["OrgMachineUpdate"];
export type MachineQuote = Schemas["MachineQuote"];
export type MachineBuyingRead = Schemas["MachineBuyingRead"];
export type WorkspaceMachineRead = Schemas["WorkspaceMachineRead"];
export type WorkspaceMachineMoveRead = Schemas["WorkspaceMachineMoveRead"];
export type LostMachineRead = Schemas["LostMachineRead"];
export type MachineUnavailableRead = Schemas["MachineUnavailableRead"];
export type OrgComputeSettingsRead = Schemas["OrgComputeSettingsRead"];
export type OrgComputeSettingsUpdate = Schemas["OrgComputeSettingsUpdate"];
export type SshMachineTarget = Schemas["SshMachineTarget"];
export type SshMachineAdd = Schemas["SshMachineAdd"];
export type SshMachineTestRead = Schemas["SshMachineTestRead"];
export type SshEndpointRead = Schemas["SshEndpointRead"];

export type MachinePowerAction = "start" | "stop" | "replace";

/** The refusals a write names, so a dialog can say each in its own words. */
export const NAME_TAKEN = "name_taken";
export const MOVE_IN_PROGRESS = "move_in_progress";
export const MOVE_TARGET_NOT_SHARED = "move_target_not_shared";

/** Every machine read: the list, each machine's page, each workspace's
 *  machine (a rename or a stop shows on the workspace chip too), and the
 *  compute settings (deleting the default machine clears it). */
export const MACHINE_SLOTS = [
  keys.machines.org,
  keys.machines.workspaceAll,
  keys.machines.current,
  keys.machines.buying,
  keys.machines.computeSettings,
] as const;

const ifMatch = (version: number) => ({ "If-Match": String(version) });

/** A 403 or a 404 is an answer (not a manager, a machine gone), not a flake. */
export const settleStatusError = (count: number, err: unknown): boolean =>
  !(err instanceof ApiError && (err.status === 401 || err.status === 403 || err.status === 404)) && count < 1;

// ---- reads ------------------------------------------------------------------

/** Whether the org may buy another machine now, and if not, why ("plan": its
 *  plan buys none; "quota": it holds as many as it may). Any member may ask. */
export function useMachineBuying(enabled = true) {
  return useQuery<MachineBuyingRead>({
    queryKey: keys.machines.buying,
    enabled,
    retry: settleStatusError,
    queryFn: () =>
      request(
        api.GET("/api/v1/org/machines/buying"),
        "Could not check whether machines can be bought",
      ),
  });
}

/** The org's machines the caller may see: every one for an org admin, the ones
 *  their teams own or they may use for everyone else. */
export function useOrgMachines(enabled = true) {
  return useQuery<OrgMachineRead[]>({
    queryKey: keys.machines.org,
    enabled,
    retry: settleStatusError,
    // The shell's credit banner reads this on every page, so an answer that is not
    // a list (a proxy's page, a server from before machines) reads as none rather
    // than breaking the shell.
    queryFn: async () => {
      const rows = await request(
        api.GET("/api/v1/org/machines"),
        "The machines could not be listed",
      );
      return Array.isArray(rows) ? rows : [];
    },
  });
}

/** One machine with its workspaces and timeline. A machine the caller may not
 *  read is the same 404 as one that does not exist. */
export function useOrgMachine(machineId: string | undefined) {
  return useQuery<OrgMachineDetail>({
    queryKey: keys.machines.orgOne(machineId),
    enabled: Boolean(machineId),
    retry: settleStatusError,
    queryFn: () =>
      request(
        api.GET("/api/v1/org/machines/{machine_id}", {
          params: { path: { machine_id: machineId as string } },
        }),
        "The machine could not be read",
      ),
  });
}

/** What `machineId` would cost with its disk at `volumeGb`, and whether the
 *  grow would be admitted now. A size it cannot grow to is a 422. */
export function useDiskGrowQuote(machineId: string, volumeGb: number, enabled = true) {
  return useQuery<MachineQuote>({
    queryKey: keys.machines.diskQuote(machineId, volumeGb),
    enabled,
    retry: false,
    queryFn: () =>
      request(
        api.POST("/api/v1/org/machines/{machine_id}/disk/quote", {
          params: { path: { machine_id: machineId } },
          body: { volume_gb: volumeGb },
        }),
        "The price could not be read",
      ),
  });
}

/** Grow a machine's disk. Only larger; a RunPod machine restarts to grow it. */
export function useGrowDisk() {
  return useMutation({
    mutationFn: (vars: { machineId: string; version: number; volumeGb: number }) =>
      request<OrgMachineRead>(
        api.POST("/api/v1/org/machines/{machine_id}/disk", {
          params: { path: { machine_id: vars.machineId }, header: ifMatch(vars.version) },
          body: { volume_gb: vars.volumeGb },
        }),
        "The disk could not be grown",
      ),
    meta: { invalidates: MACHINE_SLOTS },
  });
}

/** How long a moving workspace's machine is re-read while the event stream is
 *  the faster path: the frame refreshes it, this keeps the steps moving if the
 *  stream is down. */
export const MOVE_POLL_MS = 3_000;

const MOVE_DONE: ReadonlySet<string> = new Set(["done", "failed", "canceled"]);

/** Whether a move is still under way. */
export function moveActive(move: WorkspaceMachineMoveRead | null | undefined): boolean {
  return move != null && !MOVE_DONE.has(move.state);
}

/** Where a workspace runs, what it may move to, and the move under way. */
export function useWorkspaceMachine(workspaceId: string | undefined) {
  return useQuery<WorkspaceMachineRead>({
    queryKey: keys.machines.workspace(workspaceId),
    enabled: Boolean(workspaceId),
    retry: settleStatusError,
    refetchInterval: (query) => (moveActive(query.state.data?.active_move) ? MOVE_POLL_MS : false),
    queryFn: () =>
      request(
        api.GET("/api/v1/workspaces/{workspace_id}/machine", {
          params: { path: { workspace_id: workspaceId as string } },
        }),
        "The workspace's machine could not be read",
      ),
  });
}

/** The org's compute settings. Org admins only. */
export function useOrgComputeSettings(enabled = true) {
  return useQuery<OrgComputeSettingsRead>({
    queryKey: keys.machines.computeSettings,
    enabled,
    retry: settleStatusError,
    queryFn: () =>
      request(api.GET("/api/v1/org/compute/settings"), "The compute settings could not be read"),
  });
}

// ---- writes -----------------------------------------------------------------

/** Change the org's compute settings. Fields left out are unchanged; a default
 *  machine sent as null clears it. */
export function useUpdateOrgComputeSettings() {
  return useMutation({
    mutationFn: (vars: { version: number; patch: OrgComputeSettingsUpdate }) =>
      request<OrgComputeSettingsRead>(
        api.PUT("/api/v1/org/compute/settings", {
          params: { header: ifMatch(vars.version) },
          body: vars.patch,
        }),
        "The compute settings could not be changed",
      ),
    // The machine list marks the default.
    meta: { invalidates: [keys.machines.computeSettings, keys.machines.org] },
  });
}

/** Connect to a host the org runs before adding it. Writes nothing. */
export function useTestSshMachine() {
  return useMutation({
    mutationFn: (body: SshMachineTarget) =>
      request<SshMachineTestRead>(
        api.POST("/api/v1/org/machines/ssh/test", { body }),
        "The connection could not be tested",
      ),
    meta: { invalidates: "none" },
  });
}

/** Add a host the org runs as an org machine. */
export function useAddSshMachine() {
  return useMutation({
    mutationFn: (body: SshMachineAdd) =>
      request<OrgMachineRead>(api.POST("/api/v1/org/machines/ssh", { body }), "The machine could not be added"),
    meta: { invalidates: MACHINE_SLOTS },
  });
}

/** Rename a machine or change its settings. Fields left out are unchanged; an
 *  idle stop or a cap sent as null clears it. */
export function useUpdateMachine() {
  return useMutation({
    mutationFn: (vars: { machineId: string; version: number; patch: OrgMachineUpdate }) =>
      request<OrgMachineRead>(
        api.PATCH("/api/v1/org/machines/{machine_id}", {
          params: { path: { machine_id: vars.machineId }, header: ifMatch(vars.version) },
          body: vars.patch,
        }),
        "The machine could not be changed",
      ),
    meta: { invalidates: MACHINE_SLOTS },
  });
}

/** Replace who may use a machine. */
export function useSetMachineAudience() {
  return useMutation({
    mutationFn: (vars: { machineId: string; version: number; audience: AudienceGrant[] }) =>
      request<OrgMachineRead>(
        api.PUT("/api/v1/org/machines/{machine_id}/audience", {
          params: { path: { machine_id: vars.machineId }, header: ifMatch(vars.version) },
          body: { audience: vars.audience },
        }),
        "Who can use the machine could not be changed",
      ),
    meta: { invalidates: MACHINE_SLOTS },
  });
}

/** Start, stop or replace a machine. A stop lets running chats finish unless
 *  `now`. */
export function useMachinePower() {
  return useMutation({
    mutationFn: (vars: {
      machineId: string;
      version: number;
      action: MachinePowerAction;
      now?: boolean;
    }) => {
      const params = { path: { machine_id: vars.machineId }, header: ifMatch(vars.version) };
      const failed = `The machine could not be ${vars.action === "start" ? "started" : vars.action === "stop" ? "stopped" : "replaced"}`;
      if (vars.action === "start") {
        return request<OrgMachineRead>(
          api.POST("/api/v1/org/machines/{machine_id}/start", { params }),
          failed,
        );
      }
      if (vars.action === "stop") {
        return request<OrgMachineRead>(
          api.POST("/api/v1/org/machines/{machine_id}/stop", {
            params: { ...params, query: { now: vars.now ?? false } },
          }),
          failed,
        );
      }
      return request<OrgMachineRead>(
        api.POST("/api/v1/org/machines/{machine_id}/replace", { params }),
        failed,
      );
    },
    meta: { invalidates: MACHINE_SLOTS },
  });
}

/** Delete a machine and its disk. */
export function useDeleteMachine() {
  return useMutation({
    mutationFn: async (vars: { machineId: string; version: number }) => {
      await request<unknown>(
        api.DELETE("/api/v1/org/machines/{machine_id}", {
          params: { path: { machine_id: vars.machineId }, header: ifMatch(vars.version) },
        }),
        "The machine could not be deleted",
      );
    },
    meta: { invalidates: MACHINE_SLOTS },
  });
}

/** Move a workspace to another machine (null: the org's default). */
export function useMoveWorkspace() {
  return useMutation({
    mutationFn: (vars: {
      workspaceId: string;
      toOrgMachineId: string | null;
      stopRunning: boolean;
      /** The workspace version the reader holds; a move built on an older one is refused. */
      workspaceVersion?: number;
    }) =>
      request<WorkspaceMachineMoveRead>(
        api.POST("/api/v1/workspaces/{workspace_id}/machine", {
          params: {
            path: { workspace_id: vars.workspaceId },
            ...(vars.workspaceVersion != null ? { header: ifMatch(vars.workspaceVersion) } : {}),
          },
          body: { to_org_machine_id: vars.toOrgMachineId, stop_running: vars.stopRunning },
        }),
        "The workspace could not be moved",
      ),
    meta: { invalidates: [keys.machines.workspaceAll, keys.workspaces.all, keys.chats.all] },
  });
}

/** Cancel a move that has not switched machines yet. */
export function useCancelMove() {
  return useMutation({
    mutationFn: async (vars: { workspaceId: string; moveId: string }) => {
      await request<unknown>(
        api.POST("/api/v1/workspaces/{workspace_id}/machine/moves/{move_id}/cancel", {
          params: { path: { workspace_id: vars.workspaceId, move_id: vars.moveId } },
        }),
        "The move could not be canceled",
      );
    },
    meta: { invalidates: [keys.machines.workspaceAll] },
  });
}
