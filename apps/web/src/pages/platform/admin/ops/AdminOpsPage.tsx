// SaaS-wide health on one page: machines by state and provider, chats served, gateway spend against
// the platform cap, crash reports, deployment checks and the running build. Re-reads every 30 s.

import type { ReactElement } from "react";
import { NavLink } from "react-router-dom";

import { Card, DescList, DescRow, Inline, Pill, Stack, StatCard, Table } from "@alkera/ui";

import styles from "./AdminOpsPage.module.css";
import { PORTAL_ROUTES } from "../../../../app/extensions/portal";
import { Icon } from "../../../../app/icons";
import { useOpsSummary, type OpsSummary } from "../../../../api/admin/admin";
import { AdminShell } from "../AdminShell";
import { Reading } from "../shared/chrome";
import { count, usd } from "../shared/format";
import { RegisterError, StatStripSkeleton } from "../shared/states";

/** The page's fixed strings. Shared with the test. */
export const OPS_LABELS = {
  subtitle: "Ops",
  machines: "Machines",
  machinesEmpty: "No live machines.",
  byState: "Machines by state",
  byProvider: "Machines by provider",
  chats: "Chats served",
  spendToday: "Spend today",
  spendMonth: "Spend this month",
  headroom: "Cap headroom",
  crashes: "Crash reports (24 h)",
  health: "Deployment health",
  healthEmpty: "No checks ran.",
  build: "Build",
} as const;

/** Where an installed crash-report viewer mounts. The open build serves none, so the
 *  unread count (which only the viewer clears) shows only when one is registered. */
const CRASH_REPORTS_PATH = "/admin/crash-reports";

type Tone = "success" | "warning" | "danger" | "neutral";

/** How a machine or check state reads on the page. */
export function stateTone(state: string): Tone {
  if (state === "ready" || state === "ok") return "success";
  if (state === "starting" || state === "draining" || state === "restarting" || state === "warn") return "warning";
  if (state === "unreachable" || state === "fail") return "danger";
  return "neutral";
}

export function AdminOpsPage(): ReactElement {
  const summary = useOpsSummary();
  return (
    <AdminShell subtitle={OPS_LABELS.subtitle}>
      {summary.isError ? (
        <RegisterError what="the ops summary" error={summary.error} onRetry={() => void summary.refetch()} />
      ) : !summary.data ? (
        <StatStripSkeleton />
      ) : (
        <OpsBody data={summary.data} />
      )}
    </AdminShell>
  );
}

function OpsBody({ data }: { data: OpsSummary }) {
  const headroom = data.spend.cap_headroom_nanos;
  const crashViewer = PORTAL_ROUTES.items().some((route) => route.path === CRASH_REPORTS_PATH);
  return (
    <Stack gap={7} align="stretch">
      <div className={styles.statGrid}>
        <StatCard label={OPS_LABELS.chats} value={<span className="alk-num">{count(data.chats_served_total)}</span>} note={`${count(data.machines.length)} live machines`} />
        <StatCard label={OPS_LABELS.spendToday} value={<span className="alk-num">{usd(data.spend.today_nanos)}</span>} note={`${usd(data.spend.month_to_date_nanos)} this month`} />
        <StatCard
          label={OPS_LABELS.headroom}
          value={<span className="alk-num">{headroom == null ? "No cap" : usd(headroom)}</span>}
          note={data.spend.monthly_cap_nanos == null ? "No platform cap" : `of ${usd(data.spend.monthly_cap_nanos)}`}
        />
        <StatCard
          label={OPS_LABELS.crashes}
          value={<span className="alk-num">{count(data.crash_reports_24h)}</span>}
          note={
            crashViewer ? (
              <NavLink to={CRASH_REPORTS_PATH} className="alk-link">{`${count(data.crash_reports_unread)} unread`}</NavLink>
            ) : undefined
          }
        />
      </div>

      <Card title={OPS_LABELS.byState} icon={<Icon name="monitor" size={16} />} headingLevel={2}>
        {data.machines_by_state.length === 0 ? (
          <p className="alk-caption">{OPS_LABELS.machinesEmpty}</p>
        ) : (
          <Inline gap={3} aria-label={OPS_LABELS.byState}>
            {data.machines_by_state.map((c) => (
              <Pill key={c.key} tone={stateTone(c.key)} shape="rect">{`${c.key} ${count(c.count)}`}</Pill>
            ))}
          </Inline>
        )}
        {data.machines_by_provider.length > 0 ? (
          <p className="alk-caption">
            {data.machines_by_provider.map((c) => `${c.key} ${count(c.count)}`).join(" · ")}
          </p>
        ) : null}
      </Card>

      <Card title={OPS_LABELS.machines} icon={<Icon name="monitor" size={16} />} headingLevel={2}>
        {data.machines.length === 0 ? (
          <p className="alk-caption">{OPS_LABELS.machinesEmpty}</p>
        ) : (
          <Table responsive pageSize={20} colWidths={[undefined, "8rem", "8rem", "9rem", "7rem"]} columns={["Machine", "Provider", "Tenancy", "State", "Chats"]}>
            {data.machines.map((m) => (
              <tr key={m.id}>
                <td><NavLink to={`/admin/orgs/${m.org_id}?tab=activity`} className="alk-link">{m.name || m.id}</NavLink></td>
                <td><Reading muted>{m.provider}</Reading></td>
                <td><Reading muted>{m.tenancy}</Reading></td>
                <td><Pill tone={stateTone(m.state)} shape="rect">{m.state}</Pill></td>
                <td><Reading align="end">{count(m.chats_served)}</Reading></td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <Card
        title={OPS_LABELS.health}
        icon={<Icon name="shieldCheck" size={16} />}
        headingLevel={2}
        actions={<Pill tone={stateTone(data.health.overall)}>{data.health.overall}</Pill>}
      >
        {data.health.checks.length === 0 ? (
          <p className="alk-caption">{OPS_LABELS.healthEmpty}</p>
        ) : (
          <Table responsive colWidths={["14rem", "7rem", undefined]} columns={["Check", "Status", "Detail"]}>
            {data.health.checks.map((c) => (
              <tr key={c.key}>
                <td>{c.label}</td>
                <td><Pill tone={stateTone(c.status)} shape="rect">{c.status}</Pill></td>
                <td><Reading muted>{c.detail}</Reading></td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <Card title={OPS_LABELS.build} icon={<Icon name="note" size={16} />} headingLevel={2}>
        <DescList>
          <DescRow label="Version"><Reading>{data.app_version}</Reading></DescRow>
          <DescRow label="Build ID"><Reading muted>{data.build_id ?? "Not set"}</Reading></DescRow>
        </DescList>
      </Card>
    </Stack>
  );
}
