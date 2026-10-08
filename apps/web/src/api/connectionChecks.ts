// Checking a connection before it is saved, and again on demand: the seam the
// connection dialog and the connections list read it through.
//
// The open platform saves a connection without a check (the server says so on
// each form with `checked_before_save`), and serves no check route. An extension
// that checks connections registers the hooks below on CONNECTION_CHECKS; with
// none registered, the hooks are inert: nothing is started, nothing is polled,
// and the list offers no "Verify now".

import { useState } from "react";
import type { components } from "@alkera/sdk";
import { ExtensionPoint } from "@alkera/ui/extensions";

import type { TeamConnectionUpsert } from "./teamConnections";

type Schemas = components["schemas"];

/** What a screen reads off one check. The server's check record satisfies it. */
export interface CheckRecord {
  id: string;
  state: Schemas["VerificationState"];
  outcome?: Schemas["Outcome"] | null;
  detail?: string | null;
  requested_at: string;
  dispatch_attempts: number;
}

interface MutateOptions<T> {
  onSuccess?: (data: T) => void;
  onError?: (error: unknown) => void;
}

/** Ask for a check of a candidate payload; answers with the record's id. */
export interface CheckStart {
  mutate: (body: TeamConnectionUpsert, options?: MutateOptions<{ id: string }>) => void;
  isPending: boolean;
}

/** One check record, read by its id while it is set. */
export interface CheckPoll {
  data: CheckRecord | undefined;
  isError: boolean;
  error: Error | null;
  refetch: () => unknown;
  /** When the server last answered about the record. */
  dataUpdatedAt: number;
}

/** Check a stored connection again, by its id. */
export interface CheckRerun {
  mutate: (connectionId: string, options?: MutateOptions<unknown>) => void;
  isPending: boolean;
}

/** The hooks a connection-check extension provides. Each is called on every render. */
export interface ConnectionChecks {
  key: string;
  useStartTeam: (teamId: string) => CheckStart;
  useStartMine: () => CheckStart;
  usePollTeam: (teamId: string, checkId: string | null) => CheckPoll;
  usePollMine: (checkId: string | null) => CheckPoll;
  useRerunTeam: (teamId: string) => CheckRerun;
  useRerunMine: () => CheckRerun;
}

export const CONNECTION_CHECKS = new ExtensionPoint<ConnectionChecks>("portal.connection_checks");

const NOT_INSTALLED = new Error("This install does not check connections.");
const inertStart: CheckStart = { mutate: (_body, options) => options?.onError?.(NOT_INSTALLED), isPending: false };
const inertPoll: CheckPoll = { data: undefined, isError: false, error: null, refetch: () => undefined, dataUpdatedAt: 0 };
const inertRerun: CheckRerun = { mutate: (_id, options) => options?.onError?.(NOT_INSTALLED), isPending: false };

const NO_CHECKS: ConnectionChecks = {
  key: "none",
  useStartTeam: () => inertStart,
  useStartMine: () => inertStart,
  usePollTeam: () => inertPoll,
  usePollMine: () => inertPoll,
  useRerunTeam: () => inertRerun,
  useRerunMine: () => inertRerun,
};

/** The installed checks, or inert ones; `installed` says which. Read once per component,
 *  after composition, so a render always calls the same hooks. */
export function useConnectionChecks(): { checks: ConnectionChecks; installed: boolean } {
  const [found] = useState(() => CONNECTION_CHECKS.items()[0]);
  return found ? { checks: found, installed: true } : { checks: NO_CHECKS, installed: false };
}
