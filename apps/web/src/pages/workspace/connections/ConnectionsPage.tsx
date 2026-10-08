// Connections — everything the signed-in person's chats can reach.
//
// One list, two kinds of row: the connections they added for themselves, and the
// ones their teams configured for them. They are the same shape and the same
// daemon sync, so they belong in one place; the only thing that differs per row
// is whether this person may change it, which the server answers as `can_manage`
// and this page never re-derives.
//
// Adding is one dialog for both kinds — its first control is the owner picker —
// so there is no second "add a team connection" flow to keep in step. The team
// detail page links here with `?team=<id>` so an admin arriving from a team
// lands with that team already picked.

import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Button, Card, ConfirmDialog, EmptyState, Inline, Pill, Skeleton, Spinner, Stack, Table, ToastViewport, useToasts } from "@alkera/ui";
import { LogoChip, presentBadge, type BadgeSource, type ConnectionFormView } from "@alkera/ui/connections";
import { IconPlugConnected, IconPlus } from "@tabler/icons-react";

import { TopbarActions } from "../../../app/Topbar";

import { ConnectionDialog, type Owner } from "./ConnectionDialog";
import { RotateSecretModal } from "./RotateSecretModal";

import styles from "./connections.module.css";

import { ActionsMenu, type MenuItem } from "../../organization/teams/detail/actions";
import { useCurrentUser } from "../../../api/auth";
import {
  useConnectionForms,
  useDeleteMyConnection,
  useDeleteTeamConnection,
  useMyConnections,
  type TeamConnection,
} from "../../../api/connections";
import { useConnectionChecks } from "../../../api/connectionChecks";
import { refusalSentence } from "../../../api/errors";

/** The field name the deployment tier rides under, in the form and in the stored
 *  values alike. It is an ordinary field with an ordinary switch. */
const TIER_FIELD = "environment";

/** Whether the row menu offers rotation. Off while the flow is reworked — the
 *  modal and its mutation stay wired up, so turning it back on is this one
 *  word. Typed as a boolean so the branch below stays type-checked. */
const ROTATE_ENABLED: boolean = false;

// The ledger's columns: one fact per column, the trailing empty header being the
// per-row overflow menu. Each fixed track holds its own longest reading plus the
// 32px a cell spends on padding, so nothing clips at any width.
const COLUMNS = ["Connection", "Integration", "Tier", "Status", ""];
const COL_WIDTHS: Array<string | number | undefined> = [
  undefined,
  188, // "Amazon Redshift", plus the 24px brand mark and its gap
  148, // "Members choose"
  176, // "Members' own logins"
  188, // "Sign in with the CLI", the longest badge label, plus the pill's own padding
  64,
];

/** One group of rows under the owner they belong to. "Yours" first, then each
 *  team — the org root before the teams inside it, so the list reads outward
 *  from the person to the org. */
interface Group {
  key: string;
  title: string;
  caption: string;
  rows: TeamConnection[];
}

export function groupByOwner(rows: TeamConnection[], orgTeamId?: string): Group[] {
  const mine = rows.filter((r) => r.owner_user_id);
  const byTeam = new Map<string, TeamConnection[]>();
  for (const row of rows) {
    if (row.owner_user_id) continue;
    const list = byTeam.get(row.team_id) ?? [];
    list.push(row);
    byTeam.set(row.team_id, list);
  }
  // A team's name is on every row it owns, so the group heading needs no second
  // read of the team list.
  const teamGroups = [...byTeam.entries()]
    .map(([teamId, list]) => ({
      key: teamId,
      title: list[0]?.team_name || "Team",
      // A team group says nothing under its heading: the row menu is the
      // authority on what this person may change, and it already is per row.
      caption: "",
      rows: list,
    }))
    // The org's own connections read before any team's — everyone in the org has
    // them, so they are the widest thing on the page — then the teams by name.
    .sort((a, b) => {
      if (a.key === orgTeamId) return -1;
      if (b.key === orgTeamId) return 1;
      return a.title.localeCompare(b.title);
    });
  return [
    ...(mine.length > 0
      ? [
          {
            key: "__me__",
            title: "Yours",
            caption: "",
            rows: mine,
          },
        ]
      : []),
    ...teamGroups,
  ];
}

