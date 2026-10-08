import { useState } from "react";

import { Button, Callout, Card, ConfirmDialog, DescList, DescRow, Stack, Toolbar } from "@alkera/ui";

import { Icon } from "../../../../app/icons";
import {
  useAdminAccount,
  useAdminCancelDeletion,
  useAdminScheduleDeletion,
} from "../../../../api/admin/accountLifecycle";
import { refusalSentence } from "../../../../api/errors";
import { dateTime } from "../shared/format";

type Confirming = "schedule" | "erase" | "cancel" | null;

/**
 * A person's deletion request, for a request they made through support. Support staff read;
 * platform admins schedule a deletion with the usual grace period, erase now, or cancel.
 */
export function AccountRequestsCard({
  userId,
  isAdmin,
  onDone,
}: {
  userId: string;
  isAdmin: boolean;
  onDone: (t: string, ok: boolean) => void;
}) {
  const account = useAdminAccount(userId);
  const schedule = useAdminScheduleDeletion(userId);
  const cancel = useAdminCancelDeletion(userId);
  const [confirming, setConfirming] = useState<Confirming>(null);
  const [error, setError] = useState<string | null>(null);

  const data = account.data;
  const deletion = data?.deletion ?? null;
  const live = deletion?.status === "scheduled";
  const busy = schedule.isPending || cancel.isPending;

  const fail = (err: unknown, fallback: string) => {
    const message = refusalSentence(err, { fallback });
    setError(message);
    onDone(message, false);
  };
  const run = () => {
    setError(null);
    const which = confirming;
    setConfirming(null);
    if (which === "schedule" || which === "erase") {
      schedule.mutate(which === "erase", {
        onSuccess: () => onDone(which === "erase" ? "Erasure queued." : "Deletion scheduled.", true),
        onError: (e) => fail(e, "Could not schedule the deletion."),
      });
    } else if (which === "cancel") {
      cancel.mutate(undefined, {
        onSuccess: () => onDone("Deletion cancelled.", true),
        onError: (e) => fail(e, "Could not cancel the deletion."),
      });
    }
  };

  return (
    <Card title="Account requests" icon={<Icon name="shield" size={16} />} headingLevel={2}>
      <Stack gap={5} align="stretch">
        {account.isError ? <Callout tone="danger">{refusalSentence(account.error, { fallback: "Could not load this person's account requests." })}</Callout> : null}
        {error ? <Callout tone="danger" role="alert">{error}</Callout> : null}
        <DescList>
          <DescRow label="Deletion">
            {data?.deleted_at
              ? `Erased ${dateTime(data.deleted_at)}`
              : live
                ? `Scheduled for ${dateTime(deletion.purge_after)}${deletion.blocked_reason ? `, on hold: ${deletion.blocked_reason}` : ""}`
                : deletion
                  ? `Last request ${deletion.status}`
                  : "None"}
          </DescRow>
        </DescList>
        {isAdmin && data && !data.deleted_at ? (
          <Toolbar
            end={
              live ? (
                <>
                  <Button variant="secondary" disabled={busy} onClick={() => setConfirming("cancel")}>
                    Cancel deletion
                  </Button>
                  <Button variant="destructive" disabled={busy} onClick={() => setConfirming("erase")}>
                    Erase now
                  </Button>
                </>
              ) : (
                <Button variant="destructive" fill="outline" disabled={busy} onClick={() => setConfirming("schedule")}>
                  Schedule deletion
                </Button>
              )
            }
          />
        ) : null}
      </Stack>
      <ConfirmDialog
        open={confirming !== null}
        onClose={() => setConfirming(null)}
        onConfirm={run}
        title={
          confirming === "cancel"
            ? "Cancel this deletion?"
            : confirming === "erase"
              ? "Erase this account now?"
              : "Schedule this account's deletion?"
        }
        consequence={
          confirming === "cancel"
            ? "The account stays. The person is emailed."
            : confirming === "erase"
              ? "The erasure runs on the next lifecycle pass and cannot be undone."
              : "The account is erased after the grace period. Every credential it holds ends now."
        }
        confirmLabel={confirming === "cancel" ? "Cancel deletion" : confirming === "erase" ? "Erase now" : "Schedule deletion"}
        tone={confirming === "cancel" ? "warning" : "destructive"}
        busy={busy}
      />
    </Card>
  );
}
