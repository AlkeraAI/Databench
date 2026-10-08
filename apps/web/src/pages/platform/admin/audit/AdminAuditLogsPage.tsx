// The platform audit log — the chronicle of every successful support & admin action. Paginated
// and filtered server-side (action, actor, day range — the org log's filter bar); the table reads
// when, who (actor + role), what, what it was done to, and the response status. Clicking a row
// opens the full record in the shared log-detail drawer — the request line as a summary, the
// heterogeneous detail payload as highlighted JSON. Platform-admin only.

import { useState } from "react";

import { Card, EmptyState, Pill, Table, type PillTone } from "@alkera/ui";

import {
  AuditFilterBar,
  EMPTY_AUDIT_FILTERS,
  auditFiltersToWire,
  type AuditFilterDraft,
} from "@/components/audit/AuditFilterBar";

import { Icon } from "../../../../app/icons";
import { LogDetailPanel, LogJson, LogRow, LogSummary } from "@alkera/ui";
import { useAuditLogs, type AuditLogEntry } from "../../../../api/admin/audit";
import { AdminShell } from "../AdminShell";
import { Reading } from "../shared/chrome";
import { dateTime, dateTimeExact } from "../shared/format";
import { RegisterEmpty, RegisterError, TableSkeleton } from "../shared/states";

const PAGE_SIZE = 50;
const ROLE_TONE: Record<string, PillTone> = { alkera_admin: "brand", alkera_support: "info" };
const COLUMNS = ["When", "Actor", "Action", "Target", "Status"];
// Narrow the date / status columns; let the actor, action and target columns take the room.
const COL_WIDTHS = ["13.5rem", "15rem", undefined, undefined, "7rem"];

function StatusPill({ code }: { code: number }) {
  const tone: PillTone = code < 300 ? "success" : code < 400 ? "info" : code < 500 ? "warning" : "danger";
  return <Pill tone={tone} shape="rect">{code}</Pill>;
}

export function AdminAuditLogsPage() {
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<AuditLogEntry | null>(null);
  const [draft, setDraft] = useState(EMPTY_AUDIT_FILTERS);
  const filters = auditFiltersToWire(draft);
  const hasFilters = Object.values(filters).some(Boolean);
  const logs = useAuditLogs(page, PAGE_SIZE, filters);
  const data = logs.data;
  // A change to any filter re-queries from the first page.
  const rescope = (patch: Partial<AuditFilterDraft>) => {
    setDraft((d) => ({ ...d, ...patch }));
    setPage(1);
  };

  return (
    <AdminShell subtitle="Every successful platform support and admin action">
      {logs.isError ? (
        <RegisterError what="the audit log" error={logs.error} onRetry={() => void logs.refetch()} />
      ) : !data ? (
        <TableSkeleton rows={8} cols={5} />
      ) : data.items.length === 0 && !hasFilters ? (
        <RegisterEmpty title="No actions recorded" body="Support and admin actions appear here as they happen." mark={<Icon name="note" size={44} />} />
      ) : (
        <Card title="Audit log" icon={<Icon name="note" size={16} />}>
          <AuditFilterBar
            draft={draft}
            active={hasFilters}
            onPatch={rescope}
            actionOptions={(data.actions ?? []).map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          />
          {data.items.length === 0 ? (
            <EmptyState size="md" title="No matching actions" body="No actions match the active filters." />
          ) : (
            <Table
              responsive
              // Card mode (narrow): the action (col 2) titles the card; the rest read as fields.
              stackPrimary={2}
              colWidths={COL_WIDTHS}
              columns={COLUMNS}
              onRowClick={(i) => setSelected(data.items[i])}
              pagination={{ page, pageSize: PAGE_SIZE, total: data.total, onPageChange: setPage }}
            >
              {data.items.map((e) => (
                <tr key={e.id} aria-haspopup="dialog" aria-label={`View details for ${e.action}`}>
                  <td><Reading muted>{dateTime(e.created_at)}</Reading></td>
                  <td>
                    <span className="alk-stack">
                      <span className="alk-name">{e.actor_email || "—"}</span>
                      {e.actor_platform_role ? <Pill tone={ROLE_TONE[e.actor_platform_role] ?? "neutral"} shape="rect">{e.actor_platform_role}</Pill> : null}
                    </span>
                  </td>
                  <td><span className="alk-strong">{e.action}</span></td>
                  <td>{e.target || "—"}</td>
                  <td><StatusPill code={e.status_code} /></td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      )}

      <AuditDetailPanel entry={selected} onClose={() => setSelected(null)} />
    </AdminShell>
  );
}

/** One audit entry's full reading — the actor, the request line, the response status, then the
 *  heterogeneous detail payload as JSON. Shares the drawer with the org audit + crash logs. */
function AuditDetailPanel({ entry, onClose }: { entry: AuditLogEntry | null; onClose: () => void }) {
  return (
    <LogDetailPanel open={entry !== null} onClose={onClose} eyebrow="Audit event" title={entry?.action ?? "Event"}>
      {entry ? (
        <>
          <LogSummary>
            {/* Seconds in the drawer: the full record is what gets correlated
                against server logs, mirroring the org audit drawer. */}
            <LogRow label="When">{dateTimeExact(entry.created_at)}</LogRow>
            <LogRow label="Actor">{entry.actor_email || "—"}</LogRow>
            {entry.actor_platform_role ? <LogRow label="Role">{entry.actor_platform_role}</LogRow> : null}
            <LogRow label="Target">{entry.target || "—"}</LogRow>
            <LogRow label="Request"><span className="alk-num">{entry.method} {entry.path}</span></LogRow>
            <LogRow label="Status"><StatusPill code={entry.status_code} /></LogRow>
          </LogSummary>
          <LogJson data={entry.detail} />
        </>
      ) : null}
    </LogDetailPanel>
  );
}