/** The tier a connection carries, as a cell. A connection whose tier was left for
 *  members to answer has no single tier, and says so. */
function tierCell(c: TeamConnection, labels: Record<string, string>): string {
  const tier = c.shared_values?.[TIER_FIELD] ?? "";
  if (!tier) return c.owner_user_id ? "—" : "Members choose";
  return labels[tier] ?? tier;
}


/** The one derived status, rendered the one way. The badge is the SERVER's
 *  reading and `presentBadge` turns it into the words every Alkera surface shows
 *  for it, so this page cannot invent a vocabulary of its own. */
function StatusPill({ connection }: { connection: TeamConnection }) {
  const { label, tone, busy } = presentBadge(connection as BadgeSource);
  return (
    <Pill tone={tone} icon={busy ? <Spinner size={13} /> : undefined}>
      {label}
    </Pill>
  );
}

/** What each member's own sign-in adds up to on a members-sign-in row. Absent
 *  where the server sent no counts, and the second clause is dropped when nobody
 *  is locked out. */
function membersCaption(members: TeamConnection["members"]): string {
  if (!members) return "";
  const signedIn = `${members.authorized} signed in`;
  if (members.needs_reauth === 0) return signedIn;
  const stuck =
    members.needs_reauth === 1 ? "1 needs to sign in" : `${members.needs_reauth} need to sign in`;
  return `${signedIn} · ${stuck}`;
}

/** The sentence under a row's name: why it reads the way it does, how its members
 *  stand where each of them signs in, and who put it there. A team row always
 *  names its author — "who added it" is the question a member asks first about a
 *  connection they did not create. */
function RowNote({ connection }: { connection: TeamConnection }) {
  const { note, tone } = presentBadge(connection as BadgeSource);
  const members = membersCaption(connection.members);
  const author = connection.owner_user_id
    ? ""
    : connection.created_by_name
      ? `Added by ${connection.created_by_name}`
      : "";
  if (!note && !members && !author) return null;
  return (
    <>
      {note ? (
        // The connector's own sentence, whole. A driver's refusal lists every
        // host it tried and says why on its last line, so it wraps — and keeps
        // the driver's line breaks — rather than clipping.
        <div
          className={`${tone === "danger" ? "alk-caption alk-danger" : "alk-caption"} ${styles.driverDetail}`}
        >
          {note}
        </div>
      ) : null}
      {members ? <div className="alk-caption">{members}</div> : null}
      {author ? <div className="alk-caption">{author}</div> : null}
    </>
  );
}

