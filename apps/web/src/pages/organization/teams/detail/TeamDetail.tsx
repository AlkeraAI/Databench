import { useMemo, useState, type CSSProperties } from "react";
import { useSearchParams } from "react-router-dom";

import { Button, Card, EmptyState, Pill, Stack, Tabs, cx } from "@alkera/ui";

import { Icon } from "../../../../app/icons";
import { type MenuItem } from "./actions";
import { Breadcrumb } from "./chrome";
import { Masthead } from "./Masthead";
import { Toolbar } from "./Toolbar";
import { Roster } from "./Roster";
import { ContextColumn } from "./ContextColumn";
import { TEAM_SECTIONS } from "../../../../app/extensions/portal";
import { SpreadSkeleton } from "./Skeleton";
import {
  childrenOf,
  descentCount,
  descentRoster,
  directCount,
  directRoster,
  teamById,
  teamMemberCount,
  type DetailStatus,
  type Graph,
  type Invite,
  type Role,
  type RosterEntry,
  type Team,
} from "../data/model";
import { canRemove, isOrgAdmin, moveTargets } from "../data/permissions";
import shell from "../teams.module.css";
import styles from "./TeamDetail.module.css";

export interface DetailActions {
  selectTeam: (id: string) => void;
  changeRole: (teamId: string, personId: string, role: Role) => void;
  removeMember: (teamId: string, entry: RosterEntry) => void;
  moveMember: (teamId: string, entry: RosterEntry) => void;
  addMember: (teamId: string) => void;
  createSub: (parentId: string) => void;
  renameTeam: (teamId: string) => void;
  moveTeam: (teamId: string) => void;
  deleteTeam: (teamId: string) => void;
  revokeInvite: (teamId: string, invite: Invite) => void;
  viewPlan: (teamId: string, entry: RosterEntry) => void;
}

type Scope = "direct" | "descent";

/** The selected team's detail — a register spread. The breadcrumb + masthead show
 *  as soon as the team is known; the spread (roster ledger + context plates)
 *  resolves through `detail.status`: a skeleton while the roster loads, a bare
 *  "you don't manage this team" surface when the viewer doesn't administer it
 *  (the roster endpoint is admin-gated, and a non-admin sees nothing beyond the
 *  team's name — not even the breadcrumb's other team names), an error with
 *  retry, or the register spread. Manage actions appear only when the viewer can
 *  see the roster — i.e. is an admin here. Keyed by team id by the page, so
 *  switching resets cleanly. */
