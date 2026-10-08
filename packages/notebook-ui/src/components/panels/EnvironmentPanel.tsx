// The notebook's environment: what it is, its packages, and a way to install
// more when the person may run code.

import { useId, useState } from "react";
import type { FormEvent } from "react";
import { envKindLabel, envName, envSpecPath } from "../../model/env";
import type { EnvActionName, EnvInfo, InstallStatus, KernelState } from "../../model/types";
import "./panels.css";

export interface EnvPackage {
  name: string;
  version?: string | null;
}

export interface EnvironmentPanelProps {
  env: EnvInfo | null;
  kernelState?: KernelState;
  packages: readonly EnvPackage[];
  /** What the environment's spec asks for, which is what can be removed. */
  requirements?: readonly string[];
  /** Whether this person may run code, and so install packages. */
  canRun: boolean;
  /** An install is under way: the form waits for it. */
  installing?: boolean;
  /** The last install: under way, or how it ended. */
  install?: InstallStatus | null;
  /** Whether the workspace's members share its environments on this machine. */
  shared?: boolean;
  onInstall: (packages: string[]) => void;
  onChange?: (action: "build" | "remove" | "cancel", packages: string[]) => void;
}

/** The environment's state as a person reads it. */
export const ENV_STATE_TEXT: Record<string, string> = {
  ready: "Ready",
  stale: "Changed since it was built",
  missing: "Not built yet",
  building: "Building",
  failed: "The build failed",
};

/** The package name a requirement names (`pandas>=2` is `pandas`). */
export function requirementName(requirement: string): string {
  return /^[A-Za-z0-9._-]+/.exec(requirement.trim())?.[0] ?? requirement.trim();
}

const RUNNING: Record<EnvActionName, string> = {
  install: "Installing",
  remove: "Removing",
  build: "Building the environment",
  cancel: "Cancelling the build",
};
const DONE: Record<EnvActionName, string> = {
  install: "Installed",
  remove: "Removed",
  build: "Built the environment",
  cancel: "Cancelled the build",
};
const FAILED: Record<EnvActionName, string> = {
  install: "Could not install",
  remove: "Could not remove",
  build: "Could not build the environment",
  cancel: "Could not cancel the build",
};

/** What the panel says on a machine where each member keeps its own
 *  environments. */
export const ENVS_NOT_SHARED = "Environments aren't shared on this machine";

const KERNEL_STATES: Record<KernelState, string> = {
  absent: "Not started",
  starting: "Starting",
  idle: "Idle",
  busy: "Busy",
  restarting: "Restarting",
  stopped: "Stopped",
};

/** Package specs from what was typed: one per whitespace-separated word, so
 *  `pandas>=2,<3` stays one spec. */
export function parsePackageSpecs(text: string): string[] {
  return text.split(/\s+/).filter((spec) => spec !== "");
}

/** What the panel says about an install: "Installing polars…", "Installed
 *  polars.", why it failed, or that no result came back. */
export function installLine(install: InstallStatus): string {
  const action = install.action ?? "install";
  const names = install.packages.join(", ");
  const what = (verb: string) => (names !== "" ? `${verb} ${names}` : action === "install" || action === "remove" ? `${verb} the packages` : verb);
  switch (install.status) {
    case "running":
      return `${what(RUNNING[action])}…`;
    case "ok":
      return `${what(DONE[action])}.`;
    case "error": {
      const why = install.message?.trim().replace(/\.+$/, "") ?? "";
      return why === "" ? `${what(FAILED[action])}.` : `${what(FAILED[action])}: ${why}.`;
    }
    case "unknown":
      return `No result arrived for ${what(RUNNING[action]).replace(/^./, (c) => c.toLowerCase())}.`;
  }
}