export function ConnectionsPage() {
  const list = useMyConnections();
  const forms = useConnectionForms(true);
  const me = useCurrentUser();
  const [params, setParams] = useSearchParams();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<TeamConnection | null>(null);
  const [removing, setRemoving] = useState<TeamConnection | null>(null);
  const [rotating, setRotating] = useState<TeamConnection | null>(null);
  const { toasts, push, dismiss } = useToasts();

  // The team page links here with the team it came from, so an admin arriving
  // from a team lands with that team already picked rather than on "Just me".
  const requestedTeam = params.get("team");
  const [owner, setOwner] = useState<Owner>(
    requestedTeam ? { kind: "team", teamId: requestedTeam } : { kind: "me" },
  );

  const orgTeamId = me.data?.org_team_id;
  const groups = useMemo(() => groupByOwner(list.data ?? [], orgTeamId), [list.data, orgTeamId]);

  const titleOf = (plugin: string) =>
    (forms.data?.connectors ?? []).find((d) => d.name === plugin)?.title ?? plugin;
  const tierLabels: Record<string, string> = ((forms.data?.connectors ?? [])
    .flatMap((d) => (d.form as unknown as ConnectionFormView).trailing_fields ?? [])
    .find((f) => f.name === TIER_FIELD)?.enum_labels ?? {}) as Record<string, string>;

  const openAdd = () => {
    // Re-read the link every time the dialog opens. The seed below only ever
    // runs once, so an add opened after the consumed `?team=` was deleted — or
    // after a different team sent the admin here — would otherwise re-pick the
    // team the URL no longer names.
    setOwner(requestedTeam ? { kind: "team", teamId: requestedTeam } : { kind: "me" });
    setEditing(null);
    setDialogOpen(true);
  };
  const openEdit = (c: TeamConnection) => {
    setOwner(c.owner_user_id ? { kind: "me" } : { kind: "team", teamId: c.team_id });
    setEditing(c);
    setDialogOpen(true);
  };
  const closeDialog = () => {
    setDialogOpen(false);
    setEditing(null);
    // The deep link has been consumed; leaving it would re-pick that team the
    // next time the dialog opens, long after the admin left the team page.
    if (requestedTeam) {
      const next = new URLSearchParams(params);
      next.delete("team");
      setParams(next, { replace: true });
    }
  };

  return (
    <Stack gap={5} align="stretch">
      {/* No heading of its own: the topbar already names the route, and the
          add key rides beside that name like New chat does — except on a page
          with nothing on it, where the whole page is the invitation to add one
          and a second copy of the same key in the masthead is one control too
          many. */}
      {list.isLoading || groups.length > 0 ? (
        <TopbarActions>
          <Button variant="primary" leftSection={<IconPlus size={15} stroke={1.8} />} onClick={openAdd}>
            Add connection
          </Button>
        </TopbarActions>
      ) : null}

      {list.isLoading ? (
        <Stack gap={3} align="stretch">
          <Skeleton height={18} width={160} />
          <Skeleton height={120} />
        </Stack>
      ) : groups.length === 0 ? (
        // One fact and one action. Who a connection belongs to is the first
        // question the dialog itself asks, so saying it here only puts a
        // paragraph between the reader and the button they came for.
        <EmptyState
          icon={<IconPlugConnected size={28} stroke={1.5} />}
          title="No connections yet"
          action={
            <Button variant="primary" leftSection={<IconPlus size={15} stroke={1.8} />} onClick={openAdd}>
              Add connection
            </Button>
          }
        />
      ) : (
        groups.map((group) => (
          <Card
            key={group.key}
            variant="panel"
            headerDivider
            icon={<IconPlugConnected size={15} stroke={1.8} />}
            title={group.title}
            headingLevel={2}
            actions={
              <Pill numeric tone="brand">
                {group.rows.length}
              </Pill>
            }
          >
            <Stack gap={3} align="stretch">
              {group.caption ? <p className="alk-caption">{group.caption}</p> : null}
              <Table
                style={{ width: "100%" }}
                columns={COLUMNS}
                colWidths={COL_WIDTHS}
                responsive
                stackAt={1000}
                pageSize={10}
              >
                {group.rows.map((c) => (
                  <ConnectionRow
                    key={c.id}
                    connection={c}
                    title={titleOf(c.plugin)}
                    tier={tierCell(c, tierLabels)}
                    onEdit={() => openEdit(c)}
                    onRemove={() => setRemoving(c)}
                    onRotate={() => setRotating(c)}
                    onFailure={(title, message) => push({ tone: "danger", title, message })}
                  />
                ))}
              </Table>
            </Stack>
          </Card>
        ))
      )}

      <ConnectionDialog
        open={dialogOpen}
        owner={owner}
        onOwnerChange={setOwner}
        editing={editing}
        onClose={closeDialog}
      />
      <RemovePrompt connection={removing} onClose={() => setRemoving(null)} />
      <RotateSecretModal connection={rotating} onClose={() => setRotating(null)} />
      <ToastViewport toasts={toasts} onDismiss={dismiss} position="bottom-center" />
    </Stack>
  );
}

