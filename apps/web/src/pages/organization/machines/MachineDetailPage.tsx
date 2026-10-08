// One machine's page: what it is and what it is doing, its settings, who may
// use it, the workspaces on it, what it has cost this month and what happened
// to it. The settings and audience editors are inline, one open at a time, and
// step aside with the current figures when another admin changed the machine
// first.

import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  Button,
  Callout,
  Card,
  DescList,
  DescRow,
  EmptyState,
  Inline,
  MachineCardView,
  shownMachineState,
  waitLine,
  Skeleton,
  Stack,
  ToastViewport,
  useToasts,
} from "@alkera/ui";

import {
  useSetMachineAudience,
  useUpdateMachine,
  useOrgMachine,
  type OrgMachineDetail,
} from "../../../api/machines";
import { ApiError } from "../../../api/errors";
import { Icon } from "../../../app/icons";
import { useSpecificTitle } from "../../../app/documentTitle";
import { formatDateTime } from "@/lib/format/date";
import { MACHINE_DETAIL_CARDS, machineBilling } from "../../../app/extensions/portal";

import { AudiencePicker, toGrants, type AudienceDraft } from "./AudiencePicker";
import { MachineMenu, REPLACEABLE, SettingsForm, draftsOf, seedSettings, settingsPatch, useMachineActions, type Notify } from "./MachineActions";
import { GROW_COPY } from "./GrowDiskDialog";
import {
  COPY,
  MACHINES_PATH,
  errorText,
  hostKeyText,
  idleLabel,
  isConflict,
  machineUseLabel,
  useMachineViewer,
  type MachineViewer,
} from "./model";
import { useNow } from "./useNow";
import styles from "./machines.module.css";

export const WAIT_COPY = {
  cancel: "Cancel",
} as const;

export const diskFullLine = (name: string): string => `${name} is almost out of disk.`;

function Header({
  machine,
  viewer,
  notify,
  now,
  onDeleted,
}: {
  machine: OrgMachineDetail;
  viewer: MachineViewer;
  notify: Notify;
  now: number;
  onDeleted: () => void;
}) {
  const actions = useMachineActions(machine, viewer, notify, onDeleted);
  const billing = machineBilling();
  const state = machine.card.state;
  return (
    <Card headingLevel={2} className={styles.header}>
      <div className={styles.headerRow}>
        <MachineCardView card={machine.card} now={now} />
        {machine.can_manage ? (
          <Inline gap={2} wrap={false}>
            {state === "stopped" || state === "failed" ? (
              <Button loading={actions.busy} onClick={actions.start}>
                {state === "failed" ? "Try again" : "Start"}
              </Button>
            ) : null}
            {/* A machine that cannot run chats carries the server's fault. */}
            {state === "running" || machine.card.fault || state === "starting" || state === "unreachable" ? (
              <Button variant="secondary" onClick={() => actions.open("stop")}>
                Stop
              </Button>
            ) : null}
            {machine.can_replace && REPLACEABLE.has(state) ? (
              <Button variant="secondary" fill="outline" onClick={() => actions.open("replace")}>
                Start on new hardware
              </Button>
            ) : null}
            {state === "waiting_for_hardware" ? (
              <>
                {billing ? <billing.WaitingAction machine={machine} /> : null}
                <Button variant="secondary" onClick={() => actions.open("delete")}>
                  {WAIT_COPY.cancel}
                </Button>
              </>
            ) : null}
            {machine.disk_grow && shownMachineState(machine.card) === "disk_full" ? (
              <Button onClick={() => actions.open("disk")}>{GROW_COPY.confirm}</Button>
            ) : null}
            <MachineMenu machine={machine} viewer={viewer} notify={notify} variant="header" onDeleted={onDeleted} />
          </Inline>
        ) : null}
      </div>
      {actions.dialogs}
    </Card>
  );
}

function Facts({ machine, viewer }: { machine: OrgMachineDetail; viewer: MachineViewer }) {
  const billing = machineBilling();
  const use = machineUseLabel(machine, viewer.orgPoolAllowed);
  return (
    <DescList>
      <DescRow label="Use">{use.chips.length > 0 ? `${use.label} ${use.chips.join(", ")}` : use.label}</DescRow>
      <DescRow label="Owned by">{machine.owner_team_name}</DescRow>
      {billing ? <billing.PaymentFacts machine={machine} /> : null}
      {machine.ssh ? (
        <>
          <DescRow label="Address">{`${machine.ssh.username}@${machine.ssh.host}:${machine.ssh.port}`}</DescRow>
          <DescRow label="Sign in with">{machine.ssh.auth_kind === "private_key" ? "Private key" : "Password"}</DescRow>
          <DescRow label="Host key">
            <code>{hostKeyText(machine.ssh.host_key_type, machine.ssh.host_key_fingerprint)}</code>
          </DescRow>
        </>
      ) : null}
      {billing ? <billing.SpendFacts machine={machine} /> : null}
    </DescList>
  );
}