export function EnvironmentPanel({
  env,
  kernelState,
  packages,
  requirements = [],
  canRun,
  installing = false,
  install = null,
  shared = true,
  onInstall,
  onChange,
}: EnvironmentPanelProps) {
  const [draft, setDraft] = useState("");
  const inputId = useId();
  const specs = parsePackageSpecs(draft);
  const allowed = new Set<EnvActionName>(env?.allowed_actions ?? ["install"]);
  const may = (action: EnvActionName) => canRun && allowed.has(action) && (action === "install" || onChange !== undefined);
  const canInstall = may("install") && !installing && specs.length > 0;

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    if (!canInstall) return;
    onInstall(specs);
    setDraft("");
  };

  const spec = env ? envSpecPath(env) : null;
  const sorted = [...packages].sort((a, b) => a.name.localeCompare(b.name));

  return (
    <section className="nb-panel" aria-label="Environment">
      {env ? (
        <dl className="nb-facts">
          <dt>Name</dt>
          <dd>{envName(env)}</dd>
          {env.python.trim() !== "" ? (
            <>
              <dt>Python</dt>
              <dd>{env.python}</dd>
            </>
          ) : null}
          {env.kind !== "default" ? (
            <>
              <dt>Kind</dt>
              <dd>{envKindLabel(env.kind)}</dd>
            </>
          ) : null}
          {spec !== null ? (
            <>
              <dt>Spec</dt>
              <dd className="nb-mono">{spec}</dd>
            </>
          ) : null}
          <dt>State</dt>
          <dd>{ENV_STATE_TEXT[env.state] ?? env.state}</dd>
          {kernelState ? (
            <>
              <dt>Kernel</dt>
              <dd>{KERNEL_STATES[kernelState] ?? kernelState}</dd>
            </>
          ) : null}
        </dl>
      ) : (
        <p className="nb-panel__empty">No environment</p>
      )}
      {env ? <p className="nb-muted">{env.recorded_in_file ? "Recorded in the notebook file" : "Not recorded in the notebook file"}</p> : null}
      {shared ? null : <p className="nb-muted" data-testid="envs-not-shared">{ENVS_NOT_SHARED}</p>}
      {env?.last_failure ? (
        <p className="nb-install nb-install--error" role="alert" data-testid="env-last-failure">
          {env.last_failure} The previous build is still in use.
        </p>
      ) : null}
      {may("build") || may("cancel") ? (
        <div className="nb-panel__row">
          {may("build") ? (
            <button type="button" className="nb-chip" disabled={installing} onClick={() => onChange?.("build", [])}>
              Build
            </button>
          ) : null}
          {may("cancel") ? (
            <button type="button" className="nb-chip" onClick={() => onChange?.("cancel", [])}>
              Cancel build
            </button>
          ) : null}
        </div>
      ) : null}

      {may("install") ? (
        <form className="nb-panel__row" onSubmit={submit}>
          <label htmlFor={inputId} className="nb-visually-hidden">
            Packages to install
          </label>
          <input
            id={inputId}
            className="nb-input nb-mono"
            placeholder="polars scikit-learn==1.5"
            value={draft}
            disabled={installing}
            onChange={(event) => setDraft(event.target.value)}
          />
          <button type="submit" className="nb-chip" disabled={!canInstall}>
            {installing ? "Installing…" : "Install"}
          </button>
        </form>
      ) : null}
      {install ? (
        <p
          className={`nb-muted nb-install nb-install--${install.status}`}
          role={install.status === "error" ? "alert" : "status"}
          data-testid="install-status"
        >
          {installLine(install)}
        </p>
      ) : null}

      {requirements.length > 0 ? (
        <>
          <h3 className="nb-panel__title">Requirements ({requirements.length})</h3>
          <ul className="nb-packages" aria-label="Requirements">
            {requirements.map((requirement) => (
              <li key={requirement}>
                <span className="nb-mono">{requirement}</span>
                {may("remove") ? (
                  <button
                    type="button"
                    className="nb-chip"
                    disabled={installing}
                    aria-label={`Remove ${requirementName(requirement)}`}
                    onClick={() => onChange?.("remove", [requirementName(requirement)])}
                  >
                    Remove
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}

      <h3 className="nb-panel__title">Installed ({packages.length})</h3>
      {sorted.length === 0 ? (
        <p className="nb-panel__empty">No packages</p>
      ) : (
        <ul className="nb-packages" aria-label="Packages">
          {sorted.map((pkg) => (
            <li key={pkg.name}>
              <span className="nb-mono">{pkg.name}</span>
              {pkg.version ? <span className="nb-muted nb-mono">{pkg.version}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
