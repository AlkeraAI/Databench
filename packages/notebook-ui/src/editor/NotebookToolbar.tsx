// The notebook's toolbar: the kernel and what it is doing, the run controls,
// the environment, memory, the queue, who is here, find, settings (where
// reactivity is chosen) and the panels.


import { formatBytes } from "../components/panels/labels";
import { actorLabel } from "../model/actor";
import { envLabel, envName, envPython } from "../model/env";
import type { CellRuntime, EnvInfo, InterruptPhase, KernelView, Presence } from "../model/types";
import type { CommandId } from "./commands";
import { Menu, type MenuItem } from "./Menu";
import { KERNEL_LOOK, WAKING_LOOK } from "./status";

export type PanelId = "graph" | "variables" | "outline" | "environment" | "settings";

export const PANEL_LABEL: Record<PanelId, string> = {
  graph: "Graph",
  variables: "Variables",
  outline: "Outline",
  environment: "Environment",
  settings: "Settings",
};

/** The panels the Panels menu lists; Settings is its own gear key. */
const MENU_PANELS = ["graph", "variables", "outline", "environment"] as const satisfies readonly PanelId[];

/** The memory meter warns from this share of the guard's threshold. */
export const MEMORY_WARN_SHARE = 0.8;

export interface NotebookToolbarProps {
  kernel: KernelView;
  /** The machine serving the notebook is being woken. */
  waking?: boolean;
  envs: readonly EnvInfo[];
  presence: readonly Presence[];
  canRun: boolean;
  panel: PanelId | null;
  narrow: boolean;
  findOpen: boolean;
  /** Where the kernel chip jumps: see {@link kernelChipTarget}. */
  chipTarget: KernelChipTarget | null;
  keyFor(id: CommandId): string | undefined;
  onCommand(id: CommandId): void;
  onEnv(envId: string): void;
  onPanel(panel: PanelId | null): void;
  onFindToggle(): void;
  onShowCell(id: string): void;
}

export interface KernelChipTarget {
  cellId: string;
  running: boolean;
}

/** The lowest cell running now; with none running, the cell whose run ended
 *  last. Null when no cell has run in this kernel. */
export function kernelChipTarget(
  order: readonly string[],
  runtimeOf: (id: string) => Pick<CellRuntime, "status" | "last_run">,
): KernelChipTarget | null {
  const running = [...order].reverse().find((id) => runtimeOf(id).status === "running");
  if (running !== undefined) return { cellId: running, running: true };
  let latest: { cellId: string; at: string } | null = null;
  for (const id of order) {
    const run = runtimeOf(id).last_run;
    const at = run?.finished_at ?? run?.started_at ?? null;
    if (at !== null && (latest === null || Date.parse(at) > Date.parse(latest.at))) latest = { cellId: id, at };
  }
  return latest === null ? null : { cellId: latest.cellId, running: false };
}

const INTERRUPT_LABEL: Record<InterruptPhase, string> = {
  none: "Interrupt",
  signalled: "Interrupting",
  resignalled: "Interrupting again",
  restarting: "Restarting the kernel",
};

