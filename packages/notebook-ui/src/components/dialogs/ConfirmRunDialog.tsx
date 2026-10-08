// Asked when the engine answers a run with `needs_confirmation`: the run would
// also re-run cells the person did not ask for, and they are costly.

import { useRef } from "react";
import type { ReactNode } from "react";
import type { PlannedStep, RunConfirmation } from "../../model/types";
import { Dialog } from "./Dialog";
import { formatDuration } from "./formatDuration";

export interface ConfirmRunDialogProps {
  confirmation: RunConfirmation;
  onRun: () => void;
  onCancel: () => void;
}

function stepName(step: PlannedStep, capital: boolean): ReactNode {
  if (step.name && step.name !== "_") return <code>{step.name}</code>;
  return capital ? "An unnamed cell" : "an unnamed cell";
}

function lastTook(step: PlannedStep): string {
  return step.last_duration_ms != null ? ` (last took ${formatDuration(step.last_duration_ms)})` : "";
}

export function ConfirmRunDialog({ confirmation, onRun, onCancel }: ConfirmRunDialogProps) {
  const cancel = useRef<HTMLButtonElement>(null);
  const implicit = confirmation.plan.filter((step) => step.reason !== "target");
  let summary: ReactNode;
  if (implicit.length === 1) {
    summary = (
      <p>
        This also re-runs {stepName(implicit[0], false)}
        {lastTook(implicit[0])}.
      </p>
    );
  } else if (implicit.length > 1) {
    summary = (
      <>
        <p>This also re-runs these cells:</p>
        <ul>
          {implicit.map((step) => (
            <li key={step.cell_id}>
              {stepName(step, true)}
              {lastTook(step)}
            </li>
          ))}
        </ul>
      </>
    );
  } else {
    summary = <p>This run may be costly.</p>;
  }
  return (
    <Dialog
      title="Run these cells?"
      onCancel={onCancel}
      initialFocus={cancel}
      actions={
        <>
          <button ref={cancel} type="button" className="nb-button" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="nb-button nb-button--primary" onClick={onRun}>
            Run
          </button>
        </>
      }
    >
      {summary}
      {confirmation.estimate_s > 0 ? (
        <p className="nb-dialog__muted">Estimated time: {formatDuration(confirmation.estimate_s * 1000)}</p>
      ) : null}
    </Dialog>
  );
}
