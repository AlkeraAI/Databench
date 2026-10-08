import { useState } from "react";

import { Button, Callout, ConfirmDialog } from "@alkera/ui";

import { useSwitchOrg } from "../../../api/auth";
import { refusalSentence, stepUpLoginUrl } from "../../../api/errors";
import { useLeaveOrg, useMultiOrgEnabled } from "../../../api/orgs";
import { ActionRow } from "./fields";

/**
 * "Leave organization", only where the server runs with several orgs per person. After
 * leaving, the browser switches into the org the server names next, or, when none is left,
 * goes to the no-organization landing.
 */
export function LeaveOrgRow({ orgName }: { orgName: string }) {
  if (!useMultiOrgEnabled()) return null;
  return <LeaveOrgControl orgName={orgName} />;
}

function LeaveOrgControl({ orgName }: { orgName: string }) {
  const leave = useLeaveOrg();
  const switchOrg = useSwitchOrg();
  const [open, setOpen] = useState(false);

  const busy = leave.isPending || switchOrg.isPending;
  const refused = leave.isError
    ? refusalSentence(leave.error)
    : switchOrg.isError && !stepUpLoginUrl(switchOrg.error)
      ? refusalSentence(switchOrg.error)
      : null;
  const name = orgName || "this organization";

  return (
    <>
      <ActionRow label="Leave organization">
        <Button variant="destructive" fill="outline" onClick={() => setOpen(true)}>
          Leave
        </Button>
      </ActionRow>
      <ConfirmDialog
        open={open}
        onClose={() => {
          if (busy) return;
          leave.reset();
          setOpen(false);
        }}
        onConfirm={() =>
          leave.mutate(undefined, {
            onSuccess: (data) => {
              if (data.next_org_team_id) switchOrg.mutate({ orgTeamId: data.next_org_team_id });
            },
          })
        }
        title={`Leave ${name}?`}
        consequence="You lose access to it until someone invites you again."
        confirmLabel="Leave"
        tone="destructive"
        busy={busy}
      >
        {refused ? <Callout tone="danger">{refused}</Callout> : null}
      </ConfirmDialog>
    </>
  );
}
