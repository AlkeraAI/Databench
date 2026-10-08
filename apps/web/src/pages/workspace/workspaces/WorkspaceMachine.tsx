// Where a workspace runs, in its header, and moving it to another machine.
//
// The chip names the machine with its state as a dot and its specs on hover.
// Someone who may move the workspace opens "Change workspace machine" from it; anyone
// else sees the same card, read-only. While a move runs the chip says which
// step it is on, read from the workspace's machine (the move's frames refresh
// it), so every viewer of the workspace watches the same move.
//
// The read names the move under way and how the last one ended. A move this
// reader asked for that ended failed is told to them, with what went wrong,
// and offered again, until they act.

import { useEffect, useState, useSyncExternalStore, type ReactElement } from "react";

import { Link } from "react-router-dom";

import {
  Button,
  Callout,
  Checkbox,
  MachineCardView,
  Modal,
  Stack,
  machineSpecLine,
  type MachineCardData,
} from "@alkera/ui";

import { machineBilling } from "../../../app/extensions/portal";
import { ApiError, refusalSentence } from "../../../api/errors";
import { useNavGates } from "../../../app/useNavGates";
import { MACHINES_PATH } from "../../organization/machines/model";
import {
  MOVE_IN_PROGRESS,
  MOVE_TARGET_NOT_SHARED,
  moveActive,
  useCancelMove,
  useMachinePower,
  useMoveWorkspace,
  useOrgMachines,
  useWorkspaceMachine,
  type LostMachineRead,
  type MachineCard,
  type MachineUnavailableRead,
  type WorkspaceMachineMoveRead,
  type WorkspaceMachineRead,
} from "../../../api/machines";

export const CHANGE_MACHINE = "Change workspace machine";
export const MOVE_COPY =
  "Running chats stop and continue on the new machine. Files are kept. Installed packages, running processes and anything outside the workspace folder are not.";
export const STOP_RUNNING = "Stop running chats now";
export const GRACE = "Running chats get up to 2 minutes to finish";
export const MOVE_WORKSPACE = "Move workspace";
export const MANAGE_MACHINES = "Buy or manage machines";
export const MOVE_FAILED = "Move failed";

/** Which machine a card is, for comparing the current machine with a target. */
const cardId = (card: Pick<MachineCard, "kind" | "org_machine_id">): string =>
  card.org_machine_id ?? `kind:${card.kind}`;

/** The name of the machine a move is going to, from the targets the read lists. */
function targetName(
  move: Pick<WorkspaceMachineMoveRead, "to_org_machine_id">,
  targets: readonly MachineCard[],
): string {
  const target = targets.find(
    (t) => (t.org_machine_id ?? null) === (move.to_org_machine_id ?? null),
  );
  return target?.name ?? "the new machine";
}

/** What the chip says while a move runs, or null when none is. */
export function moveStatus(
  read: Pick<WorkspaceMachineRead, "active_move" | "targets">,
): string | null {
  const move = read.active_move;
  if (!move) return null;
  switch (move.state) {
    case "requested":
    case "draining":
      return "Saving chats";
    case "switching":
      return `Starting ${targetName(move, read.targets)}`;
    case "waking":
      return "Waking workspace";
    case "failed":
      return MOVE_FAILED;
    default:
      return null;
  }
}

/** A failed move in words, and whether hardware was what was missing. */
export function moveFailure(
  move: Pick<WorkspaceMachineMoveRead, "error_code" | "error" | "to_org_machine_id">,
  targets: readonly MachineCard[],
): { message: string; capacity: boolean } {
  const name = targetName(move, targets);
  switch (move.error_code) {
    case "target_capacity":
      return { message: `No hardware is free for ${name} right now.`, capacity: true };
    case "target_boot_failed":
      return { message: `${name} couldn't start.`, capacity: false };
    case "drain_timeout":
      return { message: "Running chats didn't stop in time.", capacity: false };
    case "not_allowed":
      return { message: `You can't use ${name}.`, capacity: false };
    default:
      return { message: move.error || `The workspace didn't move to ${name}.`, capacity: false };
  }
}

// ---- the moves this reader asked for ------------------------------------------

const asked = new Map<string, WorkspaceMachineMoveRead>();
const listeners = new Set<() => void>();
const emit = () => {
  for (const listener of listeners) listener();
};

/** Remember a move this reader asked for, until its outcome is read. */
export function rememberMove(workspaceId: string, move: WorkspaceMachineMoveRead): void {
  asked.set(workspaceId, move);
  emit();
}

export function forgetMove(workspaceId: string): void {
  if (asked.delete(workspaceId)) emit();
}

