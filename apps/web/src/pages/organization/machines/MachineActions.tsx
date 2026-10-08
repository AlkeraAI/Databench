// What a manager can do to one machine, from its row on the Machines page or
// from its own page: start, stop, replace, rename, change its settings, change
// who may use it, delete it. One module, so a stop or a delete reads and behaves
// the same from either place.
//
// Every write names the version the reader saw. A machine another admin changed
// in the meantime answers 409: the dialog closes, the reads refresh, and the
// reader sees the current settings rather than having overwritten them.

import { Fragment, useState } from "react";

import {
  Checkbox,
  ConfirmDialog,
  Dropdown,
  DropdownDivider,
  DropdownItem,
  Modal,
  SegmentedControl,
  Select,
  Stack,
  TextInput,
} from "@alkera/ui";

import {
  useDeleteMachine,
  useMachinePower,
  useSetMachineAudience,
  useUpdateMachine,
  type MachinePowerAction,
  type OrgMachineRead,
  type OrgMachineUpdate,
} from "../../../api/machines";
import { machineBilling } from "../../../app/extensions/portal";
import { Icon, type IconName } from "../../../app/icons";

import { AudiencePicker, toGrants, type AudienceDraft } from "./AudiencePicker";
import { GROW_COPY, GrowDiskDialog } from "./GrowDiskDialog";
import { COPY, errorText, idleOptionsWith, isConflict, type MachineViewer } from "./model";

export type Notify = (message: string, ok: boolean) => void;

/** The dialogs a machine's actions open. */
export type MachineDialog = "rename" | "settings" | "audience" | "stop" | "delete" | "replace" | "disk" | null;

/** States from which a start is asked for, and from which a stop is. */
const STARTABLE: ReadonlySet<string> = new Set(["stopped", "failed"]);
const STOPPABLE: ReadonlySet<string> = new Set(["running", "unhealthy", "starting", "unreachable", "waiting_for_hardware"]);
/** States from which new hardware may be asked for: the machine is not on any. */
export const REPLACEABLE: ReadonlySet<string> = new Set(["failed", "waiting_for_hardware", "stopped"]);

export const STOP_GRACE = "Running chats get up to 2 minutes to finish. Its disk is kept.";
export const STOP_NOW = "Stop running chats now";
export const deleteConsequence = (name: string): string =>
  `Deleting ${name} destroys its disk. Files in your workspaces are kept.`;
export const replaceConsequence = (name: string): string =>
  `Start ${name} on new hardware? Its disk is not carried over. Files in your workspaces are kept.`;

const POWER_DONE: Record<MachinePowerAction, (name: string) => string> = {
  start: (name) => `Starting ${name}.`,
  stop: (name) => `Stopping ${name}.`,
  replace: (name) => `Starting ${name} on new hardware.`,
};