export function NotebookToolbar(props: NotebookToolbarProps) {
  const { kernel, canRun } = props;
  const look = props.waking ? WAKING_LOOK : KERNEL_LOOK[kernel.state];
  const busy = kernel.state === "busy" || kernel.state === "starting" || kernel.interrupt !== "none";
  const memoryShare =
    kernel.memory_bytes !== null && kernel.memory_limit_bytes !== null && kernel.memory_limit_bytes > 0
      ? kernel.memory_bytes / kernel.memory_limit_bytes
      : null;
  const item = (id: CommandId, label?: string, extra: Partial<MenuItem> = {}): MenuItem => ({
    id,
    label: label ?? id,
    keys: props.keyFor(id),
    ...extra,
  });
  const runItems: MenuItem[] = [
    item("run.all", "Run all"),
    item("run.stale", "Run stale"),
    item("kernel.restart", "Restart", { separated: true }),
    item("kernel.restart_run_all", "Restart and run all"),
    item("kernel.interrupt_clear", "Interrupt and clear the queue"),
    item("kernel.shutdown", "Shut down", { disabled: kernel.state === "absent" || kernel.state === "stopped" }),
    item("output.clear_all", "Clear all outputs", { separated: true }),
  ];
  const env = kernel.env;
  // One face per actor: an agent in three cells is one agent here.
  const here = props.presence.filter((p, i, all) => all.findIndex((q) => q.id === p.id) === i);
  return (
    <div className={`nb-toolbar${props.narrow ? " nb-toolbar--narrow" : ""}`} role="toolbar" aria-label="Notebook">
      {props.chipTarget !== null ? (
        <button
          type="button"
          className={`nb-chip nb-chip--${look.tone} nb-chip--action`}
          aria-live="polite"
          title={props.chipTarget.running ? "Show the running cell" : "Show the last cell that ran"}
          onClick={() => props.onShowCell(props.chipTarget!.cellId)}
        >
          <span className={`nb-dot nb-dot--${look.tone}`} aria-hidden />
          {look.label}
        </button>
      ) : (
        <span className={`nb-chip nb-chip--${look.tone}`} aria-live="polite" title="Kernel">
          <span className={`nb-dot nb-dot--${look.tone}`} aria-hidden />
          {look.label}
        </span>
      )}
      {canRun ? (
        <>
          <button type="button" className="nb-button" onClick={() => props.onCommand("run.all")} title={titled("Run all", props.keyFor("run.all"))}>
            Run all
          </button>
          <button
            type="button"
            className="nb-button"
            onClick={() => props.onCommand("run.stale")}
            title={titled("Run stale", props.keyFor("run.stale"))}
          >
            Run stale
          </button>
          <button
            type="button"
            className="nb-button"
            disabled={!busy}
            aria-live="polite"
            onClick={() => props.onCommand("kernel.interrupt")}
            title={titled(INTERRUPT_LABEL[kernel.interrupt], props.keyFor("kernel.interrupt"))}
          >
            {INTERRUPT_LABEL[kernel.interrupt]}
          </button>
          <Menu label="Run and kernel" trigger="Kernel ▾" items={runItems} onPick={(id) => props.onCommand(id as CommandId)} align="start" />
        </>
      ) : null}
      <Menu
        label="Environment"
        trigger={env ? (props.narrow ? (envPython(env) ?? envName(env)) : envLabel(env)) : "Environment"}
        className="nb-chip-menu"
        align="start"
        items={
          props.envs.length > 0
            ? props.envs.map((e) => ({
                id: e.env_id,
                label: envLabel(e),
                checked: env?.env_id === e.env_id,
                disabled: !canRun || e.state === "missing",
              }))
            : [{ id: "_none", label: "No other environments found", disabled: true }]
        }
        onPick={(id) => props.onEnv(id)}
      />
      {memoryShare !== null ? <MemoryMeter used={kernel.memory_bytes!} limit={kernel.memory_limit_bytes!} share={memoryShare} /> : null}
      <span className="nb-toolbar__spacer" />
      {here.length > 0 ? (
        <span className="nb-toolbar__presence" aria-label={`In this notebook: ${here.map((p) => actorLabel(p)).join(", ")}`}>
          {here.slice(0, 5).map((p) => (
            <span key={p.id} className={`nb-face nb-face--${p.kind}`} style={{ ["--nb-hue" as string]: String(p.hue) }} title={actorLabel(p)}>
              {actorLabel(p).slice(0, 1).toUpperCase() || "?"}
            </span>
          ))}
          {here.length > 5 ? <span className="nb-face nb-face--more">+{here.length - 5}</span> : null}
        </span>
      ) : null}
      <button
        type="button"
        className="nb-icon-button nb-icon-button--glyph"
        aria-label="Find"
        aria-pressed={props.findOpen}
        title={titled("Find", props.keyFor("find.open"))}
        onClick={props.onFindToggle}
      >
        <FindGlyph />
      </button>
      <button
        type="button"
        className="nb-icon-button nb-icon-button--glyph"
        aria-label={PANEL_LABEL.settings}
        aria-pressed={props.panel === "settings"}
        title={PANEL_LABEL.settings}
        onClick={() => props.onPanel(props.panel === "settings" ? null : "settings")}
      >
        <GearGlyph />
      </button>
      {/* The other panels are one menu, wide or narrow: a row of tabs was too many keys. */}
      <Menu
        label="Panels"
        className="nb-menu--glyph"
        trigger={<MenuGlyph />}
        items={MENU_PANELS.map((panel) => ({ id: panel, label: PANEL_LABEL[panel], checked: props.panel === panel }))}
        onPick={(id) => props.onPanel(props.panel === id ? null : (id as PanelId))}
      />
    </div>
  );
}

function FindGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden>
      <circle cx="7" cy="7" r="4.5" />
      <path d="M10.4 10.4 14 14" />
    </svg>
  );
}

function MenuGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden>
      <path d="M2.5 4h11M2.5 8h11M2.5 12h11" />
    </svg>
  );
}

function GearGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="8" cy="8" r="2.2" />
      <path d="M8 1.5v1.8M8 12.7v1.8M14.5 8h-1.8M3.3 8H1.5M12.6 3.4l-1.3 1.3M4.7 11.3l-1.3 1.3M12.6 12.6l-1.3-1.3M4.7 4.7 3.4 3.4" />
      <circle cx="8" cy="8" r="4.6" />
    </svg>
  );
}

function titled(label: string, keys: string | undefined): string {
  return keys ? `${label} (${keys})` : label;
}

export function MemoryMeter({ used, limit, share }: { used: number; limit: number; share: number }) {
  const warn = share >= MEMORY_WARN_SHARE;
  const pct = Math.min(100, Math.round(share * 100));
  return (
    <span
      className={`nb-memory${warn ? " nb-memory--warn" : ""}`}
      role="meter"
      aria-label="Memory"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
      aria-valuetext={`${formatBytes(used)} of ${formatBytes(limit)}`}
      title={warn ? `Memory ${pct}%: the kernel is stopped at the limit` : `Memory ${formatBytes(used)} of ${formatBytes(limit)}`}
    >
      <span className="nb-memory__bar" style={{ width: `${pct}%` }} />
      {warn ? <span className="nb-memory__label">Memory {pct}%</span> : null}
    </span>
  );
}