function useAskedMove(workspaceId: string): WorkspaceMachineMoveRead | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => asked.get(workspaceId) ?? null,
  );
}

export type MoveOutcome =
  | { kind: "running"; status: string }
  | {
      kind: "failed";
      move: WorkspaceMachineMoveRead;
      failure: { message: string; capacity: boolean };
    }
  | { kind: "done" };

/** Where a move stands: under way (the read names it), failed (the read says so,
 *  or says the move this reader asked for ended failed), done, or nothing to
 *  say. The read's `last_move` is how the move this reader asked for ended,
 *  with its error code; a server without it is read from the pin instead (a
 *  failed move puts the pin back). A move someone else asked since, or one
 *  this reader canceled, leaves nothing to say. */
export function moveOutcome(
  read: Pick<WorkspaceMachineRead, "active_move" | "targets" | "pin" | "last_move">,
  mine: WorkspaceMachineMoveRead | null,
): MoveOutcome | null {
  const active = read.active_move;
  if (active?.state === "failed")
    return { kind: "failed", move: active, failure: moveFailure(active, read.targets) };
  if (active && moveActive(active))
    return { kind: "running", status: moveStatus(read) ?? "Moving" };
  if (!mine) return null;
  const last = read.last_move ?? null;
  if (last) {
    if (last.id !== mine.id || last.state !== "failed") return { kind: "done" };
    return { kind: "failed", move: last, failure: moveFailure(last, read.targets) };
  }
  if ((read.pin ?? null) === (mine.to_org_machine_id ?? null)) return { kind: "done" };
  return { kind: "failed", move: mine, failure: moveFailure(mine, read.targets) };
}

function useMoveOutcome(
  workspaceId: string,
  read: WorkspaceMachineRead | undefined,
): MoveOutcome | null {
  const mine = useAskedMove(workspaceId);
  const outcome = read ? moveOutcome(read, mine) : null;
  const done = outcome?.kind === "done";
  useEffect(() => {
    if (done) forgetMove(workspaceId);
  }, [done, workspaceId]);
  return outcome;
}

// ---- the chip -------------------------------------------------------------------

/** The workspace's machine as a chip; nothing until the server has answered,
 *  and nothing from a server with no machine read. */
export function WorkspaceMachineChip({
  workspaceId,
  workspaceVersion,
}: {
  workspaceId: string;
  /** The workspace version the reader holds, sent with a move. */
  workspaceVersion?: number;
}): ReactElement | null {
  const read = useWorkspaceMachine(workspaceId);
  const outcome = useMoveOutcome(workspaceId, read.data);
  const [open, setOpen] = useState(false);
  // Nothing until the server has answered with a machine: an older server has no such read.
  if (!read.data?.card) return null;
  const status =
    outcome?.kind === "running"
      ? outcome.status
      : outcome?.kind === "failed"
        ? MOVE_FAILED
        : lostStatus(read.data);
  return (
    <>
      <MachineCardView
        card={read.data.card as MachineCardData}
        variant="compact"
        status={status}
        onClick={() => setOpen(true)}
        className="ws-machine-chip"
      />
      {read.data.can_move ? (
        <ChangeMachineDialog
          open={open}
          onClose={() => setOpen(false)}
          workspaceId={workspaceId}
          workspaceVersion={workspaceVersion}
          read={read.data}
        />
      ) : (
        <ReadOnlyMachine open={open} onClose={() => setOpen(false)} card={read.data.card} />
      )}
    </>
  );
}

function ReadOnlyMachine({
  open,
  onClose,
  card,
}: {
  open: boolean;
  onClose: () => void;
  card: MachineCard;
}) {
  return (
    <Modal open={open} onClose={onClose} title="Machine" size="sm" cancelLabel="Close">
      <MachineCardView card={card as MachineCardData} />
    </Modal>
  );
}

// ---- change machine ---------------------------------------------------------------

export interface ChangeMachineDialogProps {
  open: boolean;
  onClose: () => void;
  workspaceId: string;
  workspaceVersion?: number;
  read: WorkspaceMachineRead;
}

/** A refused move request in words. */
function refusalText(error: unknown): string | null {
  if (!error) return null;
  if (error instanceof ApiError) {
    if (error.code === MOVE_IN_PROGRESS) return "A move is already under way.";
    if (error.code === MOVE_TARGET_NOT_SHARED)
      return error.serverMessage ?? "Someone with a running chat here can't use that machine.";
    const explained = machineBilling()?.refusal(error.status);
    if (explained) return explained;
    if (error.status === 409) return "This workspace changed. Pick the machine again.";
    return refusalSentence(error, { fallback: "The workspace could not be moved." });
  }
  return "The workspace could not be moved.";
}