/** `onDeleted` runs once a delete is accepted: the machine's own page leaves for the list. */
export function useMachineActions(machine: OrgMachineRead, viewer: MachineViewer, notify: Notify, onDeleted?: () => void) {
  const [dialog, setDialog] = useState<MachineDialog>(null);
  const power = useMachinePower();
  const close = () => setDialog(null);

  const fail = (error: unknown, fallback: string) => {
    close();
    notify(isConflict(error) ? COPY.conflict : errorText(error, fallback), false);
  };

  const runPower = (action: MachinePowerAction, now?: boolean) =>
    power.mutate(
      { machineId: machine.id, version: machine.version, action, now },
      {
        onSuccess: () => {
          close();
          notify(POWER_DONE[action](machine.name), true);
        },
        onError: (e) => fail(e, `Could not ${action} ${machine.name}.`),
      },
    );

  const state = machine.card.state;
  const items: { key: string; label: string; icon: IconName; danger?: boolean; separated?: boolean; onSelect: () => void }[] = [];
  if (machine.can_manage) {
    if (STARTABLE.has(state)) items.push({ key: "start", label: "Start", icon: "resume", onSelect: () => runPower("start") });
    if (STOPPABLE.has(state)) items.push({ key: "stop", label: "Stop", icon: "stop", onSelect: () => setDialog("stop") });
    items.push(
      { key: "rename", label: "Rename", icon: "pencil", separated: items.length > 0, onSelect: () => setDialog("rename") },
      { key: "settings", label: "Settings", icon: "settings", onSelect: () => setDialog("settings") },
      { key: "audience", label: "Who can use it", icon: "users", onSelect: () => setDialog("audience") },
    );
    if (machine.disk_grow) items.push({ key: "disk", label: GROW_COPY.confirm, icon: "drive", onSelect: () => setDialog("disk") });
    items.push(
      { key: "delete", label: "Delete", icon: "trash", danger: true, separated: true, onSelect: () => setDialog("delete") },
    );
  }

  const dialogs = (
    <>
      <RenameDialog open={dialog === "rename"} machine={machine} onClose={close} notify={notify} />
      <SettingsDialog open={dialog === "settings"} machine={machine} viewer={viewer} onClose={close} notify={notify} />
      <AudienceDialog open={dialog === "audience"} machine={machine} viewer={viewer} onClose={close} notify={notify} />
      <StopDialog
        open={dialog === "stop"}
        name={machine.name}
        busy={power.isPending}
        onClose={close}
        onStop={(now) => runPower("stop", now)}
      />
      <ConfirmDialog
        open={dialog === "replace"}
        title={`Replace ${machine.name}`}
        consequence={replaceConsequence(machine.name)}
        confirmLabel="Start on new hardware"
        tone="warning"
        busy={power.isPending}
        onConfirm={() => runPower("replace")}
        onClose={close}
      />
      <GrowDiskDialog open={dialog === "disk"} machine={machine} onClose={close} notify={notify} />
      <DeleteDialog open={dialog === "delete"} machine={machine} onClose={close} notify={notify} onDeleted={onDeleted} />
    </>
  );

  return {
    items,
    dialogs,
    open: setDialog,
    start: () => runPower("start"),
    busy: power.isPending,
  };
}

/** The row's menu over {@link useMachineActions}' items; nothing for a reader
 *  who manages nothing on the machine. */
export function MachineMenu({
  machine,
  viewer,
  notify,
  variant = "row",
  onDeleted,
}: {
  machine: OrgMachineRead;
  viewer: MachineViewer;
  notify: Notify;
  variant?: "row" | "header";
  onDeleted?: () => void;
}) {
  const { items, dialogs } = useMachineActions(machine, viewer, notify, onDeleted);
  if (items.length === 0) return null;
  const label = `Actions for ${machine.name}`;
  return (
    <>
      <Dropdown
        label={label}
        align="end"
        trigger={{
          kind: "icon",
          icon: <Icon name="dotsV" size={18} />,
          ariaLabel: label,
          variant: "secondary",
          fill: variant === "header" ? "filled" : "ghost",
          size: variant === "header" ? "lg" : "md",
        }}
      >
        {items.map((item) => (
          <Fragment key={item.key}>
            {item.separated ? <DropdownDivider /> : null}
            <DropdownItem icon={<Icon name={item.icon} size={15} />} danger={item.danger} onSelect={item.onSelect}>
              {item.label}
            </DropdownItem>
          </Fragment>
        ))}
      </Dropdown>
      {dialogs}
    </>
  );
}

function StopDialog({
  open,
  name,
  busy,
  onClose,
  onStop,
}: {
  open: boolean;
  name: string;
  busy: boolean;
  onClose: () => void;
  onStop: (now: boolean) => void;
}) {
  const [now, setNow] = useState(false);
  return (
    <ConfirmDialog
      open={open}
      title={`Stop ${name}?`}
      consequence={now ? "Running chats stop now. Its disk is kept." : STOP_GRACE}
      confirmLabel="Stop"
      tone="warning"
      busy={busy}
      onConfirm={() => onStop(now)}
      onClose={() => {
        setNow(false);
        onClose();
      }}
    >
      <Checkbox label={STOP_NOW} checked={now} onChange={(e) => setNow(e.target.checked)} />
    </ConfirmDialog>
  );
}