function SettingsCard({ machine, viewer, notify }: { machine: OrgMachineDetail; viewer: MachineViewer; notify: Notify }) {
  const billing = machineBilling();
  const update = useUpdateMachine();
  const [draft, setDraft] = useState<Parameters<typeof settingsPatch>[1] | null>(null);
  const [stepped, setStepped] = useState(false);
  const edit = () => {
    setStepped(false);
    setDraft(seedSettings(machine));
  };
  const patch = draft ? settingsPatch(machine, draft) : null;
  const save = () => {
    if (!patch) return;
    update.mutate(
      { machineId: machine.id, version: machine.version, patch },
      {
        onSuccess: () => {
          setDraft(null);
          notify("Saved.", true);
        },
        onError: (e) => {
          setDraft(null);
          if (isConflict(e)) setStepped(true);
          else notify(errorText(e, "Could not save the settings."), false);
        },
      },
    );
  };
  return (
    <Card
      title="Settings"
      headingLevel={3}
      actions={
        machine.can_manage && !draft ? (
          <Button variant="secondary" fill="outline" size="md" onClick={edit}>
            Edit
          </Button>
        ) : null
      }
    >
      {stepped ? <p className="alk-meta" role="status">{COPY.conflict}</p> : null}
      {draft ? (
        <Stack gap={4} align="stretch">
          <SettingsForm machine={machine} viewer={viewer} draft={draft} onChange={setDraft} disabled={update.isPending} />
          <Inline gap={3}>
            <Button onClick={save} disabled={!patch} loading={update.isPending}>
              Save
            </Button>
            <Button variant="secondary" fill="ghost" onClick={() => setDraft(null)} disabled={update.isPending}>
              Cancel
            </Button>
          </Inline>
        </Stack>
      ) : (
        <DescList>
          <DescRow label="Stop when idle">{idleLabel(machine.idle_stop_minutes)}</DescRow>
          {billing ? <billing.cap.Reading machine={machine} /> : null}
          <DescRow label="Use">{machine.use_mode === "pool" ? COPY.orgPool : "Assigned"}</DescRow>
        </DescList>
      )}
    </Card>
  );
}

