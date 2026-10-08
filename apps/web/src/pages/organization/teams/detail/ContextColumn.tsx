import { Fragment, type ReactNode } from "react";

import { Button, Card, IconChip, Inline, Pill, Stack, cx } from "@alkera/ui";

import { TEAM_PLATES, placeAfter } from "../../../../app/extensions/portal";
import { Icon, type IconName } from "../../../../app/icons";
import {
  childrenOf,
  descentAdmins,
  directAdmins,
  invitesFor,
  memberCountLabel,
  teamById,
  viewerDescentSource,
  type Graph,
  type Invite,
} from "../data/model";
import styles from "./ContextColumn.module.css";

/** The context column — three always-present register plates beside the roster, so the detail
 *  reads as a filled spread for a 1-member leaf as much as an 18-member root. Each plate carries
 *  one of the team's facts that the roster can't: permission descent (↓), membership inheritance
 *  (↑ + the contributing sub-teams), and pending invitations. Every plate has a populated AND a
 *  compact intentional-empty state, so the column never collapses to a void. An installed
 *  extension adds its own plates (the team's data connections) through `TEAM_PLATES`. */
export function ContextColumn({
  graph,
  teamId,
  onSelectTeam,
  onCreateSub,
  onRevoke,
}: {
  graph: Graph;
  teamId: string;
  onSelectTeam: (id: string) => void;
  /** Omitted when the viewer may not create teams (an org-admin decision). */
  onCreateSub?: () => void;
  onRevoke: (invite: Invite) => void;
}) {
  return (
    // A single full-width column of plates, in the descent order: permission descends (↓), then
    // membership rises (↑) with its sub-teams, then pending invitations. One column has no
    // column-balance to go ragged — each plate spans the pane and fills its width with a multi-column
    // body (the sub-team list, the invite list), so a 1-member leaf reads as composed as the root.
    <Stack as="aside" gap={7} align="stretch" aria-label="Team governance, provenance, and invitations">
      {placeAfter(
        [
          { key: "governance", node: <GovernancePlate graph={graph} teamId={teamId} onSelectTeam={onSelectTeam} /> },
          {
            key: "provenance",
            node: <ProvenancePlate graph={graph} teamId={teamId} onSelectTeam={onSelectTeam} onCreateSub={onCreateSub} />,
          },
          { key: "invitations", node: <InvitationsPlate graph={graph} teamId={teamId} onRevoke={onRevoke} /> },
        ],
        TEAM_PLATES.items().map(({ key, after, Plate }) => ({
          item: { key, node: <Plate teamId={teamId} teamName={teamById(graph, teamId)?.name ?? "this team"} /> },
          after,
          label: key,
        })),
        (plate) => plate.key,
      ).map((plate) => (
        <Fragment key={plate.key}>{plate.node}</Fragment>
      ))}
    </Stack>
  );
}

/** A register plate — a titled card. The title is a small-caps eyebrow with a leading glyph; the
 *  body is the plate's facts. The eyebrow reading (a count) rides the header, never a stacked line.
 *  `bodyClassName` lets a plate (the descent card) carry its own body treatment. */
function Plate({
  glyph,
  title,
  reading,
  bodyClassName,
  children,
}: {
  glyph: IconName;
  title: string;
  reading?: number;
  bodyClassName?: string;
  children: ReactNode;
}) {
  return (
    <Card
      variant="panel"
      headerDivider
      icon={<Icon name={glyph} size={15} />}
      title={title}
      headingLevel={3}
      actions={reading != null ? <Pill numeric tone="brand">{reading}</Pill> : undefined}
      bodyClassName={bodyClassName}
    >
      {children}
    </Card>
  );
}

/** Permission flows DOWN — who governs this team from above. Always present (every team is governed,
 *  the root governs itself). The body is the accent-tinted descent card — a wash with a 2px accent
 *  left-border and a descent glyph leading each governance line. */
function GovernancePlate({ graph, teamId, onSelectTeam }: { graph: Graph; teamId: string; onSelectTeam: (id: string) => void }) {
  const team = teamById(graph, teamId);
  const isRoot = Boolean(team?.isRoot);
  const teamName = team?.name ?? "this team";
  const dsource = viewerDescentSource(graph, teamId);
  const ownAdmins = directAdmins(graph, teamId);
  const above = descentAdmins(graph, teamId);

  return (
    <Plate glyph="descend" title="Permissions" bodyClassName={styles.descent}>
      <DescentLine glyph={isRoot ? "branch" : "descent"}>
        {isRoot ? (
          <>The organization root. Its admins govern <b>every</b> team below by descent.</>
        ) : dsource ? (
          <>
            You administer <b>{teamName}</b> by descent from{" "}
            <FromLink id={dsource.id} name={dsource.name} onSelectTeam={onSelectTeam} />.
          </>
        ) : (
          <>You administer <b>{teamName}</b> directly.</>
        )}
      </DescentLine>
      {above.length > 0 && !isRoot ? (
        <DescentLine glyph="descent">
          {above.length === 1 ? "One admin reaches" : <><b className="alk-num">{above.length}</b> admins reach</>} this team by descent.
          Their standing here is set on the team it comes from.
        </DescentLine>
      ) : null}
      {ownAdmins.length > 0 && !isRoot ? (
        <DescentLine glyph="descent">
          Led directly by{" "}
          {ownAdmins.map((p, i) => (
            <Fragment key={p.id}>
              {i > 0 ? (i === ownAdmins.length - 1 ? " and " : ", ") : ""}
              <b>{p.name}</b>
            </Fragment>
          ))}
          .
        </DescentLine>
      ) : null}
    </Plate>
  );
}

