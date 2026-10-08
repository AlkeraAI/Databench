// Whether this org's files open live (co-edited, saved as typed), and the one
// place an operator overrides the deployment's LIVE_EDITING_ENABLED for it.
//
// Reading is staff-grade; changing it is platform-admin, matching the route. The
// org's own setting has three states, each its own choice: follow the
// deployment, on, off. Every change goes through a confirmation naming its
// consequence, and the route's answer is shown afterwards: turning live editing
// off writes back the org's unsaved sessions first and says how many it could not.

import { useState } from "react";

import { Callout, Card, ConfirmDialog, DescList, DescRow, SegmentedControl, Stack } from "@alkera/ui";

import { Icon } from "../../../../app/icons";
import { ApiError } from "../../../../api/errors";
import {
  useAdminOrgLiveEditing,
  useSetAdminOrgLiveEditing,
  type OrgLiveEditing,
} from "../../../../api/admin/liveEditing";
import { RegisterError, TableSkeleton } from "../shared/states";
import { Reading } from "../shared/chrome";

export type LiveEditingChoice = "inherit" | "on" | "off";

/** The card's fixed strings, shared with the test. */
export const LIVE_EDITING_LABELS = {
  title: "Live editing",
  deployment: "Deployment default",
  orgSetting: "This org",
  result: "Result",
  on: "On",
  off: "Off",
  inherit: "Follow deployment",
  control: "Live editing for this org",
  adminOnly: "Only a platform admin can change an org's live editing.",
  readRefused: "Only platform staff can see an org's live editing.",
  confirm: "Change",
  failed: "Could not change live editing",
} as const;

const CHOICES: readonly { key: LiveEditingChoice; label: string }[] = [
  { key: "inherit", label: LIVE_EDITING_LABELS.inherit },
  { key: "on", label: LIVE_EDITING_LABELS.on },
  { key: "off", label: LIVE_EDITING_LABELS.off },
];

const choiceOf = (override: boolean | null): LiveEditingChoice =>
  override == null ? "inherit" : override ? "on" : "off";

const bodyOf = (choice: LiveEditingChoice): boolean | null =>
  choice === "inherit" ? null : choice === "on";

const onOff = (on: boolean): string => (on ? LIVE_EDITING_LABELS.on : LIVE_EDITING_LABELS.off);

/** What a choice will do, given where the deployment stands. */
export function consequence(choice: LiveEditingChoice, deployment: boolean): string {
  const turnsOn = choice === "on" || (choice === "inherit" && deployment);
  return turnsOn
    ? "Everyone in this org can open files live."
    : "Live editing ends for everyone in this org: pending edits are saved first and open files become read-only.";
}

/** The route's answer to a change, when it wrote anything back. */
function resultLine(answer: OrgLiveEditing): string | null {
  if (answer.sessions_written == null) return null;
  const left = answer.sessions_left_unsaved ?? 0;
  const saved = `${answer.sessions_written} ${answer.sessions_written === 1 ? "session" : "sessions"} saved`;
  return left > 0 ? `${saved}; ${left} left unsaved for the background sweep.` : `${saved}.`;
}

export function LiveEditingCard({ orgId, isAdmin }: { orgId: string; isAdmin: boolean }) {
  const reading = useAdminOrgLiveEditing(orgId);
  const change = useSetAdminOrgLiveEditing(orgId);
  const [pending, setPending] = useState<LiveEditingChoice | null>(null);
  const [answer, setAnswer] = useState<OrgLiveEditing | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const icon = <Icon name="monitor" size={16} />;
  if (reading.isError && reading.error instanceof ApiError && reading.error.status === 403) {
    return (
      <Card title={LIVE_EDITING_LABELS.title} icon={icon} headingLevel={2}>
        <p className="alk-caption">{LIVE_EDITING_LABELS.readRefused}</p>
      </Card>
    );
  }
  if (reading.isError) {
    return (
      <RegisterError
        what="this org's live editing"
        error={reading.error}
        onRetry={() => void reading.refetch()}
      />
    );
  }
  if (!reading.data) return <TableSkeleton rows={3} cols={2} />;
  const r = reading.data;
  const current = choiceOf(r.override);

  const run = () => {
    if (pending == null) return;
    setFailure(null);
    setAnswer(null);
    change.mutate(bodyOf(pending), {
      onSuccess: (done) => {
        setAnswer(done);
        setPending(null);
      },
      onError: (e: unknown) => {
        setFailure(e instanceof Error ? e.message : "The change failed.");
        setPending(null);
      },
    });
  };

  const result = answer ? resultLine(answer) : null;

  return (
    <Card title={LIVE_EDITING_LABELS.title} icon={icon} headingLevel={2}>
      <DescList>
        <DescRow label={LIVE_EDITING_LABELS.deployment}>
          <Reading muted>{onOff(r.deployment_default)}</Reading>
        </DescRow>
        <DescRow label={LIVE_EDITING_LABELS.orgSetting}>
          <Reading>{r.override == null ? LIVE_EDITING_LABELS.inherit : onOff(r.override)}</Reading>
        </DescRow>
        <DescRow label={LIVE_EDITING_LABELS.result}>
          <Reading>{onOff(r.enabled)}</Reading>
        </DescRow>
      </DescList>

      {failure ? (
        <Callout tone="danger" title={LIVE_EDITING_LABELS.failed}>
          {failure}
        </Callout>
      ) : null}
      {result ? (
        <Callout tone={(answer?.sessions_left_unsaved ?? 0) > 0 ? "warning" : "success"} title="Live editing changed">
          {result}
        </Callout>
      ) : null}

      {isAdmin ? (
        <Stack gap={5} align="stretch">
          <SegmentedControl
            options={CHOICES}
            value={current}
            semantics="radio"
            size="md"
            label={LIVE_EDITING_LABELS.control}
            onChange={(key) => {
              if (key !== current && !change.isPending) setPending(key as LiveEditingChoice);
            }}
          />
        </Stack>
      ) : (
        <p className="alk-caption">{LIVE_EDITING_LABELS.adminOnly}</p>
      )}

      <ConfirmDialog
        open={pending !== null}
        title={pending === null ? "" : `Set live editing to ${CHOICES.find((c) => c.key === pending)!.label.toLowerCase()}?`}
        consequence={pending === null ? "" : consequence(pending, r.deployment_default)}
        confirmLabel={LIVE_EDITING_LABELS.confirm}
        tone="warning"
        busy={change.isPending}
        onConfirm={run}
        onClose={() => setPending(null)}
      />
    </Card>
  );
}