export function ChangeMachineDialog({
  open,
  onClose,
  workspaceId,
  workspaceVersion,
  read,
}: ChangeMachineDialogProps) {
  const move = useMoveWorkspace();
  const cancel = useCancelMove();
  const outcome = useMoveOutcome(workspaceId, read);
  // The Machines page is a team-admin page; the link hides with its nav entry.
  const gates = useNavGates();
  const current = cardId(read.card);
  const [target, setTarget] = useState<string | null>(null);
  const [stopRunning, setStopRunning] = useState(true);
  const active = read.active_move;
  const running = outcome?.kind === "running";
  const picked = read.targets.find((t) => cardId(t) === target) ?? null;

  const send = (to: string | null, stop: boolean) =>
    move.mutate(
      { workspaceId, toOrgMachineId: to, stopRunning: stop, workspaceVersion },
      {
        onSuccess: (requested) => {
          rememberMove(workspaceId, requested);
          onClose();
        },
      },
    );

  const refusal = refusalText(move.error);

  return (
    <Modal
      open={open}
      onClose={() => {
        move.reset();
        setTarget(null);
        onClose();
      }}
      title={CHANGE_MACHINE}
      size="md"
      confirmLabel={MOVE_WORKSPACE}
      confirmDisabled={picked === null || running}
      confirmBusy={move.isPending}
      onConfirm={() => {
        if (picked) send(picked.org_machine_id ?? null, stopRunning);
      }}
    >
      <Stack gap={4} align="stretch">
        {read.lost ? (
          <Callout tone="info" role="status">
            {lostSentence(read.lost)}{" "}
            {read.lost.fell_back ? `This workspace now runs on ${read.card.name}.` : null}
          </Callout>
        ) : null}
        {outcome?.kind === "failed" ? (
          <MoveFailure
            failure={outcome.failure}
            move={outcome.move}
            onRetry={() => send(outcome.move.to_org_machine_id ?? null, stopRunning)}
            onChooseAnother={() => {
              forgetMove(workspaceId);
              setTarget(null);
            }}
            busy={move.isPending}
          />
        ) : null}
        {running && active ? (
          <Callout tone="info" role="status">
            {outcome.status}.{" "}
            {active.state === "requested" || active.state === "draining" ? (
              <Button
                variant="secondary"
                fill="ghost"
                size="sm"
                loading={cancel.isPending}
                onClick={() => {
                  forgetMove(workspaceId);
                  cancel.mutate({ workspaceId, moveId: active.id });
                }}
              >
                Cancel move
              </Button>
            ) : null}
          </Callout>
        ) : null}
        <MachinePicker
          targets={read.targets}
          current={current}
          selected={target}
          disabled={running}
          onSelect={setTarget}
        />
        {gates.teamAdmin ? (
          <Link className="alk-link" to={MACHINES_PATH} onClick={onClose}>
            {MANAGE_MACHINES}
          </Link>
        ) : null}
        <p className="alk-meta">{MOVE_COPY}</p>
        <Checkbox
          label={STOP_RUNNING}
          checked={stopRunning}
          onChange={(e) => setStopRunning(e.target.checked)}
        />
        {stopRunning ? null : <p className="alk-meta">{GRACE}</p>}
        {refusal ? (
          <Callout tone="danger" role="alert">
            {refusal}
          </Callout>
        ) : null}
      </Stack>
    </Modal>
  );
}

/** A failed move: what went wrong, and the ways on. When the target had no
 *  hardware, one of its managers may start it on new hardware; anyone else
 *  picks another machine. */
function MoveFailure({
  failure,
  move,
  onRetry,
  onChooseAnother,
  busy,
}: {
  failure: { message: string; capacity: boolean };
  move: WorkspaceMachineMoveRead;
  onRetry: () => void;
  onChooseAnother: () => void;
  busy: boolean;
}) {
  const machines = useOrgMachines(failure.capacity && move.to_org_machine_id != null);
  const power = useMachinePower();
  const target = machines.data?.find((m) => m.id === move.to_org_machine_id);
  return (
    <Callout tone="danger" role="alert">
      <Stack gap={2} align="stretch">
        <span>{failure.message}</span>
        <span className="ws-machine-actions">
          <Button size="sm" variant="secondary" onClick={onRetry} loading={busy}>
            Retry
          </Button>
          {failure.capacity && target?.can_manage ? (
            <Button
              size="sm"
              variant="secondary"
              loading={power.isPending}
              onClick={() =>
                power.mutate({ machineId: target.id, version: target.version, action: "replace" })
              }
            >
              Start on new hardware
            </Button>
          ) : failure.capacity ? (
            <Button size="sm" variant="secondary" fill="ghost" onClick={onChooseAnother}>
              Choose another machine
            </Button>
          ) : null}
        </span>
      </Stack>
    </Callout>
  );
}

