// A machine as every surface draws it: its name, and under it the specs a person
// picks a machine by. Pure presentation over the server's machine card, so the
// org's Machines page, the workspace chip, the change-machine list and the chat
// banner can never describe the same machine two ways.

import type { ReactElement } from "react";

import { IconServer } from "@tabler/icons-react";

import { cx } from "../primitives/cx";
import { Meter } from "../primitives/display/Meter";
import { Pill } from "../primitives/display/Pill";
import { StatusPill, type StatusFactData } from "../primitives/display/StatusPill";
import { Tooltip } from "../primitives/overlays/Tooltip";

import {
  machineStateLabel,
  machineStateTone,
  providerLabel,
  machineRateText,
  shownMachineState,
  startingLine,
  waitLine,
  stepProgress,
} from "./format";
import "./machine-card.css";

export interface MachineGpu {
  name: string;
  count: number;
  memory_gb: number;
}

/** The hardware and the reader's rates of one machine (a subset of the server's
 *  `MachineSpec`, so the generated shape satisfies it). */
export interface MachineSpecData {
  offering_name?: string;
  provider: string;
  region: string;
  gpu?: MachineGpu | null;
  vcpu: number;
  memory_gb: number;
  disk_gb: number;
  /** Absent when the reader may not see rates. */
  rate_per_minute_nanos?: number | null;
  storage_rate_per_minute_nanos?: number | null;
}

/** One machine as the server describes it (a subset of `MachineCard`). */
export interface MachineCardData {
  kind: string;
  org_machine_id?: string | null;
  name: string;
  /** Absent for the shared machines: which box serves a chat is not the reader's. */
  spec?: MachineSpecData | null;
  state: string;
  step?: string | null;
  step_started_at?: string | null;
  step_expected_seconds?: number | null;
  stop_reason?: string;
  /** When a machine draining because its credit (or its cap) ran out stops. */
  drain_stops_at?: string | null;
  /** Why an `unhealthy` machine runs no chat, in the server's words. */
  fault?: { code: string; message: string } | null;
  /** The server says its disk is almost full. */
  disk_full?: boolean | null;
  /** Where the machine stands, as the server wrote it. Drawn in place of the
   *  words this file would otherwise choose for `state`, which remain only for
   *  a server that sends none. */
  status?: StatusFactData | null;
  /** Why a machine waits for hardware, and when the server asks again. */
  wait?: { reason: string; next_try_at?: string | null; gives_up_at: string } | null;
}

/** "1x A100 80 GB": the count and the name, with the memory when the name does
 *  not already carry it. */
function gpuPart(gpu: MachineGpu): string {
  const memory = gpu.memory_gb > 0 && !/\d\s*GB/i.test(gpu.name) ? ` ${gpu.memory_gb} GB` : "";
  return `${gpu.count}x ${gpu.name}${memory}`;
}

/** The spec line's parts, without the state, omitting any the machine lacks. */
export function machineSpecParts(card: MachineCardData): string[] {
  const spec = card.spec;
  if (!spec) return [];
  const parts: string[] = [];
  if (spec.gpu && spec.gpu.count > 0) parts.push(gpuPart(spec.gpu));
  if (spec.vcpu > 0) parts.push(`${spec.vcpu} vCPU`);
  if (spec.memory_gb > 0) parts.push(`${spec.memory_gb} GB memory`);
  if (spec.disk_gb > 0) parts.push(`${spec.disk_gb} GB disk`);
  const where = [spec.provider ? providerLabel(spec.provider) : "", spec.region].filter(Boolean).join(" ");
  if (where) parts.push(where);
  const rate = spec.rate_per_minute_nanos != null ? machineRateText(spec.rate_per_minute_nanos) : null;
  if (rate) parts.push(rate);
  return parts;
}

/** The whole spec line in words, the state last: what a tooltip or an option says. */
export function machineSpecLine(card: MachineCardData, now: number = Date.now()): string {
  if (card.kind === "shared" || card.state === "shared") return card.name;
  const state =
    card.state === "starting"
      ? startingLine(card.step, card.step_started_at, card.step_expected_seconds, now)
      : machineStateLabel(shownMachineState(card));
  return [...machineSpecParts(card), state].join(" · ");
}

export interface MachineCardViewProps {
  card: MachineCardData;
  /** `full` (default): name over the spec line. `compact`: the name and a state
   *  dot, the spec in a tooltip, for a header or a rail. */
  variant?: "full" | "compact";
  /** Full only: `all` (default) adds the rate and the state; `rate` adds the rate
   *  but no state, for a machine not bought yet; `hardware` keeps to the hardware
   *  and where it runs. */
  facts?: "all" | "rate" | "hardware";
  /** The clock a starting machine's step is read against. */
  now?: number;
  /** Compact only: renders the chip as a button. */
  onClick?: () => void;
  /** Compact only: what the chip says instead of the state while something is
   *  happening to it ("Saving chats"). */
  status?: string;
  className?: string;
}