function RenameDialog({ open, machine, onClose, notify }: { open: boolean; machine: OrgMachineRead; onClose: () => void; notify: Notify }) {
  const update = useUpdateMachine();
  const [name, setName] = useState(machine.name);
  const trimmed = name.trim();
  const save = () =>
    update.mutate(
      { machineId: machine.id, version: machine.version, patch: { name: trimmed } },
      {
        onSuccess: () => {
          onClose();
          notify(`Renamed to ${trimmed}.`, true);
        },
        onError: (e) => {
          if (isConflict(e) && e.code === "name_taken") return;
          onClose();
          notify(isConflict(e) ? COPY.conflict : errorText(e, "Could not rename the machine."), false);
        },
      },
    );
  const nameTaken = update.error && isConflict(update.error) && update.error.code === "name_taken";
  return (
    <Modal
      open={open}
      onClose={() => {
        setName(machine.name);
        update.reset();
        onClose();
      }}
      title={`Rename ${machine.name}`}
      size="sm"
      confirmLabel="Rename"
      onConfirm={save}
      confirmBusy={update.isPending}
      confirmDisabled={trimmed === "" || trimmed === machine.name || trimmed.length > 64}
    >
      <TextInput
        label="Name"
        value={name}
        maxLength={64}
        error={nameTaken ? "Another machine already has this name." : undefined}
        onChange={(e) => setName(e.target.value)}
      />
    </Modal>
  );
}

/** The settings form's fields as typed. `cap` is the billing extension's spend cap,
 *  empty where none is installed. */
interface SettingsDraft {
  idle: string;
  cap: string;
  useMode: OrgMachineRead["use_mode"];
}

export const seedSettings = (m: OrgMachineRead): SettingsDraft => ({
  idle: m.idle_stop_minutes == null ? "" : String(m.idle_stop_minutes),
  cap: machineBilling()?.cap.draft(m) ?? "",
  useMode: m.use_mode,
});

/** Only the fields that changed, so a save never rewrites one another admin
 *  changed meanwhile. */
export function settingsPatch(machine: OrgMachineRead, draft: SettingsDraft): OrgMachineUpdate | null {
  const patch: OrgMachineUpdate = {};
  const idle = draft.idle === "" ? null : Number(draft.idle);
  if (idle !== (machine.idle_stop_minutes ?? null)) patch.idle_stop_minutes = idle;
  const billing = machineBilling();
  if (billing) Object.assign(patch, billing.cap.patch(machine, draft.cap));
  if (draft.useMode !== machine.use_mode) patch.use_mode = draft.useMode;
  return Object.keys(patch).length === 0 ? null : patch;
}

/** Whether the reader may put a machine in the org pool: an org admin of an
 *  org that may run one. Moving one OUT of the pool is open to any manager. */
export const mayChoosePool = (viewer: MachineViewer): boolean => viewer.isOrgAdmin && viewer.orgPoolAllowed;

export function SettingsForm({
  machine,
  viewer,
  draft,
  onChange,
  disabled,
}: {
  machine: OrgMachineRead;
  viewer: MachineViewer;
  draft: SettingsDraft;
  onChange: (next: SettingsDraft) => void;
  disabled?: boolean;
}) {
  const billing = machineBilling();
  return (
    <Stack gap={4} align="stretch">
      <Select
        label="Stop when idle"
        value={draft.idle}
        disabled={disabled}
        onChange={(e) => onChange({ ...draft, idle: e.target.value })}
      >
        {idleOptionsWith(machine.idle_stop_minutes).map((o) => (
          <option key={o.label} value={o.value == null ? "" : String(o.value)}>
            {o.label}
          </option>
        ))}
      </Select>
      {billing ? <billing.cap.Field value={draft.cap} disabled={disabled} onChange={(cap) => onChange({ ...draft, cap })} /> : null}
      {mayChoosePool(viewer) ? (
        <SegmentedControl
          label="Use"
          options={[
            { key: "assigned", label: "Assigned" },
            { key: "pool", label: COPY.orgPool },
          ]}
          value={draft.useMode}
          onChange={(key) => onChange({ ...draft, useMode: key as SettingsDraft["useMode"] })}
          semantics="radio"
        />
      ) : machine.use_mode === "pool" ? (
        // A pool machine the org may no longer run as one, or one a team
        // admin manages: the way out is open.
        <Checkbox
          label="Switch to Assigned"
          checked={draft.useMode === "assigned"}
          disabled={disabled}
          onChange={(e) => onChange({ ...draft, useMode: e.target.checked ? "assigned" : "pool" })}
        />
      ) : null}
    </Stack>
  );
}