export function TeamDetail({
  graph,
  teamId,
  detail,
  actions,
  onRetry,
  leads = [],
}: {
  graph: Graph;
  teamId: string;
  detail: { status: DetailStatus; errorMessage: string | null };
  actions: DetailActions;
  onRetry: () => void;
  /** Other teams the viewer leads — offered from the "you don't manage this team" surface. */
  leads?: Team[];
}) {
  const team = teamById(graph, teamId);
  if (!team) return null;

  const canManage = detail.status === "ready";
  // Creating, moving and deleting teams are org-admin decisions on the server; a sub-team admin
  // is not offered them.
  const restructure = isOrgAdmin(graph);
  // The delete gate counts rows on the team: an admin reaching it by descent holds none, and the
  // backend refuses a delete only for rows.
  const total = teamMemberCount(graph, teamId);
  const hasSubs = childrenOf(graph, teamId).length > 0;

  const teamActions: MenuItem[] = [
    { key: "rename", label: "Rename team", icon: "pencil", onClick: () => actions.renameTeam(teamId) },
    ...(restructure
      ? [
          { key: "move", label: "Move team…", icon: "move" as const, disabled: team.isRoot, onClick: () => actions.moveTeam(teamId) },
          {
            key: "delete",
            label: "Delete team",
            icon: "trash" as const,
            danger: true,
            separated: true,
            disabled: team.isRoot || total > 0 || hasSubs,
            onClick: () => actions.deleteTeam(teamId),
          },
        ]
      : []),
  ];

  const primary = (
    // Named on the button itself: the tightest panes hide the label (TeamDetail.module.css), and a
    // primary action that reads as a bare "button" is one nobody can identify.
    <Button className={styles.primary} aria-label="Add member" leftSection={<Icon name="userPlus" size={16} />} onClick={() => actions.addMember(teamId)}>
      Add member
    </Button>
  );

  // The detail swap rides the shared rise-in with a short 4px settle (theme/motion.css).
  return (
    <section
      className={cx(shell.detail, "alk-rise-in")}
      style={{ "--alk-rise-from": "4px" } as CSSProperties}
      key={teamId}
      aria-label={`${team.name} team`}
    >
      {/* The breadcrumb names the org's other teams, so a viewer who doesn't
          manage this one gets the header alone. */}
      {detail.status !== "forbidden" ? (
        <div className={shell.detailTop}>
          <Breadcrumb graph={graph} teamId={teamId} onSelect={actions.selectTeam} />
        </div>
      ) : null}

      <Masthead graph={graph} teamId={teamId} primaryAction={canManage ? primary : null} menuItems={canManage ? teamActions : []} />

      {detail.status === "loading" ? <SpreadSkeleton /> : null}
      {detail.status === "forbidden" ? (
        <EmptyState
          icon={<Icon name="shield" size={48} />}
          title="You don’t manage this team"
          body="Its roster and invitations are visible to the team’s admins and the admins above it."
          action={
            leads.length > 0 ? (
              <Stack gap={3} align="stretch">
                {leads.map((t) => (
                  <Button key={t.id} variant="secondary" onClick={() => actions.selectTeam(t.id)}>
                    Go to {t.name}
                  </Button>
                ))}
              </Stack>
            ) : undefined
          }
        />
      ) : null}
      {detail.status === "error" ? (
        <EmptyState
          tone="alert"
          icon={<Icon name="alert" size={48} />}
          title="We couldn’t load this team"
          body={detail.errorMessage ?? "Try again."}
          action={
            <Button variant="secondary" onClick={onRetry}>
              Try again
            </Button>
          }
        />
      ) : null}
      {detail.status === "ready" ? <TeamSpread graph={graph} teamId={teamId} teamName={team.name} actions={actions} /> : null}
    </section>
  );
}

/** The register spread (shown when the roster is loaded). Members is the roster ledger as the main
 *  column over the context column of governance / provenance / invitation plates; an installed
 *  extension adds sections beside it through TEAM_SECTIONS, and the tab strip shows only when it
 *  has more than one entry. */
function TeamSpread({ graph, teamId, teamName, actions }: { graph: Graph; teamId: string; teamName: string; actions: DetailActions }) {
  const dCount = directCount(graph, teamId);
  const iCount = descentCount(graph, teamId);
  const [scope, setScope] = useState<Scope>(dCount === 0 && iCount > 0 ? "descent" : "direct");
  // `?tab=<key>` opens on that section: the link org billing gives admins for where member
  // caps are set lands on the limits table rather than on the roster.
  const [searchParams] = useSearchParams();
  const [extra] = useState(() => TEAM_SECTIONS.items());
  const [section, setSection] = useState<string>(() => {
    const asked = searchParams.get("tab");
    return extra.some((s) => s.key === asked) ? (asked as string) : "members";
  });
  const chosen = extra.find((s) => s.key === section);

  return (
    <Stack gap={7} align="stretch">
      {extra.length > 0 ? (
        <Tabs
          items={[{ key: "members", label: "Members" }, ...extra.map((s) => ({ key: s.key, label: s.label }))]}
          value={section}
          onChange={setSection}
          label={`${teamName} sections`}
        />
      ) : null}
      {chosen ? (
        <chosen.Section teamId={teamId} />
      ) : (
        <>
          <div style={{ minWidth: 0 }}>
            <RosterPane graph={graph} teamId={teamId} teamName={teamName} scope={scope} setScope={setScope} dCount={dCount} iCount={iCount} actions={actions} />
          </div>
          <ContextColumn
            graph={graph}
            teamId={teamId}
            onSelectTeam={actions.selectTeam}
            onCreateSub={isOrgAdmin(graph) ? () => actions.createSub(teamId) : undefined}
            onRevoke={(inv) => actions.revokeInvite(teamId, inv)}
          />
        </>
      )}
    </Stack>
  );
}