function isShared(card: MachineCardData): boolean {
  return card.kind === "shared" || card.state === "shared";
}

function CompactCard({ card, now, onClick, status, className }: Required<Pick<MachineCardViewProps, "card" | "now">> & MachineCardViewProps): ReactElement {
  const shared = isShared(card);
  const tone = card.status ? card.status.tone : machineStateTone(shownMachineState(card));
  const tip = shared ? null : machineSpecLine(card, now);
  const body = (
    <>
      <IconServer className="alk-machine__icon" size={14} stroke={1.8} aria-hidden />
      {shared ? null : <span className="alk-machine__dot" data-tone={tone} aria-hidden="true" />}
      <span className="alk-machine__name">{card.name}</span>
      {status ? <span className="alk-machine__status">{status}</span> : null}
    </>
  );
  const cls = cx("alk-machine", "alk-machine--compact", onClick && "alk-machine--interactive", className);
  const said = card.status ? card.status.label : machineStateLabel(shownMachineState(card));
  const label = [card.name, status ?? (shared ? null : said)].filter(Boolean).join(", ");
  if (tip === null) {
    return onClick ? (
      <button type="button" className={cls} data-state={card.state} onClick={onClick} aria-label={label}>
        {body}
      </button>
    ) : (
      <span className={cls} data-state={card.state}>
        {body}
      </span>
    );
  }
  return (
    <Tooltip label={tip} side="bottom" wrap maxWidth={360}>
      {(trigger) =>
        onClick ? (
          <button {...trigger} type="button" className={cls} data-state={card.state} onClick={onClick} aria-label={label}>
            {body}
          </button>
        ) : (
          <span {...trigger} className={cls} data-state={card.state} tabIndex={0} role="group" aria-label={label}>
            {body}
          </span>
        )
      }
    </Tooltip>
  );
}

/** A machine's state as a chip, with the step and a progress bar while it starts:
 *  the state column of a table that shows the hardware beside it. */
export function MachineStateView({ card, now = Date.now() }: { card: MachineCardData; now?: number }): ReactElement {
  if (card.status) {
    // The server's words: the chip, and its sentence under it while the state
    // carries one worth reading (a step and its estimate, a reason).
    // The server sends a step's times only while the machine starts.
    const progress = stepProgress(card.step_started_at, card.step_expected_seconds, now);
    return (
      <span className="alk-machine__state">
        <StatusPill status={card.status} />
        {card.status.reason_code ? (
          <span className="alk-machine__step">
            <span>{card.status.sentence}</span>
            {progress ? <Meter value={progress.fraction} tone="neutral" label={`${card.name} starting`} /> : null}
          </span>
        ) : null}
      </span>
    );
  }
  const starting = card.state === "starting";
  const progress = starting ? stepProgress(card.step_started_at, card.step_expected_seconds, now) : null;
  return (
    <span className="alk-machine__state">
      <Pill tone={machineStateTone(shownMachineState(card))} dot shape="rect" data-state={card.state}>
        {machineStateLabel(shownMachineState(card))}
      </Pill>
      {starting ? (
        <span className="alk-machine__step">
          <span>{startingLine(card.step, card.step_started_at, card.step_expected_seconds, now)}</span>
          {progress ? <Meter value={progress.fraction} tone="neutral" label={`${card.name} starting`} /> : null}
        </span>
      ) : null}
      {card.state === "waiting_for_hardware" && card.wait ? (
        <span className="alk-machine__step">{waitLine(card.wait)}</span>
      ) : null}
    </span>
  );
}

/** A machine: its name, and its specs and state under it. `facts="hardware"`
 *  keeps to the hardware and where it runs, for a table whose other columns
 *  carry the rate and the state. */
export function MachineCardView({
  card,
  variant = "full",
  facts = "all",
  now = Date.now(),
  onClick,
  status,
  className,
}: MachineCardViewProps): ReactElement {
  if (variant === "compact") {
    return <CompactCard card={card} now={now} onClick={onClick} status={status} className={className} />;
  }
  if (isShared(card)) {
    return (
      <div className={cx("alk-machine", className)} data-state={card.state}>
        <span className="alk-machine__name">{card.name}</span>
      </div>
    );
  }
  const withState = facts === "all";
  const withRate = facts !== "hardware";
  const parts = machineSpecParts(withRate ? card : { ...card, spec: card.spec ? { ...card.spec, rate_per_minute_nanos: null } : null });
  return (
    <div className={cx("alk-machine", className)} data-state={withState ? card.state : undefined}>
      <span className="alk-machine__name">{card.name}</span>
      {parts.length > 0 || withState ? (
        <span className="alk-machine__spec">
          {parts.length > 0 ? <span className="alk-machine__parts">{parts.join(" · ")}</span> : null}
          {withState ? <MachineStateView card={card} now={now} /> : null}
        </span>
      ) : null}
    </div>
  );
}
