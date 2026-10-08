import { Button, Callout, Card, currentBrand, Inline, Pill, Stack, type PillTone } from "@alkera/ui";

import { Icon } from "../../../../app/icons";
import {
  useDeploymentHealth,
  useRunDeploymentHealth,
  type DeploymentHealthCheck,
} from "../../../../api/orgDeploymentHealth";
import { OrgLoadError, OrgLoading } from "../chrome";
import { refusalSentence } from "../../../../api/errors";

const STATUS_TONE: Record<string, PillTone> = {
  ok: "success",
  warn: "warning",
  fail: "danger",
  skipped: "neutral",
};
const STATUS_LABEL: Record<string, string> = {
  ok: "OK",
  warn: "Warn",
  fail: "Fail",
  skipped: "Skipped",
};

// Each check is filed under a section by a static key→group map (keeps the API flat).
const GROUP: Record<string, string> = {
  postgres: "Infrastructure",
  migrations: "Infrastructure",
  temporal: "Infrastructure",
  worker_beat: "Infrastructure",
  model_gateway: "Connectivity",
  alkera_upstream: "Connectivity",
  alkera_proxy_token: "Connectivity",
  smtp: "Connectivity",
  entitlement: "Configuration",
  entitlement_consistency: "Configuration",
  catalog: "Configuration",
  secret_box: "Configuration",
};
const GROUP_ORDER = ["Infrastructure", "Connectivity", "Configuration"];

function groupFor(key: string): string {
  if (key.startsWith("provider_")) return "Connectivity";
  return GROUP[key] ?? "Configuration";
}

function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const secs = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  return `${Math.round(secs / 3600)}h ago`;
}

export function HealthPanel() {
  const health = useDeploymentHealth();
  const run = useRunDeploymentHealth();

  if (health.isPending) return <OrgLoading sections={3} />;
  if (health.isError || !health.data) {
    return (
      <OrgLoadError
        message={refusalSentence(health.error, { fallback: "could not load deployment health" })}
        onRetry={() => void health.refetch()}
      />
    );
  }

  const report = health.data;
  const banner =
    report.overall === "ok"
      ? { tone: "success" as const, title: "All systems go" }
      : report.overall === "warn"
        ? { tone: "warning" as const, title: "Needs attention" }
        : { tone: "danger" as const, title: "Problems detected" };

  const byGroup = new Map<string, DeploymentHealthCheck[]>();
  for (const check of report.checks) {
    const g = groupFor(check.key);
    (byGroup.get(g) ?? byGroup.set(g, []).get(g)!).push(check);
  }
  const modeLabel = report.mode === "proxy" ? `Proxying through ${currentBrand().productName}'s gateway` : "Direct provider access";

  return (
    <Stack gap={5} align="stretch">
      <Callout tone={banner.tone} title={banner.title}>
        {modeLabel} · backend {report.backend_version}
      </Callout>

      {GROUP_ORDER.filter((g) => byGroup.has(g)).map((group) => (
        <Card key={group} variant="section" title={group}>
          <Stack gap={4} align="stretch">
            {byGroup.get(group)!.map((check) => (
              <Stack key={check.key + (check.org_scoped ? "-org" : "")} gap={1}>
                <Inline gap={2} align="center">
                  <Pill tone={STATUS_TONE[check.status]} shape="rect" dot>
                    {STATUS_LABEL[check.status]}
                  </Pill>
                  <span className="alk-strong">{check.label}</span>
                  {check.org_scoped ? <Pill tone="brand">org</Pill> : null}
                </Inline>
                {check.detail ? <span className="alk-caption">{check.detail}</span> : null}
              </Stack>
            ))}
          </Stack>
        </Card>
      ))}

      <Inline justify="space-between" align="center">
        <span className="alk-meta">
          Last checked {ago(report.last_run_at)}
          {report.last_trigger ? ` (${report.last_trigger})` : ""}
        </span>
        <Button
          leftSection={<Icon name="refresh" size={15} />}
          loading={run.isPending}
          onClick={() => run.mutate()}
        >
          {run.isPending ? "Running checks…" : "Run now"}
        </Button>
      </Inline>
    </Stack>
  );
}
