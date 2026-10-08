// Organization › Machines: the machines the org holds, who uses each, what
// state each is in and what it costs. Org admins see every machine; a team
// admin sees the ones their teams own and the ones they may use. The list
// follows each machine's state live over the event stream.

import { useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import {
  Button,
  Callout,
  Card,
  currentBrand,
  EmptyState,
  MachineCardView,
  MachineStateView,
  Pill,
  Select,
  Skeleton,
  Table,
  ToastViewport,
  machineSpecParts,
  useToasts,
} from "@alkera/ui";

import {
  useOrgComputeSettings,
  useOrgMachines,
  useUpdateOrgComputeSettings,
  type OrgMachineRead,
} from "../../../api/machines";
import { machineBilling, type MachineBuyContext } from "../../../app/extensions/portal";
import { Icon } from "../../../app/icons";
import { TopbarActions } from "../../../app/Topbar";

import { AddMachineModal } from "./AddMachineModal";
import { MachineMenu } from "./MachineActions";
import { useNow } from "./useNow";
import { COPY, errorText, machinePath, machineUseLabel, useMachineViewer, type MachineViewer } from "./model";
import styles from "./machines.module.css";

/** The Add machine button, when the server lets the reader add one. */
export function AddButton({ viewer, onAdd }: { viewer: MachineViewer; onAdd: () => void }) {
  if (!viewer.mayAdd) return null;
  return (
    <Button variant="secondary" leftSection={<Icon name="plus" size={16} />} onClick={onAdd}>
      {COPY.add}
    </Button>
  );
}

const NO_BUY = { button: null, dialog: null };

/** The registered buy entry, or none. The registry is fixed before the app renders, so the
 *  same hook runs on every render. */
function useBuyEntry(ctx: MachineBuyContext): { button: ReactNode; dialog: ReactNode } {
  const useEntry = machineBilling()?.useBuyEntry ?? useNoBuy;
  return useEntry(ctx);
}

function useNoBuy(): { button: ReactNode; dialog: ReactNode } {
  return NO_BUY;
}

function UseCell({ machine, viewer }: { machine: OrgMachineRead; viewer: MachineViewer }) {
  const { label, chips } = machineUseLabel(machine, viewer.orgPoolAllowed);
  return (
    <div className={styles.use}>
      <span>{label}</span>
      {chips.length > 0 ? (
        <span className={styles.chips}>
          {chips.map((chip) => (
            <Pill key={chip} shape="rect" variant="outline">
              {chip}
            </Pill>
          ))}
        </span>
      ) : null}
    </div>
  );
}

/** The machine new workspaces run on unless their creator picks another. Org
 *  admins only; saved the moment it changes, naming the settings version read. */
export function DefaultMachineField({
  machines,
  notify,
}: {
  machines: readonly OrgMachineRead[];
  notify: (message: string, ok: boolean) => void;
}) {
  const settings = useOrgComputeSettings();
  const update = useUpdateOrgComputeSettings();
  const current = settings.data?.default_org_machine_id ?? "";
  const change = (value: string) => {
    if (!settings.data || value === current) return;
    update.mutate(
      { version: settings.data.version, patch: { default_org_machine_id: value === "" ? null : value } },
      { onError: (error) => notify(errorText(error, "The default could not be changed."), false) },
    );
  };
  return (
    <Select
      label={COPY.defaultMachine}
      value={current}
      disabled={!settings.data || update.isPending}
      onChange={(event) => change(event.target.value)}
      rootClassName={styles.defaultMachine}
    >
      <option value="">{COPY.noDefaultMachine}</option>
      {machines.map((m) => (
        <option key={m.id} value={m.id}>
          {m.name}
        </option>
      ))}
    </Select>
  );
}

/** The machine's name and hardware in one line, for the link's tooltip. */
function machineTitle(m: OrgMachineRead): string {
  return [m.card.name, machineSpecParts({ ...m.card, spec: m.card.spec ? { ...m.card.spec, rate_per_minute_nanos: null } : null }).join(" · ")]
    .filter(Boolean)
    .join(": ");
}

export function MachinesPage() {
  const viewer = useMachineViewer();
  const machines = useOrgMachines(!viewer.loading);
  const navigate = useNavigate();
  const { toasts, push, dismiss } = useToasts();
  const notify = (message: string, ok: boolean) => push({ message, tone: ok ? "success" : "danger" });
  // One add per opening: each opening mounts a fresh dialog too.
  const [adding, setAdding] = useState(0);
  const [addOpen, setAddOpen] = useState(false);
  const rows = machines.data ?? [];
  const starting = rows.some((m) => m.card.state === "starting");
  const now = useNow(starting);
  // How machines are bought and priced, when an extension serves it.
  const billing = machineBilling();
  const buy = useBuyEntry({
    viewer,
    held: viewer.isOrgAdmin ? rows.length : null,
    onBought: (machine) => {
      notify(`Starting ${machine.name}.`, true);
      navigate(machinePath(machine.id));
    },
  });
  const moneyColumns = (billing?.listColumns ?? []).filter((column) => column.shown(rows));

  const openAdd = () => {
    setAdding((n) => n + 1);
    setAddOpen(true);
  };
  const entryPoints = (
    <>
      <AddButton viewer={viewer} onAdd={openAdd} />
      {buy.button}
    </>
  );

  const columns = ["Machine", "Use", "State", ...moneyColumns.map((column) => column.header), ""];
  // The machine and its use share what the narrow columns leave; the state is wide enough
  // for a starting step's line ("Reserving hardware, about 1 min left").
  const colWidths = [undefined, undefined, 200, ...moneyColumns.map((column) => column.width), 56];

  return (
    <div className={styles.page} data-measure-surface="product">
      <TopbarActions>{entryPoints}</TopbarActions>
      <Card title={COPY.title} icon={<Icon name="monitor" size={16} />} headingLevel={2}>
        {machines.isError ? (
          <EmptyState
            tone="alert"
            size="md"
            icon={<Icon name="alert" size={32} />}
            title="We couldn't load the machines"
            body={errorText(machines.error, "Try again in a moment.")}
            action={
              <Button variant="secondary" onClick={() => void machines.refetch()}>
                Try again
              </Button>
            }
          />
        ) : machines.isPending ? (
          <div aria-busy="true" aria-label="Loading machines" className={styles.loading}>
            <Skeleton width="100%" height={48} />
            <Skeleton width="100%" height={48} />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            size="md"
            icon={<Icon name="monitor" size={32} />}
            title={COPY.empty}
            action={entryPoints}
          />
        ) : (
          <Table
            responsive
            stackPrimary={0}
            stackActions={columns.length - 1}
            verticalAlign="top"
            columns={columns}
            colWidths={colWidths}
          >
            {rows.map((m) => (
              <tr key={m.id} data-machine={m.id}>
                <td className={styles.wrapCell}>
                  <Link to={machinePath(m.id)} className={styles.machineLink} title={machineTitle(m)}>
                    <MachineCardView card={m.card} facts="hardware" now={now} />
                  </Link>
                </td>
                <td>
                  <UseCell machine={m} viewer={viewer} />
                </td>
                <td className={styles.wrapCell}>
                  <MachineStateView card={m.card} now={now} />
                </td>
                {moneyColumns.map(({ key, Cell }) => (
                  <td key={key} className="alk-num">
                    <Cell machine={m} />
                  </td>
                ))}
                <td className={styles.menuCell}>
                  <MachineMenu machine={m} viewer={viewer} notify={notify} />
                </td>
              </tr>
            ))}
          </Table>
        )}
        {viewer.isOrgAdmin && rows.length > 0 ? <DefaultMachineField machines={rows} notify={notify} /> : null}
      </Card>
      {billing && rows.some((m) => m.use_mode === "pool") && !viewer.orgPoolAllowed ? (
        <Callout tone="warning">{billing.poolUnavailableNote}</Callout>
      ) : null}
      {buy.dialog}
      {addOpen ? (
        <AddMachineModal
          key={adding}
          open={addOpen}
          viewer={viewer}
          onClose={() => setAddOpen(false)}
          onAdded={(machine) => {
            setAddOpen(false);
            notify(`Installing ${currentBrand().productName} on ${machine.name}.`, true);
            navigate(machinePath(machine.id));
          }}
        />
      ) : null}
      <ToastViewport toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