// ---- the machine picker -------------------------------------------------------------

/** Which machine a card is, as the picker keys it. */
export const machineKey = cardId;

/** The machines a workspace may move to, one radio each. `current` is the one
 *  it runs on, which cannot be picked. */
export function MachinePicker({
  targets,
  current,
  selected,
  disabled = false,
  onSelect,
}: {
  targets: readonly MachineCard[];
  current: string | null;
  selected: string | null;
  disabled?: boolean;
  onSelect: (key: string) => void;
}): ReactElement {
  return (
    <div role="radiogroup" aria-label="Machines" className="ws-machine-targets">
      {targets.map((card) => {
        const id = cardId(card);
        const isCurrent = id === current;
        return (
          <button
            key={id}
            type="button"
            role="radio"
            aria-checked={selected === id}
            aria-disabled={isCurrent || disabled || undefined}
            aria-label={
              isCurrent ? `${card.name}, current machine` : machineSpecLine(card as MachineCardData)
            }
            className="ws-machine-target"
            data-selected={selected === id || undefined}
            onClick={() => {
              if (!isCurrent && !disabled) onSelect(id);
            }}
          >
            <MachineCardView card={card as MachineCardData} />
            {isCurrent ? <span className="alk-meta">Current</span> : null}
          </button>
        );
      })}
    </div>
  );
}

// ---- a machine the workspace lost ------------------------------------------------------

/** What happened to the machine a workspace lost, in a sentence. */
export function lostSentence(lost: Pick<LostMachineRead, "name" | "reason">): string {
  const name = lost.name || "The machine this workspace used";
  return lost.reason === "deleted" ? `${name} was deleted.` : `You can no longer use ${name}.`;
}

/** The chip's status for a lost machine: why it waits, or where it went. */
export function lostStatus(read: Pick<WorkspaceMachineRead, "lost" | "card">): string | undefined {
  const lost = read.lost;
  if (!lost) return undefined;
  if (lost.fell_back) return `Moved to ${read.card.name}`;
  return lost.reason === "deleted" ? "Machine deleted" : "Machine unavailable";
}

export const WAKE_ON = "Wake workspace";
export const PICK_MACHINE = "Pick a machine";

/** The prompt shown when opening a workspace whose machine is gone. The server
 *  holds the wake and lists where this reader may wake it; the default
 *  placement is preselected when the server offers it. Picking moves the
 *  workspace there, then asks for the wake again. */
export function WakeMachinePrompt({
  held,
  onWake,
}: {
  held: MachineUnavailableRead | null;
  /** Ask for the wake again, once the move is under way. */
  onWake: () => void;
}): ReactElement | null {
  const move = useMoveWorkspace();
  const [closed, setClosed] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  if (!held || closed === held.workspace_id) return null;
  const preselected = held.preselect_default
    ? (held.choices.find((c) => c.org_machine_id == null) ?? null)
    : null;
  const selected = picked ?? (preselected ? cardId(preselected) : null);
  const target = held.choices.find((c) => cardId(c) === selected) ?? null;
  const close = () => setClosed(held.workspace_id);
  const lead = lostSentence(held.lost);
  const ask =
    held.choices.length === 0
      ? "Ask someone with full access to this workspace to pick a machine."
      : preselected
        ? `Wake on ${preselected.name}, or pick a machine.`
        : "Pick a machine to wake it on.";
  const refusal = refusalText(move.error);
  return (
    <Modal
      open
      onClose={close}
      title={PICK_MACHINE}
      size="md"
      cancelLabel={held.choices.length === 0 ? "Close" : undefined}
      confirmLabel={held.choices.length === 0 ? undefined : WAKE_ON}
      confirmDisabled={target === null}
      confirmBusy={move.isPending}
      onConfirm={() => {
        if (!target) return;
        move.mutate(
          {
            workspaceId: held.workspace_id,
            toOrgMachineId: target.org_machine_id ?? null,
            stopRunning: false,
          },
          {
            onSuccess: (requested) => {
              rememberMove(held.workspace_id, requested);
              close();
              onWake();
            },
          },
        );
      }}
    >
      <Stack gap={4} align="stretch">
        <p>
          {lead} {ask}
        </p>
        {held.choices.length > 0 ? (
          <MachinePicker
            targets={held.choices}
            current={null}
            selected={selected}
            onSelect={setPicked}
          />
        ) : null}
        {refusal ? (
          <Callout tone="danger" role="alert">
            {refusal}
          </Callout>
        ) : null}
      </Stack>
    </Modal>
  );
}
