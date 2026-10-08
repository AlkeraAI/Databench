import { useState, type ReactNode } from "react";

import {
  Anchor,
  Button,
  Callout,
  Card,
  CopyButton,
  currentBrand,
  EmptyState,
  Pill,
  Table,
} from "@alkera/ui";

import { Icon, type IconName } from "../../../app/icons";
import { TopbarActions } from "../../../app/Topbar";
import { LogDetailPanel, LogJson, LogRow, LogSummary } from "@alkera/ui";
import {
  auditCsvUrl,
  useAuditVerify,
  useOrgAudit,
  type AuditChainVerification,
  type OrgAuditEvent,
} from "../../../api/orgAdminAudit";
import { OrgLoadError, OrgLoading, OrgPage } from "./chrome";
import { UnavailableFeature, useFeatureGate } from "./FeatureGate";
import { formatDateTime, formatDateTimeExact } from "@/lib/format/date";
import {
  AuditFilterBar,
  EMPTY_AUDIT_FILTERS,
  auditFiltersToWire,
  type AuditFilterDraft,
} from "@/components/audit/AuditFilterBar";
import { refusalSentence } from "../../../api/errors";

export const PAGE = 50;

// Core columns only — When / Actor / Action / Target. An event's structured payload (role, team,
// amount, protocol) is heterogeneous per action, so it lives in the row's side panel rather than
// in mostly-empty columns.
const COLUMNS = ["When", "Actor", "Action", "Target"];
// When + Action stay contained (a date, a dotted code); Actor + Target (emails/labels) share the rest.
const COL_WIDTHS = ["11rem", undefined, "16rem", undefined];

// The action families an admin filters by — each value is a server-side prefix match, so one
// entry covers its whole dotted vocabulary. Marks reuse the app's nav glyphs where a family
// has a page (SSO/shield, billing/coins, deployment health/monitor).
const ACTION_FAMILIES: [string, string, IconName][] = [
  ["agent.", "Agent activity", "chats"],
  ["auth.", "Authentication", "key"],
  ["billing.", "Billing", "coins"],
  ["team_connection.", "Connections", "branch"],
  ["deployment_health.", "Deployment health", "monitor"],
  ["invitation.", "Invitations", "mail"],
  ["member", "Members & roles", "users"],
  ["model_provider.", "Model providers", "flask"],
  ["org_settings.", "Organization settings", "settings"],
  ["scim.", "SCIM", "refresh"],
  ["sso.", "SSO", "shield"],
];

const FAMILY_OPTIONS = ACTION_FAMILIES.map(([value, label]) => (
  <option key={value} value={value}>
    {label}
  </option>
));

const FAMILY_ICONS: Record<string, ReactNode> = Object.fromEntries(
  ACTION_FAMILIES.map(([value, , icon]) => [value, <Icon key={value} name={icon} size={14} />]),
);

export function AuditLogPage() {
  // Gated by the server: an org it refuses sees the unavailable plate, and
  // AuditBody (which fetches the now-403 events) is never mounted.
  const gate = useFeatureGate();
  return (
    <OrgPage subtitle="Security-relevant actions across your organization.">
      {() =>
        gate.status === "loading" ? (
          <OrgLoading sections={1} />
        ) : gate.status === "error" ? (
          <OrgLoadError message="could not check your access" onRetry={gate.refetch} />
        ) : gate.status === "gated" ? (
          <UnavailableFeature
            feature="Audit log"
            blurb="A tamper-evident audit trail with CSV export"
            icon={<Icon name="books" size={48} />}
          />
        ) : (
          <AuditBody />
        )
      }
    </OrgPage>
  );
}

const eventRow = (e: OrgAuditEvent) => (
  <tr key={e.id} aria-haspopup="dialog" aria-label={`View details for ${e.action}`}>
    <td className="alk-meta">{formatDateTime(e.created_at)}</td>
    <td>{e.actor_email || "—"}</td>
    <td>
      <Pill variant="code">{e.action}</Pill>
    </td>
    <td className="alk-meta">{e.target || "—"}</td>
  </tr>
);

