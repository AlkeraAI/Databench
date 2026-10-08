// Naming a new project workspace before it is made, and choosing the machine it
// runs on when the reader may use one of the org's.

import { useRef, useState, type ReactElement } from "react";

import { ConfirmDialog, MachineCardView, Select, Stack, TextInput, machineStateLabel, type MachineCardData } from "@alkera/ui";

export const NEW_WORKSPACE_TITLE = "New workspace";
export const NEW_WORKSPACE_KEY = "Create";
export const RUN_ON = "Run on";
/** What the dialog says when neither the default placement nor a machine of the reader's can run it. */
export const NOWHERE_TO_RUN = "Nothing in your organization can run a workspace right now.";

/** A machine the reader may run a workspace on. */
export interface RunOnMachine {
  id: string;
  card: MachineCardData;
}

export interface NewWorkspaceDialogProps {
  open: boolean;
  busy: boolean;
  /** The server's refusal, read out under the field. */
  error: string | null;
  onClose(): void;
  /** `machinePin` is the org machine chosen; null when the reader moved off the
   *  org's default machine to the default placement; undefined to leave it to the
   *  server, which uses the org's default machine when the reader may. */
  onCreate(title: string, machinePin: string | null | undefined): void;
  /** Machines the reader may use. With none, the dialog asks only for a name. */
  machines?: readonly RunOnMachine[];
  /** What the org's default placement is called: "Standard", or "Org machines". */
  defaultLabel?: string;
  /** The org's default machine for new workspaces, preselected when it is among `machines`. */
  orgDefaultId?: string | null;
  /** Whether the default placement serves the org's regular chats (the server's
   *  `GET /machines/current` answer). When it does not, it is not offered: one of
   *  `machines` is preselected, or the dialog says there is nowhere to run. */
  defaultAvailable?: boolean;
}

export function NewWorkspaceDialog({
  open,
  busy,
  error,
  onClose,
  onCreate,
  machines = [],
  defaultLabel = "Standard",
  orgDefaultId = null,
  defaultAvailable = true,
}: NewWorkspaceDialogProps): ReactElement {
  const [title, setTitle] = useState("");
  // null until the reader picks: the org's default machine stands until then.
  const [pin, setPin] = useState<string | null>(null);
  const field = useRef<HTMLInputElement | null>(null);
  const trimmed = title.trim();
  const offersDefault = orgDefaultId !== null && machines.some((m) => m.id === orgDefaultId);
  // Without the default placement, the org's default machine or else the first one stands.
  const fallbackId = offersDefault ? orgDefaultId : defaultAvailable ? "" : (machines[0]?.id ?? "");
  const selected = pin ?? fallbackId;
  const chosen = machines.find((m) => m.id === selected) ?? null;
  const submit = (): void => {
    if (trimmed === "" || busy) return;
    onCreate(trimmed, chosen ? chosen.id : offersDefault ? null : undefined);
  };
  return (
    <ConfirmDialog
      open={open}
      onClose={() => {
        setTitle("");
        setPin(null);
        onClose();
      }}
      onConfirm={submit}
      title={NEW_WORKSPACE_TITLE}
      confirmLabel={NEW_WORKSPACE_KEY}
      busy={busy}
      confirmDisabled={trimmed === ""}
      initialFocusRef={field}
    >
      <Stack gap={4} align="stretch">
        <TextInput
          ref={field}
          label="Name"
          value={title}
          error={error ?? undefined}
          onChange={(event) => setTitle(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              submit();
            }
          }}
        />
        {machines.length > 0 ? (
          <>
            <Select label={RUN_ON} value={selected} onChange={(event) => setPin(event.target.value)}>
              {defaultAvailable ? <option value="">{defaultLabel}</option> : null}
              {machines.map((m) => (
                <option key={m.id} value={m.id}>
                  {`${m.card.name} · ${machineStateLabel(m.card.state)}`}
                </option>
              ))}
            </Select>
            {chosen ? <MachineCardView card={chosen.card} /> : null}
          </>
        ) : defaultAvailable ? null : (
          <p className="alk-meta" role="status">
            {NOWHERE_TO_RUN}
          </p>
        )}
      </Stack>
    </ConfirmDialog>
  );
}
