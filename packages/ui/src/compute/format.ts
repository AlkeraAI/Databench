// How a machine reads to a person: its provider, its state and the step a
// starting machine is on. One module, so the platform fleet, the org's
// Machines page, the workspace chip and the chat banner say the same words for
// the same facts.

import { ExtensionPoint } from "../extensions";
import type { PillTone } from "../primitives/display/Pill";

/** The states an org machine reads as, plus the shared machines (which have none
 *  of their own). An unknown state from a newer server reads as itself. */
export type MachineCardState =
  | "starting"
  | "running"
  | "disk_full"
  | "unhealthy"
  | "unreachable"
  | "stopping"
  | "stopped"
  | "waiting_for_hardware"
  | "failed"
  | "deleted"
  | "shared";

export type StartingStep = "reserving" | "booting" | "installing" | "connecting";

/** A compute provider's name. `key` is the provider as the server spells it. */
export interface ProviderLabel {
  readonly key: string;
  readonly label: string;
}

/** The providers the platform runs on. A product that adds a cloud provider registers its
 *  name here during composition. */
export const PROVIDER_LABELS = new ExtensionPoint<ProviderLabel>("compute.provider_labels");

const BUILT_IN_PROVIDERS: readonly ProviderLabel[] = [{ key: "localdev", label: "Local box" }];

/** Every provider's name, registered ones first, in the order the console offers them. */
export function providerLabels(): readonly ProviderLabel[] {
  return [...PROVIDER_LABELS.items(), ...BUILT_IN_PROVIDERS];
}

/** The product's name for a provider; an unknown one shows as the server spelt it. */
export function providerLabel(provider: string): string {
  return providerLabels().find((entry) => entry.key === provider)?.label ?? provider;
}

/** How a machine's running rate reads, where machines are priced. The open platform prices
 *  nothing and registers none, so no card names a rate; a product that bills machines
 *  registers its reading during composition. The first registration is used. */
export interface MachineRateFormat {
  readonly key: string;
  /** A rate in nanos per minute as the reader sees it ("$1.89/hour"). */
  readonly format: (nanosPerMinute: number) => string;
}

export const MACHINE_RATE_FORMAT = new ExtensionPoint<MachineRateFormat>("compute.machine_rate_format");

/** The rate in words, or null where nothing prices machines. */
export function machineRateText(nanosPerMinute: number): string | null {
  return MACHINE_RATE_FORMAT.items()[0]?.format(nanosPerMinute) ?? null;
}

const STATE_LABEL: Record<MachineCardState, string> = {
  starting: "Starting",
  running: "Running",
  disk_full: "Almost out of disk",
  unhealthy: "Can't run chats",
  unreachable: "Not responding",
  stopping: "Stopping",
  stopped: "Stopped",
  waiting_for_hardware: "Waiting for hardware",
  failed: "Couldn't start",
  deleted: "Deleted",
  shared: "Shared",
};

const STATE_TONE: Record<MachineCardState, PillTone> = {
  starting: "neutral",
  running: "success",
  disk_full: "warning",
  unhealthy: "danger",
  unreachable: "danger",
  stopping: "neutral",
  stopped: "catNeutral",
  waiting_for_hardware: "warning",
  failed: "danger",
  deleted: "catNeutral",
  shared: "neutral",
};

/** The state a card reads as: a running machine the server says is almost
 *  out of disk reads that way, ranked over running and under every fault. */
export function shownMachineState(card: { state: string; disk_full?: boolean | null }): string {
  return card.state === "running" && card.disk_full ? "disk_full" : card.state;
}

/** The server's stock verdict for a size (``StockRead.state``), in words. */
const STOCK_LABEL: Record<string, string> = {
  in_stock: "In stock",
  limited: "Limited",
  out_of_stock: "Out of stock",
  unknown: "Stock unknown",
};

const STOCK_TONE: Record<string, PillTone> = {
  in_stock: "success",
  limited: "warning",
  out_of_stock: "danger",
  unknown: "neutral",
};

/** A wall-clock time as a person reads it here: "12:47 PM". */
export function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** A machine waiting for hardware, in one sentence: the server's reason and
 *  when the server asks the provider again, or until when it keeps asking. */
export function waitLine(wait: { reason: string; next_try_at?: string | null; gives_up_at: string }): string {
  const when = wait.next_try_at
    ? `Trying again at ${clockTime(wait.next_try_at)}.`
    : `Retrying until ${clockTime(wait.gives_up_at)}.`;
  return `${wait.reason} ${when}`;
}

export function stockLabel(state: string): string {
  return STOCK_LABEL[state] ?? state;
}

export function stockTone(state: string): PillTone {
  return STOCK_TONE[state] ?? "neutral";
}

export function machineStateLabel(state: string): string {
  return (STATE_LABEL as Record<string, string>)[state] ?? state;
}

export function machineStateTone(state: string): PillTone {
  return (STATE_TONE as Record<string, PillTone>)[state] ?? "neutral";
}

const STEP_LABEL: Record<StartingStep, string> = {
  reserving: "Reserving hardware",
  booting: "Booting",
  installing: "Installing the runtime",
  connecting: "Connecting",
};

export function startingStepLabel(step: string): string {
  return (STEP_LABEL as Record<string, string>)[step] ?? "Starting";
}

/** Where a starting step stands at `now`: whole minutes left (at least one, so a
 *  step running over its typical time still reads as nearly done rather than as
 *  zero) and the fraction of the step's typical time spent, held below 1 so the
 *  bar never claims a finish the machine has not reported. Null without timings. */
export function stepProgress(
  stepStartedAt: string | null | undefined,
  expectedSeconds: number | null | undefined,
  now: number,
): { minutesLeft: number; fraction: number } | null {
  if (!stepStartedAt || !expectedSeconds || expectedSeconds <= 0) return null;
  const started = Date.parse(stepStartedAt);
  if (Number.isNaN(started)) return null;
  const elapsed = Math.max(0, (now - started) / 1000);
  const left = expectedSeconds - elapsed;
  return {
    minutesLeft: Math.max(1, Math.ceil(left / 60)),
    fraction: Math.min(0.95, elapsed / expectedSeconds),
  };
}

/** "Installing the runtime · about 3 min", or the step alone without timings. */
export function startingLine(
  step: string | null | undefined,
  stepStartedAt: string | null | undefined,
  expectedSeconds: number | null | undefined,
  now: number,
): string {
  const label = step ? startingStepLabel(step) : "Starting";
  const progress = stepProgress(stepStartedAt, expectedSeconds, now);
  return progress ? `${label} · about ${progress.minutesLeft} min` : label;
}

/** When a machine draining for money stops, in words: "stops at 2:32 PM" at the
 *  reader's local time, or "stops in a few minutes" when the time is unknown. */
export function drainStopsWhen(stopsAt: string | null | undefined): string {
  const at = stopsAt ? new Date(stopsAt) : null;
  if (!at || Number.isNaN(at.getTime())) return "stops in a few minutes";
  return `stops at ${at.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
}