function AuditBody() {
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<OrgAuditEvent | null>(null);
  const [draft, setDraft] = useState(EMPTY_AUDIT_FILTERS);
  const verify = useAuditVerify();
  const filters = auditFiltersToWire(draft);
  const hasFilters = Object.values(filters).some(Boolean);
  const audit = useOrgAudit(offset, PAGE, filters);

  if (audit.isPending) return <OrgLoading sections={1} />;
  if (audit.isError || !audit.data) {
    return (
      <OrgLoadError
        message={refusalSentence(audit.error, { fallback: "could not load the audit log" })}
        onRetry={() => void audit.refetch()}
      />
    );
  }

  const { events, total } = audit.data;
  const page = Math.floor(offset / PAGE) + 1;
  // A change to any filter re-queries from the first page.
  const rescope = (patch: Partial<AuditFilterDraft>) => {
    setDraft((d) => ({ ...d, ...patch }));
    setOffset(0);
  };

  const table = (
    <Table
      responsive
      colWidths={COL_WIDTHS}
      columns={COLUMNS}
      onRowClick={(i) => setSelected(events[i])}
      pagination={{ page, pageSize: PAGE, total, onPageChange: (p) => setOffset((p - 1) * PAGE) }}
    >
      {events.map(eventRow)}
    </Table>
  );
  // A backwards range can match nothing; say that rather than implying the log is empty there.
  const inverted = Boolean(draft.from && draft.to && draft.from > draft.to);
  const emptyState = hasFilters ? (
    <EmptyState
      size="md"
      title="No matching events"
      body={inverted ? "The From date is after the To date." : "No events match the active filters."}
    />
  ) : (
    // No table/pager/filters on an empty log — a lone "Page 1 of 1" reads as broken.
    <EmptyState
      size="md"
      title="No events yet"
      body="Security-relevant actions will appear here as they happen."
    />
  );

  return (
    <>
      <AuditTopbar csvHref={auditCsvUrl(filters)} verify={verify} />
      <VerifyOutcome verify={verify} />
      <Card variant="section" icon={<Icon name="books" />} title="Audit log">
        {events.length > 0 || hasFilters ? (
          <AuditFilterBar
            draft={draft}
            active={hasFilters}
            onPatch={rescope}
            actionOptions={FAMILY_OPTIONS}
            actionIcons={FAMILY_ICONS}
          />
        ) : null}
        {events.length === 0 ? emptyState : table}
      </Card>
      <AuditDetailPanel event={selected} onClose={() => setSelected(null)} />
    </>
  );
}

type VerifyQuery = ReturnType<typeof useAuditVerify>;

function AuditTopbar({ csvHref, verify }: { csvHref: string; verify: VerifyQuery }) {
  return (
    <TopbarActions>
      <Button
        style={{ flex: "none" }}
        variant="secondary"
        leftSection={<Icon name="shield" size={15} />}
        disabled={verify.isPending}
        onClick={() => verify.mutate()}
      >
        {verify.isPending ? "Verifying…" : "Verify chain"}
      </Button>
      <Anchor style={{ flex: "none" }} href={csvHref} download>
        <Button variant="secondary" leftSection={<Icon name="down" size={15} />}>
          Export CSV
        </Button>
      </Anchor>
    </TopbarActions>
  );
}

function VerifyOutcome({ verify }: { verify: VerifyQuery }) {
  if (verify.isError) {
    return (
      <Callout tone="warning" title="Verification failed">
        {refusalSentence(verify.error, { fallback: "Could not verify the audit chain." })} Try again.
      </Callout>
    );
  }
  if (!verify.data) return null;
  return verify.data.ok ? (
    <IntactResult result={verify.data} />
  ) : (
    <BrokenResult result={verify.data} />
  );
}

function IntactResult({ result }: { result: AuditChainVerification }) {
  return (
    <Callout tone="success" title="Chain intact">
      All {result.checked.toLocaleString()} events in the chain verify. Actions by platform staff
      (platform.*) are listed with them but recorded outside the chain.
      {result.head_hash ? <HeadHashNote hash={result.head_hash} /> : null}
    </Callout>
  );
}

/** The head hash is the chain's external checkpoint: copied somewhere the platform can't touch, it
 *  pins today's history — even a full rewrite of the log cannot reproduce it. */
function HeadHashNote({ hash }: { hash: string }) {
  return (
    <>
      {" "}
      Head hash <code>{hash.slice(0, 16)}…</code>{" "}
      <CopyButton value={hash} size="sm" variant="secondary" fill="ghost">
        Copy
      </CopyButton>{" "}
      Save it outside {currentBrand().productName}; it pins today's history against a future rewrite.
    </>
  );
}

function BrokenResult({ result }: { result: AuditChainVerification }) {
  const at = result.broken_at ? ` (${formatDateTimeExact(result.broken_at)})` : "";
  return (
    <Callout tone="danger" title="Chain broken">
      Verification failed at event <code>{result.broken_event_id}</code>
      {at}. The {result.checked.toLocaleString()} events before it verify; that row and everything
      after it can no longer be trusted.
    </Callout>
  );
}

/** The full reading of one audit event — its core fields as a summary, then whatever else the event
 *  recorded as highlighted JSON. Shares the log-detail drawer with the platform audit + crash logs. */
function AuditDetailPanel({
  event,
  onClose,
}: {
  event: OrgAuditEvent | null;
  onClose: () => void;
}) {
  return (
    <LogDetailPanel
      open={event !== null}
      onClose={onClose}
      eyebrow="Audit event"
      title={event?.action ?? "Event"}
      width={460}
    >
      {event ? (
        <>
          <LogSummary>
            <LogRow label="When">{formatDateTimeExact(event.created_at)}</LogRow>
            <LogRow label="Actor">{event.actor_email || "—"}</LogRow>
            <LogRow label="Target">{event.target || "—"}</LogRow>
          </LogSummary>
          <LogJson data={event.detail} />
        </>
      ) : null}
    </LogDetailPanel>
  );
}
