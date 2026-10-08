import { useRef } from "react";
import { Dialog } from "./Dialog";

export interface RestartDialogProps {
  /** Restart, then run every cell. */
  runAll?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function RestartDialog({ runAll = false, onConfirm, onCancel }: RestartDialogProps) {
  const cancel = useRef<HTMLButtonElement>(null);
  return (
    <Dialog
      title={runAll ? "Restart and run all?" : "Restart the kernel?"}
      onCancel={onCancel}
      initialFocus={cancel}
      actions={
        <>
          <button ref={cancel} type="button" className="nb-button" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="nb-button nb-button--primary" onClick={onConfirm}>
            {runAll ? "Restart and run all" : "Restart"}
          </button>
        </>
      }
    >
      <p>Restarting clears every variable in memory.</p>
    </Dialog>
  );
}