/** One governance line in the descent card — a leading accent glyph pinned to the first text line,
 *  optically centred on the line's cap height, then the prose. */
function DescentLine({ glyph, children }: { glyph: IconName; children: ReactNode }) {
  return (
    <p className={styles.descentLine}>
      <span className={styles.descentGlyph} aria-hidden="true">
        <Icon name={glyph} size={16} />
      </span>
      <span>{children}</span>
    </p>
  );
}

/** The sub-teams beneath this one, as navigable rows, each with its direct member count (the wire's
 *  count of rows on it — this team's admins reach every one of them by descent without a row); a
 *  leaf with no sub-teams shows the intentional-empty branch with the create action. */
function ProvenancePlate({
  graph,
  teamId,
  onSelectTeam,
  onCreateSub,
}: {
  graph: Graph;
  teamId: string;
  onSelectTeam: (id: string) => void;
  onCreateSub?: () => void;
}) {
  const team = teamById(graph, teamId);
  const teamName = team?.name ?? "this team";
  const kids = childrenOf(graph, teamId);
  const create = onCreateSub ? (
    <Button variant="secondary" style={{ alignSelf: "flex-start" }} leftSection={<Icon name="plus" size={15} />} onClick={onCreateSub}>
      Create sub-team
    </Button>
  ) : null;

  if (kids.length === 0) {
    return (
      <Plate glyph="branch" title="Sub-teams">
        {onCreateSub ? null : <p className={cx("alk-body", "alk-muted")}>{teamName} has no sub-teams.</p>}
        {create}
      </Plate>
    );
  }

  return (
    <Plate glyph="branch" title="Sub-teams" reading={kids.length}>
      <p className={cx("alk-body", "alk-muted")}>
        {kids.length === 1 ? (
          <>One sub-team under {teamName}. Its admins reach it by descent.</>
        ) : (
          <>
            <b className={cx("alk-num", "alk-strong")}>{kids.length}</b> sub-teams under {teamName}. Its admins reach each of them by descent.
          </>
        )}
      </p>
      <ul className={styles.subs}>
        {kids.map((child) => {
          const grandKids = childrenOf(graph, child.id).length;
          return (
            <li key={child.id}>
              <button type="button" className={styles.subrow} onClick={() => onSelectTeam(child.id)}>
                <IconChip size={30} tone="neutral">
                  <Icon name="branch" size={16} />
                </IconChip>
                <Stack as="span" grow>
                  <span className={cx("alk-strong", "alk-truncate")}>{child.name}</span>
                  <span className={cx("alk-meta", "alk-tnum")}>
                    {memberCountLabel(child.memberCount)}
                    {grandKids > 0 ? ` · ${grandKids} sub` : ""}
                  </span>
                </Stack>
                <Inline as="span" style={{ flex: "0 0 auto", color: "var(--alkSecondaryText)" }} aria-hidden="true">
                  <Icon name="chevronRight" size={15} />
                </Inline>
              </button>
            </li>
          );
        })}
      </ul>
      {create}
    </Plate>
  );
}

/** Pending invitations to this team — a compact list, or its intentional-empty branch. Inviting
 *  someone happens in the Add member dialog (it searches the org and offers the email invitation for
 *  an address nobody holds), so this plate reads the outstanding invitations and revokes them. */
function InvitationsPlate({
  graph,
  teamId,
  onRevoke,
}: {
  graph: Graph;
  teamId: string;
  onRevoke: (invite: Invite) => void;
}) {
  const invites = invitesFor(graph, teamId);

  if (invites.length === 0) {
    return (
      <Plate glyph="mail" title="Pending invitations">
        <p className={cx("alk-body", "alk-muted")}>
          No one is waiting to join.
        </p>
      </Plate>
    );
  }

  return (
    <Plate glyph="mail" title="Pending invitations" reading={invites.length}>
      {/* A single-column list of rows — the invitee (email + when it lapses) reads on the left, the
          two moves sit on the right. A ruled list, not a grid of cards, so the column stays calm and
          a long email gets the full row width to truncate in. */}
      <ul className={styles.invites}>
        {invites.map((inv) => (
          <li key={inv.id} className={styles.invite}>
            <Stack grow>
              <span className={cx("alk-name", "alk-truncate")}>{inv.email}</span>
              <span className={cx("alk-meta", "alk-tnum", "alk-warning")}>Expires {inv.expires}</span>
            </Stack>
            <Inline gap={3} wrap={false} style={{ flex: "0 0 auto" }}>
              <Button variant="destructive" fill="outline" leftSection={<Icon name="trash" size={15} />} onClick={() => onRevoke(inv)}>
                Revoke
              </Button>
            </Inline>
          </li>
        ))}
      </ul>
      {/* The single-org rule, stated as the rule: whether a given address already belongs to
          another organization is never told to the inviter. */}
      <p className={cx("alk-meta", "alk-muted")}>An address that already belongs to another organization can’t accept.</p>
    </Plate>
  );
}

function FromLink({ id, name, onSelectTeam }: { id: string; name: string; onSelectTeam: (id: string) => void }) {
  return (
    <button type="button" className="alk-link" onClick={() => onSelectTeam(id)}>
      {name}
    </button>
  );
}
