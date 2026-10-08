// What each cell status looks like and says.

import { formatDuration } from "../components/dialogs/formatDuration";
import { actorLabel } from "../model/actor";
import type { CellStatus, KernelState, RunAttribution } from "../model/types";

export { formatDuration };

export type StatusTone = "neutral" | "accent" | "success" | "warning" | "danger" | "muted";

export interface StatusLook {
  /** The gutter's short label, read by screen readers. */
  label: string;
  /** The tooltip: what it means for this cell. */
  detail: string;
  tone: StatusTone;
  /** Animated while it lasts. */
  live?: boolean;
}

export const STATUS_LOOK: Record<CellStatus, StatusLook> = {
  fresh: { label: "Up to date", detail: "Ran with its current code, and so did everything it reads from.", tone: "success" },
  edited: { label: "Edited", detail: "The code changed since it last ran. Its output is from the earlier code.", tone: "warning" },
  stale: { label: "Stale", detail: "A cell it reads from ran or changed since. Run it to update.", tone: "warning" },
  not_run: { label: "Not run", detail: "Not run in this kernel.", tone: "muted" },
  queued: { label: "Queued", detail: "Waiting for the kernel.", tone: "accent" },
  running: { label: "Running", detail: "Running now.", tone: "accent", live: true },
  error: { label: "Error", detail: "The last run raised an error.", tone: "danger" },
  interrupted: { label: "Interrupted", detail: "The run was interrupted.", tone: "danger" },
  skipped: { label: "Skipped", detail: "Not run because a cell it reads from failed.", tone: "muted" },
  stopped: { label: "Stopped", detail: "Stopped by alkera.stop.", tone: "muted" },
  disabled: { label: "Disabled", detail: "Disabled: it and the cells that read from it do not run.", tone: "muted" },
};

export const KERNEL_LOOK: Record<KernelState, { label: string; tone: StatusTone }> = {
  absent: { label: "No kernel", tone: "muted" },
  starting: { label: "Starting", tone: "accent" },
  idle: { label: "Idle", tone: "success" },
  busy: { label: "Busy", tone: "accent" },
  restarting: { label: "Restarting", tone: "warning" },
  stopped: { label: "Stopped", tone: "muted" },
};

/** The kernel chip while the machine serving the notebook wakes. */
export const WAKING_LOOK: { label: string; tone: StatusTone } = { label: "Waking the machine…", tone: "accent" };

/** "just now", "2 min ago", "3 h ago", "4 days ago". */
export function formatAgo(iso: string | null, now: number): string {
  if (iso === null) return "";
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) return "";
  const s = Math.max(0, Math.round((now - at) / 1000));
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

/** The cell footer, as two parts. `when` always shows: "4 min ago · 1.2 s".
 *  `by` shows on hover or focus: "by Alice", "by Agent for Alice",
 *  with ", autorun" when the cell re-ran because a cell it reads changed. */
export interface RunFooter {
  when: string;
  by: string;
}

export function runFooter(run: RunAttribution | null, durationMs: number | null, now: number): RunFooter | null {
  if (run === null) return null;
  const when = [formatAgo(run.finished_at ?? run.started_at, now), durationMs !== null ? formatDuration(durationMs) : ""].filter((part) => part !== "").join(" · ");
  const by = `by ${actorLabel(run.by)}${run.trigger === "autorun" ? ", autorun" : ""}`;
  return { when, by };
}
