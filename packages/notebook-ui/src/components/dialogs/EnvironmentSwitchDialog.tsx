import { useRef } from "react";
import { Dialog } from "./Dialog";

export interface EnvironmentSwitchDialogProps {
  /** The environment being switched to, as the picker names it. */
  environment: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function EnvironmentSwitchDialog({ environment, onConfirm, onCancel }: EnvironmentSwitchDialogProps) {
  const cancel = useRef<HTMLButtonElement>(null);
  return (
    <Dialog
      title={`Switch to ${environment}?`}
      onCancel={onCancel}
      initialFocus={cancel}
      actions={
        <>
          <button ref={cancel} type="button" className="nb-button" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="nb-button nb-button--primary" onClick={onConfirm}>
            Switch
          </button>
        </>
      }
    >
      <p>Switching the environment restarts the kernel.</p>
    </Dialog>
  );
}