function AudienceCard({ machine, viewer, notify }: { machine: OrgMachineDetail; viewer: MachineViewer; notify: Notify }) {
  const save = useSetMachineAudience();
  const [drafts, setDrafts] = useState<AudienceDraft[] | null>(null);
  const [stepped, setStepped] = useState(false);
  if (machine.use_mode === "pool") {
    return (
      <Card title="Who can use it" headingLevel={3}>
        <p>{COPY.everyone}</p>
      </Card>
    );
  }
  const submit = () => {
    if (!drafts) return;
    save.mutate(
      { machineId: machine.id, version: machine.version, audience: toGrants(drafts) },
      {
        onSuccess: () => {
          setDrafts(null);
          notify("Saved.", true);
        },
        onError: (e) => {
          // Another admin changed the machine: the refreshed list is shown in place of the edit.
          setDrafts(null);
          if (isConflict(e)) setStepped(true);
          else notify(errorText(e, "Could not save who can use it."), false);
        },
      },
    );
  };
  return (
    <Card
      title="Who can use it"
      headingLevel={3}
      actions={
        machine.can_manage && !drafts ? (
          <Button
            variant="secondary"
            fill="outline"
            size="md"
            onClick={() => {
              setStepped(false);
              setDrafts(draftsOf(machine));
            }}
          >
            Edit
          </Button>
        ) : null
      }
    >
      {stepped ? <p className="alk-meta" role="status">{COPY.conflict}</p> : null}
      {drafts ? (
        <Stack gap={4} align="stretch">
          <AudiencePicker value={drafts} onChange={setDrafts} viewer={viewer} disabled={save.isPending} />
          <Inline gap={3}>
            <Button onClick={submit} loading={save.isPending}>
              Save
            </Button>
            <Button variant="secondary" fill="ghost" onClick={() => setDrafts(null)} disabled={save.isPending}>
              Cancel
            </Button>
          </Inline>
        </Stack>
      ) : machine.audience.length === 0 ? (
        <p className="alk-meta">{COPY.noAudience}</p>
      ) : (
        <ul className={styles.list} aria-label="Who can use it">
          {machine.audience.map((entry) => (
            <li key={`${entry.kind}:${entry.team_id ?? ""}:${entry.user_id ?? ""}`}>{entry.label}</li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export function MachineDetailPage() {
  const { machineId } = useParams<{ machineId: string }>();
  const viewer = useMachineViewer();
  const machine = useOrgMachine(machineId);
  const navigate = useNavigate();
  const [cards] = useState(() => MACHINE_DETAIL_CARDS.items());
  const { toasts, push, dismiss } = useToasts();
  const notify: Notify = (message, ok) => push({ message, tone: ok ? "success" : "danger" });
  const data = machine.data;
  const now = useNow(data?.card.state === "starting");
  useSpecificTitle(data?.name ?? null);

  if (machine.isError) {
    const gone = machine.error instanceof ApiError && machine.error.status === 404;
    return (
      <div className={styles.page}>
        <EmptyState
          tone={gone ? "neutral" : "alert"}
          icon={<Icon name={gone ? "monitor" : "alert"} size={40} />}
          title={gone ? "This machine doesn't exist" : "We couldn't load this machine"}
          body={gone ? undefined : errorText(machine.error, "Try again in a moment.")}
          action={
            <Link className="alk-link" to={MACHINES_PATH}>
              All machines
            </Link>
          }
        />
      </div>
    );
  }
  if (!data) {
    return (
      <div className={styles.page} aria-busy="true" aria-label="Loading machine">
        <Skeleton width="100%" height={96} />
        <Skeleton width="100%" height={160} />
      </div>
    );
  }

  const billing = machineBilling();
  return (
    <div className={styles.page} data-measure-surface="product">
      <Link className="alk-link" to={MACHINES_PATH}>
        All machines
      </Link>
      <Header machine={data} viewer={viewer} notify={notify} now={now} onDeleted={() => navigate(MACHINES_PATH)} />
      {billing ? <billing.FundingNotice machine={data} viewer={viewer} /> : null}
      {/* The server sends a wait only for a machine waiting for hardware, and a
          fault only for one that cannot run chats. */}
      {data.card.wait ? (
        <Callout tone="warning" role="status">
          {waitLine(data.card.wait)}
        </Callout>
      ) : null}
      {shownMachineState(data.card) === "disk_full" ? (
        <Callout tone="warning" role="status">
          {diskFullLine(data.name)}
        </Callout>
      ) : null}
      {data.card.fault ? (
        <Callout tone="danger" role="status">
          {`${data.name} can't run chats.`} {data.card.fault.message}
        </Callout>
      ) : null}
      {billing ? <billing.ProviderNotice machine={data} /> : null}
      <Card headingLevel={3} title="About">
        <Facts machine={data} viewer={viewer} />
      </Card>
      <div className={styles.columns}>
        <SettingsCard machine={data} viewer={viewer} notify={notify} />
        <AudienceCard machine={data} viewer={viewer} notify={notify} />
      </div>
      <Card title="Workspaces on this machine" headingLevel={3}>
        {data.workspaces.length === 0 ? (
          <p className="alk-meta">No workspace runs here yet. Move one here from its header.</p>
        ) : (
          <ul className={styles.list}>
            {data.workspaces.map((w) => (
              <li key={w.id}>
                <Link className="alk-link" to={`/workspaces/${encodeURIComponent(w.id)}`}>
                  {w.name}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
      {cards.map(({ key, Card: Extra }) => (
        <Extra key={key} machine={data} now={now} />
      ))}
      <Card title="Timeline" headingLevel={3}>
        {data.timeline.length === 0 ? (
          <p className="alk-meta">Nothing yet.</p>
        ) : (
          <ol className={styles.timeline}>
            {data.timeline.map((entry, i) => (
              <li key={`${entry.at}-${i}`}>
                <span className="alk-meta alk-num">{formatDateTime(entry.at)}</span>
                <span>{entry.words}</span>
              </li>
            ))}
          </ol>
        )}
      </Card>
      <ToastViewport toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
