// The readings the org's Machines pages, the workspace chip and the chat banner
// share: who the reader is to machines, how a machine's use reads, and the words
// each refusal gets.

import { useMemo } from "react";

import { useCurrentUser } from "../../../api/auth";
import { useIdentityDashboard } from "../../../api/dashboard";
import { ApiError, refusalSentence } from "../../../api/errors";
import { useMachineBuying, type OrgMachineRead } from "../../../api/machines";
import { machineBilling } from "../../../app/extensions/portal";

export const MACHINES_PATH = "/settings/organization/machines";
export const machinePath = (id: string): string => `${MACHINES_PATH}/${encodeURIComponent(id)}`;

export const COPY = {
  title: "Machines",
  add: "Add machine",
  empty: "No machines yet.",
  orgPool: "Org pool",
  assignedTo: "Assigned to",
  noAudience: "Not assigned to anyone",
  everyone: "Everyone in the organization",
  conflict: "This machine changed while you were editing. The current settings are shown.",
  defaultMachine: "Default for new workspaces",
  noDefaultMachine: "None",
} as const;

/** Who the reader is to machines. A manager of a machine is an org admin or an
 *  admin of its owning team (or a team above it); the server says so per
 *  machine (`can_manage`), and these facts only decide which doors to show. */
export interface MachineViewer {
  userId: string | null;
  isOrgAdmin: boolean;
  /** Every team the reader administers, descent included. */
  adminTeamIds: ReadonlySet<string>;
  /** Administers something machines can belong to: the org, or some team. */
  mayManage: boolean;
  /** The org may run an org pool (the server's verdict). */
  orgPoolAllowed: boolean;
  /** The server lets the reader add a machine the org runs by its SSH details. */
  mayAdd: boolean;
  loading: boolean;
}

export function useMachineViewer(): MachineViewer {
  const identity = useIdentityDashboard();
  const me = useCurrentUser();
  const isOrgAdmin = identity.data?.is_org_admin === true;
  const adminIds = me.data?.admin_team_ids;
  const adminTeamIds = useMemo(() => new Set(adminIds ?? []), [adminIds]);
  const orgPoolAllowed = identity.data?.enterprise_features_enabled === true;
  const mayManage = isOrgAdmin || adminTeamIds.size > 0;
  const buying = useMachineBuying(mayManage);
  return {
    userId: me.data?.id ?? null,
    isOrgAdmin,
    adminTeamIds,
    mayManage,
    orgPoolAllowed,
    mayAdd: buying.data?.can_add === true,
    loading: identity.isPending || me.isPending,
  };
}

/** How a machine is used, in words: the org pool, or whom it is assigned to. */
export function machineUseLabel(machine: Pick<OrgMachineRead, "use_mode" | "audience">, orgPoolAllowed: boolean): {
  label: string;
  chips: string[];
} {
  if (machine.use_mode === "pool") {
    const unavailable = orgPoolAllowed ? null : machineBilling()?.poolUnavailableLabel;
    return { label: unavailable ?? COPY.orgPool, chips: [] };
  }
  if (machine.audience.length === 0) return { label: COPY.noAudience, chips: [] };
  return { label: COPY.assignedTo, chips: machine.audience.map((entry) => entry.label) };
}

/** The idle-stop choices, null being never. */
export const IDLE_OPTIONS: readonly { value: number | null; label: string }[] = [
  { value: null, label: "Never" },
  { value: 15, label: "15 minutes" },
  { value: 30, label: "30 minutes" },
  { value: 60, label: "60 minutes" },
  { value: 240, label: "4 hours" },
];

export function idleLabel(minutes: number | null | undefined): string {
  if (minutes == null) return "Never";
  return IDLE_OPTIONS.find((o) => o.value === minutes)?.label ?? `${minutes} minutes`;
}

/** The choices for a machine whose current setting is not one of the standard
 *  ones: the standard list with its own value kept, so opening the editor never
 *  silently changes it. */
export function idleOptionsWith(current: number | null | undefined): { value: number | null; label: string }[] {
  if (current == null || IDLE_OPTIONS.some((o) => o.value === current)) return [...IDLE_OPTIONS];
  return [...IDLE_OPTIONS, { value: current, label: `${current} minutes` }].sort(
    (a, b) => (a.value ?? -1) - (b.value ?? -1),
  );
}

export function isConflict(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 409;
}

export const errorText = (error: unknown, fallback: string): string =>
  refusalSentence(error, { fallback });

/** A host key as an admin compares it with `ssh-keygen -lf`: the key type the
 *  server named, then the fingerprint ("ED25519 SHA256:…"). A key whose type
 *  the server could not name is shown by its fingerprint alone. */
export function hostKeyText(keyType: string | null | undefined, fingerprint: string): string {
  return keyType ? `${keyType} ${fingerprint}` : fingerprint;
}
