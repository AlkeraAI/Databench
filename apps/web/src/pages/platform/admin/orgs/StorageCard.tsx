// The org's Files ceiling, and the one place an operator overrides it.
//
// Reading is staff-grade; writing is platform-admin, matching the endpoint. The
// card is the ONLY route to a ceiling for an Enterprise org — its plan sets no
// figure, so until someone writes one here the org is living on the deployment's
// configured default.
//
// Three distinct writes, deliberately not folded together: a figure caps the org,
// Unlimited is an explicit override that the plan no longer applies to, and Use
// plan default drops the override entirely. "Unlimited" and "no override" look
// the same from a distance and are opposite states — an org whose plan later
// changes follows the new plan in one and ignores it in the other — so each has
// its own control and its own confirmation naming what it does.

import { useEffect, useState } from "react";

import { Button, Callout, Card, ConfirmDialog, DescList, DescRow, Inline, Stack, Switch } from "@alkera/ui";

import { Icon } from "../../../../app/icons";
import { ApiError, refusalSentence } from "../../../../api/errors";
import {
  useAdminOrgStorage,
  useClearAdminOrgStorage,
  useSetAdminOrgStorage,
  type OrgStorage,
} from "../../../../api/admin/storage";
import { SizeField } from "../../../../components/SizeField";
import { formatBytes, splitBytes, bytesFrom, type SizeUnit } from "@/lib/format/bytes";
import { RegisterError, TableSkeleton } from "../shared/states";
import { Reading } from "../shared/chrome";

/** The card's fixed strings. Shared with the test so the copy and the assertions
 *  never drift. */
export const STORAGE_LABELS = {
  title: "Storage",
  used: "Used",
  limit: "Limit",
  source: "Source",
  unlimited: "Unlimited",
  unlimitedSwitch: "No ceiling for this org",
  save: "Save limit",
  usePlan: "Use plan default",
  adminOnly: "Only a platform admin can change an org's storage limit.",
  readRefused: "Only a platform admin can see an org's storage.",
  editLabel: "Storage limit",
} as const;

/** The tier as the register writes it — the wire sends the enum's lowercase value. */
const tierName = (tier: string): string => tier.charAt(0).toUpperCase() + tier.slice(1).toLowerCase();

/**
 * Where this org's ceiling comes from, as a sentence.
 *
 * The plan reading names the tier and its figure, because "Plan default" alone
 * does not tell an operator whether the org is on 10 GB or 5 TB. An override
 * says so plainly, and an unlimited override says BOTH facts — that there is no
 * ceiling and that a person decided it — since a reader who saw only "Unlimited"
 * could not tell it from a deployment that never had a limit.
 */
export function sourceLine(s: OrgStorage): string {
  if (s.storage_limit_source === "override") {
    return s.storage_limit_bytes == null
      ? "Unlimited (set by an operator)"
      : "Set by an operator";
  }
  if (s.storage_limit_source === "plan") {
    const figure = formatBytes(s.storage_limit_bytes);
    return `Plan default · ${tierName(s.plan_tier)}${figure == null ? "" : ` · ${figure}`}`;
  }
  return "Deployment default";
}

/** The ceiling as a reading — `null` is not zero, it is no ceiling at all. */
const limitReading = (bytes: number | null): string =>
  bytes == null ? STORAGE_LABELS.unlimited : (formatBytes(bytes) ?? STORAGE_LABELS.unlimited);

type Pending = { kind: "set"; bytes: number | null } | { kind: "clear" } | null;