function SettingsDialog({
  open,
  machine,
  viewer,
  onClose,
  notify,
}: {
  open: boolean;
  machine: OrgMachineRead;
  viewer: MachineViewer;
  onClose: () => void;
  notify: Notify;
}) {
  const update = useUpdateMachine();
  const billing = machineBilling();
  const [draft, setDraft] = useState(() => seedSettings(machine));
  const patch = settingsPatch(machine, draft);
  const reset = () => setDraft(seedSettings(machine));
  return (
    <Modal
      open={open}
      onClose={() => {
        reset();
        onClose();
      }}
      title={`${machine.name} settings`}
      size="md"
      confirmLabel="Save"
      confirmDisabled={patch === null}
      confirmBusy={update.isPending}
      onConfirm={() => {
        if (!patch) return;
        update.mutate(
          { machineId: machine.id, version: machine.version, patch },
          {
            onSuccess: () => {
              onClose();
              notify(`Saved ${machine.name}'s settings.`, true);
            },
            onError: (e) => {
              reset();
              onClose();
              notify(isConflict(e) ? COPY.conflict : errorText(e, "Could not save the settings."), false);
            },
          },
        );
      }}
    >
      <SettingsForm machine={machine} viewer={viewer} draft={draft} onChange={setDraft} disabled={update.isPending} />
      {billing ? <billing.cap.Current machine={machine} /> : null}
    </Modal>
  );
}

export const draftsOf = (machine: OrgMachineRead): AudienceDraft[] =>
  machine.audience.map((entry) => ({ ...entry }));

function AudienceDialog({
  open,
  machine,
  viewer,
  onClose,
  notify,
}: {
  open: boolean;
  machine: OrgMachineRead;
  viewer: MachineViewer;
  onClose: () => void;
  notify: Notify;
}) {
  const save = useSetMachineAudience();
  const [drafts, setDrafts] = useState(() => draftsOf(machine));
  return (
    <Modal
      open={open}
      onClose={() => {
        setDrafts(draftsOf(machine));
        onClose();
      }}
      title={`Who can use ${machine.name}`}
      size="md"
      confirmLabel="Save"
      confirmBusy={save.isPending}
      onConfirm={() =>
        save.mutate(
          { machineId: machine.id, version: machine.version, audience: toGrants(drafts) },
          {
            onSuccess: () => {
              onClose();
              notify(`Saved who can use ${machine.name}.`, true);
            },
            onError: (e) => {
              setDrafts(draftsOf(machine));
              onClose();
              notify(isConflict(e) ? COPY.conflict : errorText(e, "Could not save who can use it."), false);
            },
          },
        )
      }
    >
      <AudiencePicker value={drafts} onChange={setDrafts} viewer={viewer} disabled={save.isPending} />
    </Modal>
  );
}

function DeleteDialog({
  open,
  machine,
  onClose,
  notify,
  onDeleted,
}: {
  open: boolean;
  machine: OrgMachineRead;
  onClose: () => void;
  notify: Notify;
  onDeleted?: () => void;
}) {
  const remove = useDeleteMachine();
  return (
    <ConfirmDialog
      open={open}
      title={`Delete ${machine.name}?`}
      consequence={deleteConsequence(machine.name)}
      confirmLabel="Delete"
      tone="destructive"
      requireTyped={machine.name}
      busy={remove.isPending}
      // The promise, not mutate's callbacks: the refetch after a delete can unmount
      // this dialog's page section first, and mutate drops callbacks of an unmounted caller.
      onConfirm={() =>
        void remove.mutateAsync({ machineId: machine.id, version: machine.version }).then(
          () => {
            onClose();
            notify(`Deleting ${machine.name}.`, true);
            onDeleted?.();
          },
          (e: unknown) => {
            onClose();
            notify(isConflict(e) ? COPY.conflict : errorText(e, "Could not delete the machine."), false);
          },
        )
      }
      onClose={onClose}
    />
  );
}