/** The roster ledger — the main column. Its header is the "Members" eyebrow with the shown listing's
 *  count, then the scope + search toolbar, then the table (or its empty state). The two listings —
 *  direct members and members by descent — can overlap (an org admin added to a sub-team is in
 *  both), so the scope shows one at a time and the counts are per listing. The folio card owns the
 *  whole pane. */
function RosterPane({
  graph,
  teamId,
  teamName,
  scope,
  setScope,
  dCount,
  iCount,
  actions,
}: {
  graph: Graph;
  teamId: string;
  teamName: string;
  scope: Scope;
  setScope: (s: Scope) => void;
  dCount: number;
  iCount: number;
  actions: DetailActions;
}) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const base = useMemo(
    () => (scope === "direct" ? directRoster(graph, teamId) : descentRoster(graph, teamId)),
    [graph, teamId, scope],
  );
  const entries = q
    ? base.filter((e) => e.person.name.toLowerCase().includes(q) || e.person.email.toLowerCase().includes(q))
    : base;
  const shown = scope === "direct" ? dCount : iCount;

  return (
    <Card
      style={{ overflow: "hidden" }}
      variant="panel"
      region
      headerDivider
      icon={<Icon name="user" size={15} />}
      title="Members"
      headingLevel={3}
      actions={<Pill numeric tone="brand">{shown}</Pill>}
      bodyClassName={shell.folioBody}
    >
      <Toolbar
        hasScope={iCount > 0}
        scope={scope}
        onScope={setScope}
        directCount={dCount}
        descentCount={iCount}
        query={query}
        onQuery={setQuery}
        searchLabel={`Find a member in ${teamName}`}
      />
      {entries.length === 0 ? (
        q ? (
          <EmptyState size="md" icon={<Icon name="search" size={32} />} title="No members match" body={`No one in this view matches “${query.trim()}”.`} />
        ) : scope === "direct" && iCount > 0 ? (
          <EmptyState
            size="md"
            icon={<Icon name="descend" size={32} />}
            title="No direct members"
            body={`${iCount === 1 ? "One admin reaches" : `${iCount} admins reach`} this team by descent from above. Switch to By descent to see them.`}
            action={
              <Button variant="secondary" onClick={() => setScope("descent")}>
                By descent
              </Button>
            }
          />
        ) : (
          <EmptyState
            size="md"
            icon={<Icon name="user" size={32} />}
            title="No members yet"
            body="Add someone already in your organization, or invite a new person by email."
            action={
              <Button variant="secondary" leftSection={<Icon name="userPlus" size={16} />} onClick={() => actions.addMember(teamId)}>
                Add member
              </Button>
            }
          />
        )
      ) : (
        <Roster
          entries={entries}
          teamName={teamName}
          viewerId={graph.viewerId}
          canRemove={(personId) => canRemove(graph, teamId, personId)}
          canMove={moveTargets(graph, teamId).length > 0}
          onChangeRole={(personId, role) => actions.changeRole(teamId, personId, role)}
          onRemove={(entry) => actions.removeMember(teamId, entry)}
          onMove={(entry) => actions.moveMember(teamId, entry)}
          onSelectTeam={actions.selectTeam}
          onViewPlan={(entry) => actions.viewPlan(teamId, entry)}
        />
      )}
    </Card>
  );
}