/** One row. Every action here is gated on the server by team admin — verifying
 *  included, because a re-check dials the customer's warehouse with the stored
 *  credential rather than reading anything Alkera already holds. So the whole
 *  menu hangs off `can_manage`, and a row a person may use but not manage
 *  carries no menu at all: an affordance that always answers 403 is worse than
 *  none. Widening the re-check to an entitled member is a server change first. */
function ConnectionRow({
  connection: c,
  title,
  tier,
  onEdit,
  onRemove,
  onRotate,
  onFailure,
}: {
  connection: TeamConnection;
  title: string;
  tier: string;
  onEdit: () => void;
  onRemove: () => void;
  onRotate: () => void;
  onFailure: (title: string, message: string) => void;
}) {
  const { checks, installed: checked } = useConnectionChecks();
  const verifyTeam = checks.useRerunTeam(c.team_id);
  const verifyMine = checks.useRerunMine();
  const verify = c.owner_user_id ? verifyMine : verifyTeam;
  const checking = verify.isPending;

  const actions: MenuItem[] = c.can_manage
    ? [
        // Only where an installed extension checks connections: the open platform has
        // no check to run.
        ...(checked
          ? [
              {
                key: "verify",
                label: checking ? "Verifying…" : "Verify now",
                icon: "refresh" as const,
                disabled: checking,
                onClick: () =>
                  verify.mutate(c.id, {
                    onError: (err) =>
                      onFailure(
                        `Couldn't check ${c.handle}`,
                        refusalSentence(err, { fallback: "Couldn't start the check." }),
                      ),
                  }),
              },
            ]
          : []),
        { key: "edit", label: "Edit…", icon: "pencil" as const, onClick: onEdit },
        // Rotation stays where the server actually holds a credential — a
        // sign-in-yourself row has nothing to replace.
        ...(ROTATE_ENABLED && c.auth_mode === "shared"
          ? [{ key: "rotate", label: "Rotate credential…", icon: "key" as const, onClick: onRotate }]
          : []),
        {
          key: "remove",
          label: "Remove…",
          icon: "trash" as const,
          danger: true,
          separated: true,
          onClick: onRemove,
        },
      ]
    : [];

  return (
    <tr>
      <td>
        <div style={{ minWidth: 0 }}>
          <div className="alk-strong">{c.handle}</div>
          <RowNote connection={c} />
        </div>
      </td>
      <td style={{ color: "var(--alkSecondaryText)", whiteSpace: "nowrap" }}>
        <Inline gap={3} wrap={false}>
          <LogoChip pluginId={c.plugin} size={24} />
          {title}
        </Inline>
      </td>
      <td style={{ color: "var(--alkSecondaryText)", whiteSpace: "nowrap" }}>{tier}</td>
      <td>
        <StatusPill connection={c} />
      </td>
      <td style={{ textAlign: "right" }}>
        {actions.length > 0 ? (
          <ActionsMenu items={actions} label={`Actions for ${c.handle}`} />
        ) : null}
      </td>
    </tr>
  );
}

/** Removing is destructive and irreversible, and for a team row it is
 *  destructive for everyone below it, so it always confirms first. */
function RemovePrompt({
  connection,
  onClose,
}: {
  connection: TeamConnection | null;
  onClose: () => void;
}) {
  const removeTeam = useDeleteTeamConnection(connection?.team_id ?? "");
  const removeMine = useDeleteMyConnection();
  const remove = connection?.owner_user_id ? removeMine : removeTeam;
  const personal = Boolean(connection?.owner_user_id);
  return (
    <ConfirmDialog
      open={connection !== null}
      onClose={onClose}
      onConfirm={() => {
        if (connection) remove.mutate(connection.id, { onSettled: onClose });
      }}
      title={connection ? `Remove ${connection.handle}?` : ""}
      consequence={
        personal
          ? "Your chats lose this connection, credential included, on their next sync. This can't be undone."
          : "This deletes the connection for the whole team. Every member using it loses it on their next sync. This can't be undone."
      }
      confirmLabel={personal ? "Remove" : "Remove for all members"}
      tone="destructive"
      busy={remove.isPending}
    />
  );
}
