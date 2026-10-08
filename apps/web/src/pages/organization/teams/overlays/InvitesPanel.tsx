import { useState } from "react";

import { Button, Callout, Card, EmptyState, Skeleton, Stack } from "@alkera/ui";

import { Icon } from "../../../../app/icons";
import { useCurrentUser, useSwitchOrg } from "../../../../api/auth";
import { refusalSentence, stepUpLoginUrl } from "../../../../api/errors";
import { useMultiOrgEnabled, type InvitationAccepted } from "../../../../api/orgs";
import { useAcceptInvitationMutation, useMyInvitations, useRejectInvitationMutation } from "../../../../api/teams";
import { formatDate } from "@/lib/format/date";
import styles from "./InvitesPanel.module.css";

// The caller's OWN pending invitations to join a team — the recipient side (distinct from a team's
// outgoing invitations shown on the detail). Each row names its destination from the payload itself
// (an invitation into another organization is not in the caller's team list). One the account
// cannot accept — the single-org rule — carries the server's refusal in place of Accept: it says why
// and what would have to change, and Decline still clears it for the sender. Rendered inside a
// SidePanel by the page, so this owns only the list + its empty/loading/error states.

export function InvitesPanel({ onResolved }: { onResolved?: (text: string, ok: boolean) => void }) {
  // The last accepted invitation, kept above the list (which drops the row once it refreshes)
  // so an acceptance into another org can offer the switch.
  const [joined, setJoined] = useState<InvitationAccepted | null>(null);
  const list = <InvitesList onResolved={onResolved} onJoined={setJoined} />;
  if (!joined) return list;
  return (
    <>
      <JoinedOrgOffer accepted={joined} />
      {list}
    </>
  );
}

function InvitesList({
  onResolved,
  onJoined,
}: {
  onResolved?: (text: string, ok: boolean) => void;
  onJoined: (accepted: InvitationAccepted) => void;
}) {
  const invites = useMyInvitations();
  const accept = useAcceptInvitationMutation();
  const reject = useRejectInvitationMutation();

  if (invites.isLoading) {
    return (
      <div className={styles.invlist} aria-hidden="true">
        {[0, 1].map((i) => (
          <Skeleton key={i} height={96} />
        ))}
      </div>
    );
  }
  if (invites.isError) {
    return <EmptyState size="md" tone="alert" icon={<Icon name="alert" size={32} />} title="We couldn’t load your invitations" body="Try again." />;
  }
  const rows = invites.data ?? [];
  if (rows.length === 0) {
    return <EmptyState size="md" icon={<Icon name="mail" size={32} />} title="No pending invitations" />;
  }

  return (
    <ul className={styles.invlist}>
      {rows.map((inv) => {
        const busy = (accept.isPending && accept.variables === inv.id) || (reject.isPending && reject.variables === inv.id);
        const name = destination(inv.team_name, inv.org_name);
        const failed = (verb: string) => (e: unknown) =>
          onResolved?.(refusalSentence(e, { fallback: `Couldn’t ${verb} the invitation to ${name}.` }), false);
        return (
          <li key={inv.id}>
            <Card>
              <Stack gap={5} align="stretch">
                <div className="alk-stack">
                  <span className="alk-strong">{name}</span>
                  <span className="alk-meta">
                    {inv.inviter_display_name ? `From ${inv.inviter_display_name} · ` : ""}
                    {inv.role_display} · expires {formatDate(inv.expires_at)}
                  </span>
                </div>
                {inv.refusal ? <Callout tone="warning">{inv.refusal.message}</Callout> : null}
                <div className="alk-inline">
                  {inv.refusal ? null : (
                    <Button
                      variant="secondary"
                      disabled={busy}
                      leftSection={<Icon name="check" size={15} />}
                      onClick={() =>
                        accept.mutate(inv.id, {
                          onSuccess: (accepted) => {
                            onResolved?.(`Joined ${name}.`, true);
                            if (accepted) onJoined(accepted);
                          },
                          onError: failed("accept"),
                        })
                      }
                    >
                      Accept
                    </Button>
                  )}
                  <Button
                    variant="secondary" fill="ghost"
                    disabled={busy}
                    onClick={() =>
                      reject.mutate(inv.id, {
                        onSuccess: () => onResolved?.(`Declined the invitation to ${name}.`, true),
                        onError: failed("decline"),
                      })
                    }
                  >
                    Decline
                  </Button>
                </div>
              </Stack>
            </Card>
          </li>
        );
      })}
    </ul>
  );
}

/** After accepting an invitation into another org than the one this browser is in: the switch
 *  into it. An acceptance into the current org offers nothing (it already shows there). */
function JoinedOrgOffer({ accepted }: { accepted: InvitationAccepted }) {
  const current = useCurrentUser().data?.org_team_id;
  const switchOrg = useSwitchOrg();
  const multiOrg = useMultiOrgEnabled();
  const org = accepted.org_team_id;
  if (!multiOrg || !org || !current || org === current) return null;
  const name = accepted.org_name || "the organization";
  const failed = switchOrg.isError && !stepUpLoginUrl(switchOrg.error) ? refusalSentence(switchOrg.error) : null;
  return (
    <Callout tone="success" title={`Joined ${name}.`}>
      {failed ? <p className="alk-meta">{failed}</p> : null}
      <Button variant="secondary" loading={switchOrg.isPending} onClick={() => switchOrg.mutate({ orgTeamId: org })}>
        {`Switch to ${name}`}
      </Button>
    </Callout>
  );
}

/** "Team in Org", or the org alone when the invitation is to the org itself (or a team that shares
 *  its name) — the same rule the signup page's invitation line follows. */
function destination(team: string, org: string): string {
  return !org || team === org ? team || org : `${team} in ${org}`;
}