export function StorageCard({ orgId, isAdmin }: { orgId: string; isAdmin: boolean }) {
  const storage = useAdminOrgStorage(orgId);
  const set = useSetAdminOrgStorage(orgId);
  const clear = useClearAdminOrgStorage(orgId);

  const [amount, setAmount] = useState("");
  const [unit, setUnit] = useState<SizeUnit>("TB");
  const [unlimited, setUnlimited] = useState(false);
  const [pending, setPending] = useState<Pending>(null);
  const [failure, setFailure] = useState<string | null>(null);

  // Seed the editor from whatever ceiling the org is living under, once it
  // arrives. Keyed on the figure so a refetch that changes nothing does not
  // stamp on a half-typed edit.
  const current = storage.data?.storage_limit_bytes ?? null;
  const loaded = storage.data != null;
  useEffect(() => {
    if (!loaded) return;
    if (current == null) {
      setUnlimited(true);
      setAmount("");
      return;
    }
    const split = splitBytes(current);
    setUnlimited(false);
    setAmount(split.amount);
    setUnit(split.unit);
  }, [loaded, current]);

  // A refused read is the endpoint's answer for this grade, not a failure to retry: the card says
  // who may see it, the same way the bands beside it say who may change them.
  if (storage.isError && storage.error instanceof ApiError && storage.error.status === 403) {
    return (
      <Card title={STORAGE_LABELS.title} icon={<Icon name="vessel" size={16} />} headingLevel={2}>
        <p className="alk-caption">{STORAGE_LABELS.readRefused}</p>
      </Card>
    );
  }
  if (storage.isError) {
    return (
      <RegisterError
        what="this org's storage"
        error={storage.error}
        onRetry={() => void storage.refetch()}
      />
    );
  }
  if (!storage.data) return <TableSkeleton rows={3} cols={2} />;
  const s = storage.data;

  const entered = unlimited ? null : bytesFrom(amount, unit);
  const canSave = unlimited || entered != null;
  const busy = set.isPending || clear.isPending;

  const run = () => {
    if (pending == null) return;
    setFailure(null);
    const done = { onSuccess: () => setPending(null), onError: (e: unknown) => { setFailure(refusalSentence(e, { fallback: "The write failed." })); setPending(null); } };
    if (pending.kind === "clear") clear.mutate(undefined, done);
    else set.mutate(pending.bytes, done);
  };

  const confirmTitle =
    pending?.kind === "clear"
      ? "Use this org's plan default?"
      : pending?.kind === "set" && pending.bytes == null
        ? "Remove this org's storage ceiling?"
        : `Set this org's storage limit to ${formatBytes(pending?.kind === "set" ? (pending.bytes ?? 0) : 0)}?`;

  const confirmBody =
    pending?.kind === "clear"
      ? "The override is dropped and the org follows its plan's figure from now on, including any future plan change."
      : pending?.kind === "set" && pending.bytes == null
        ? "This org will be able to store without a ceiling, and its plan's figure will no longer apply."
        : "Members over the new ceiling keep what they have stored; every new write is refused until they are back under it.";

  return (
    <Card title={STORAGE_LABELS.title} icon={<Icon name="vessel" size={16} />} headingLevel={2}>
      <DescList>
        <DescRow label={STORAGE_LABELS.used}>
          <Reading>{formatBytes(s.storage_used_bytes) ?? "—"}</Reading>
        </DescRow>
        <DescRow label={STORAGE_LABELS.limit}>
          <Reading>{limitReading(s.storage_limit_bytes)}</Reading>
        </DescRow>
        <DescRow label={STORAGE_LABELS.source}>
          <Reading muted>{sourceLine(s)}</Reading>
        </DescRow>
      </DescList>

      {failure ? (
        <Callout tone="danger" title="Could not save the storage limit">
          {failure}
        </Callout>
      ) : null}

      {isAdmin ? (
        <Stack gap={5} align="stretch">
          <Inline gap={5} wrap={false}>
            <span className="alk-strong" style={{ marginRight: "auto" }}>{STORAGE_LABELS.unlimitedSwitch}</span>
            <Switch
              checked={unlimited}
              aria-label={STORAGE_LABELS.unlimitedSwitch}
              onChange={(e) => setUnlimited(e.target.checked)}
            />
          </Inline>
          {unlimited ? null : (
            <SizeField
              label={STORAGE_LABELS.editLabel}
              amount={amount}
              unit={unit}
              onAmount={setAmount}
              onUnit={setUnit}
              width={140}
            />
          )}
          <Inline gap={3}>
            <Button
              variant="primary"
              disabled={!canSave || busy}
              onClick={() => setPending({ kind: "set", bytes: entered })}
            >
              {STORAGE_LABELS.save}
            </Button>
            <Button
              variant="secondary"
              fill="ghost"
              disabled={busy || !s.override_set}
              onClick={() => setPending({ kind: "clear" })}
            >
              {STORAGE_LABELS.usePlan}
            </Button>
          </Inline>
        </Stack>
      ) : (
        <p className="alk-caption">{STORAGE_LABELS.adminOnly}</p>
      )}

      <ConfirmDialog
        open={pending !== null}
        title={pending === null ? "" : confirmTitle}
        consequence={pending === null ? "" : confirmBody}
        confirmLabel={pending?.kind === "clear" ? STORAGE_LABELS.usePlan : STORAGE_LABELS.save}
        tone="warning"
        busy={busy}
        onConfirm={run}
        onClose={() => setPending(null)}
      />
    </Card>
  );
}
