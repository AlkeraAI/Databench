import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { Button, ConfirmDialog, EmptyState, Pill, SidePanel, Stack, ToastViewport, useToasts } from "@alkera/ui";

import { Icon } from "../../../app/icons";
import { TopbarActions, TopbarSubtitle } from "../../../app/Topbar";
import { refusalSentence } from "../../../api/errors";
import {
  useAddMemberMutation,
  useChangeRoleMutation,
  useCreateInvitationMutation,
  useCreateTeamMutation,
  useDeleteTeamMutation,
  useMoveMemberMutation,
  useMoveTeamMutation,
  useMyInvitations,
  useRemoveMemberMutation,
  useRenameTeamMutation,
  useRevokeInvitationMutation,
  useTeamMembers,
} from "../../../api/teams";
import { toDirectory } from "./data/adapt";
import { TeamDetail, type DetailActions } from "./detail/TeamDetail";
import { TeamTree } from "./overlays/TeamTree";
import { MemberDrawer, type PlanTarget } from "./overlays/MemberDrawer";
import { InvitesPanel } from "./overlays/InvitesPanel";
import { DetailSkeleton } from "./detail/Skeleton";
import {
  AddMemberModal,
  CreateTeamModal,
  MoveMemberModalPick,
  RenameModal,
  TeamPickModal,
  type TeamChoice,
} from "./overlays/modals";
import {
  ancestorsOf,
  descendantsOf,
  removalCascade,
  descentRoster,
  directRoster,
  invitesFor,
  teamById,
  teamMemberCount,
  type Graph,
  type Invite,
  type Role,
  type RosterEntry,
  type Team,
} from "./data/model";
import { administeredTops, isOrgAdmin, landingFor, moveTargets } from "./data/permissions";
import { useLiveTeamsSync, useTeamsData } from "./data/provider";
import shell from "./teams.module.css";
import styles from "./TeamsPage.module.css";

type ModalState =
  | { kind: "create"; parentId: string; parentName?: string }
  | { kind: "add"; teamId: string }
  | { kind: "rename"; teamId: string }
  | { kind: "move-team"; teamId: string }
  | { kind: "delete"; teamId: string }
  | { kind: "move-member"; teamId: string; entry: RosterEntry }
  // Removal from a sub-team cascades: the person also leaves every team under it, and any team
  // above it they belong to only through it. Confirmed and said, not fired on one click.
  | { kind: "remove"; teamId: string; entry: RosterEntry }
  // Removing someone from the ORG ROOT is deprovisioning, not a team edit: the
  // backend deactivates the account, revokes every live session and CLI token,
  // and cancels their outstanding invitations. Same control, very different
  // blast radius, so the root case is confirmed and named rather than fired on
  // one click like an ordinary sub-team removal.
  | { kind: "deprovision"; teamId: string; entry: RosterEntry };

function ancestorsAndSelf(g: Graph, id: string): string[] {
  return [...ancestorsOf(g, id).map((t) => t.id), id];
}

const errMessage = (e: unknown): string =>
  refusalSentence(e);

/**
 * Teams — the org's survey register, wired to the real backend. The navigable
 * Team Tree index is an on-demand side panel; the selected team's roster +
 * governance read through the data SEAM (provider.tsx). Every action calls a real
 * mutation and reports success or the server's error in a toast; the roster
 * endpoint is admin-gated, so a non-admin reaches a first-class "read-only"
 * detail rather than a blank pane. A product surface.
 */
export function TeamsPage() {
  // The selected team IS the route param (/teams/:teamId), so deep links land on
  // the right team and picking one updates the URL. No param = the org root.
  const { teamId: selectedId } = useParams<{ teamId: string }>();
  const navigate = useNavigate();
  // The invitations panel IS the ?tab=invites param — invitation emails deep-link
  // to it, and in-page open/close writes the param (replace: no history spam).
  const [searchParams, setSearchParams] = useSearchParams();
  const invitesOpen = searchParams.get("tab") === "invites";
  const setInvitesOpen = (open: boolean) =>
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (open) next.set("tab", "invites");
        else next.delete("tab");
        return next;
      },
      { replace: true },
    );

  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [treeOpen, setTreeOpen] = useState(false);
  const [planTarget, setPlanTarget] = useState<PlanTarget | null>(null);

  const [modal, setModal] = useState<ModalState | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const { toasts, push, dismiss } = useToasts();
  const seededExpand = useRef(false);

  // Run the live source for the current selection (it drives the fetch) and read the resolved state
  // back from the store; the seam keeps the two decoupled.
  useLiveTeamsSync(selectedId);
  const { status, graph, detail, errorMessage, retry } = useTeamsData();
  // With no team in the URL the page opens where the viewer's standing lands them: the root for an
  // org admin, the team a sub-team admin leads, or a choice between the teams they lead.
  const landing = graph ? landingFor(graph) : null;
  const effectiveId = selectedId ?? (landing?.kind === "team" ? landing.teamId : undefined);
  const orgAdmin = graph ? isOrgAdmin(graph) : false;
  const leads = graph ? administeredTops(graph) : [];

  // Mutations — each reports its outcome in a toast; the hooks own cache invalidation.
  const createMut = useCreateTeamMutation();
  const renameMut = useRenameTeamMutation();
  const moveTeamMut = useMoveTeamMutation();
  const deleteMut = useDeleteTeamMutation();
  const addMut = useAddMemberMutation();
  const roleMut = useChangeRoleMutation();
  const removeMut = useRemoveMemberMutation();
  const moveMemberMut = useMoveMemberMutation();
  const inviteMut = useCreateInvitationMutation();
  const revokeMut = useRevokeInvitationMutation();

  // The org directory for the "add member" picker — the root team's roster (every org member
  // holds a row on the root, and nothing reaches the root by descent). Fetched only while the
  // modal is open.
  // Admin-gated like every roster: only an org admin can read the root's, so a sub-team admin adds
  // people by address (an address already in the org joins at once).
  const directoryQuery = useTeamMembers(graph?.rootId, false, modal?.kind === "add" && Boolean(graph && isOrgAdmin(graph)));
  const directory = useMemo(() => toDirectory(directoryQuery.data ?? []), [directoryQuery.data]);

  // The viewer's OWN pending invitations — drives the topbar Invitations action + its count badge.
  const inviteCount = useMyInvitations().data?.length ?? 0;

  // Seed the tree's expansion to the first selected team's chain, once.
  useEffect(() => {
    if (graph && effectiveId && !seededExpand.current) {
      seededExpand.current = true;
      setExpanded(new Set(ancestorsAndSelf(graph, effectiveId)));
    }
  }, [graph, effectiveId]);

  const pushToast = (text: string, tone: "success" | "error" = "success") =>
    push({ message: text, tone: tone === "error" ? "danger" : "success" });
  const fail = (e: unknown) => pushToast(errMessage(e), "error");

  const openModal = (m: ModalState) => {
    setModal(m);
    setModalOpen(false);
    requestAnimationFrame(() => requestAnimationFrame(() => setModalOpen(true)));
  };
  const closeModal = () => {
    setModalOpen(false);
    window.setTimeout(() => setModal(null), 200);
  };

  const selectTeam = (id: string) => {
    navigate(`/teams/${id}`);
    setExpanded((prev) => {
      const next = new Set(prev);
      if (graph) for (const a of ancestorsAndSelf(graph, id)) next.add(a);
      return next;
    });
    setTreeOpen(false);
  };
  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const teamName = (id: string) => (graph ? teamById(graph, id)?.name ?? "team" : "team");
  const personName = (id: string) => graph?.people[id]?.name ?? "the member";

  // ---- mutations wired to the real API (toast on success / server error) ----
  const changeRole = (teamId: string, personId: string, role: Role) =>
    roleMut.mutate(
      { teamId, userId: personId, role },
      { onSuccess: () => pushToast(`Updated ${personName(personId)}’s role to ${role === "admin" ? "Admin" : "Member"}.`), onError: fail },
    );

  const deprovision = (teamId: string, entry: RosterEntry) => {
    closeModal();
    removeMut.mutate(
      { teamId, userId: entry.person.id },
      {
        onSuccess: () => pushToast(`Deactivated ${entry.person.name} and signed them out everywhere.`),
        onError: fail,
      },
    );
  };

  const removeMember = (teamId: string, entry: RosterEntry) => {
    // At the org root this deactivates the account rather than editing a team; below it the
    // removal cascades. Both are confirmed, each saying what it does.
    openModal(graph?.rootId === teamId ? { kind: "deprovision", teamId, entry } : { kind: "remove", teamId, entry });
  };

  const confirmRemove = (teamId: string, entry: RosterEntry) => {
    closeModal();
    removeMut.mutate(
      { teamId, userId: entry.person.id },
      { onSuccess: () => pushToast(`Removed ${entry.person.name} from ${teamName(teamId)}.`), onError: fail },
    );
  };

  const addMember = (teamId: string, personId: string, role: Role) => {
    closeModal();
    addMut.mutate(
      { teamId, userId: personId, role },
      { onSuccess: () => pushToast(`Added ${personName(personId)} to ${teamName(teamId)}.`), onError: fail },
    );
  };

  // The invitation is sent from the add-member dialog, so its refusal belongs there: a cross-org
  // address is the one case the dialog cannot rule out before asking, and the admin needs to read
  // why while the address they typed is still on screen.
  const sendInvite = async (teamId: string, email: string, role: Role): Promise<string | null> => {
    try {
      await inviteMut.mutateAsync({ teamId, email, role });
      closeModal();
      pushToast(`Invitation sent to ${email}.`);
      return null;
    } catch (e) {
      return errMessage(e);
    }
  };

  const createTeam = (parentId: string, name: string) => {
    closeModal();
    createMut.mutate(
      { name, parent_team_id: parentId },
      {
        onSuccess: (created) => {
          navigate(`/teams/${created.id}`);
          setExpanded((prev) => new Set(prev).add(parentId));
          pushToast(`Created ${name}.`);
        },
        onError: fail,
      },
    );
  };

  const renameTeam = (teamId: string, name: string) => {
    closeModal();
    renameMut.mutate({ teamId, name }, { onSuccess: () => pushToast(`Renamed to ${name}.`), onError: fail });
  };

  const moveTeam = (teamId: string, newParentId: string) => {
    closeModal();
    moveTeamMut.mutate(
      { teamId, newParentTeamId: newParentId },
      {
        onSuccess: () => {
          setExpanded((prev) => new Set(prev).add(newParentId));
          pushToast(`Moved ${teamName(teamId)} under ${teamName(newParentId)}.`);
        },
        onError: fail,
      },
    );
  };

  const deleteTeam = (teamId: string) => {
    const parentId = (graph && teamById(graph, teamId)?.parentId) || graph?.rootId;
    const name = teamName(teamId);
    closeModal();
    deleteMut.mutate(teamId, {
      onSuccess: () => {
        if (parentId) navigate(`/teams/${parentId}`);
        pushToast(`Deleted ${name}.`);
      },
      onError: fail,
    });
  };

  const moveMember = (fromTeamId: string, entry: RosterEntry, destTeamId: string) => {
    closeModal();
    moveMemberMut.mutate(
      { teamId: fromTeamId, userId: entry.person.id, targetTeamId: destTeamId, role: entry.role },
      { onSuccess: () => pushToast(`Moved ${entry.person.name} to ${teamName(destTeamId)}.`), onError: fail },
    );
  };

  const revokeInvite = (teamId: string, invite: Invite) =>
    revokeMut.mutate(
      { teamId, invitationId: invite.id },
      { onSuccess: () => pushToast(`Invitation to ${invite.email} revoked.`), onError: fail },
    );

  const detailActions: DetailActions = {
    selectTeam,
    changeRole,
    removeMember,
    moveMember: (teamId, entry) => openModal({ kind: "move-member", teamId, entry }),
    addMember: (teamId) => openModal({ kind: "add", teamId }),
    createSub: (parentId) => openModal({ kind: "create", parentId, parentName: teamName(parentId) }),
    renameTeam: (teamId) => openModal({ kind: "rename", teamId }),
    moveTeam: (teamId) => openModal({ kind: "move-team", teamId }),
    deleteTeam: (teamId) => openModal({ kind: "delete", teamId }),
    revokeInvite,
    viewPlan: (teamId, entry) => setPlanTarget({ teamId, teamName: teamName(teamId), person: entry.person }),
  };

  const moveTeamChoices = useMemo<TeamChoice[]>(() => {
    if (!graph || modal?.kind !== "move-team") return [];
    const blocked = new Set([modal.teamId, ...descendantsOf(graph, modal.teamId).map((t) => t.id)]);
    const current = teamById(graph, modal.teamId)?.parentId;
    return graph.teams
      .filter((t) => !blocked.has(t.id) && t.id !== current)
      .map((t) => ({ id: t.id, name: t.name, depth: ancestorsOf(graph, t.id).length, members: teamMemberCount(graph, t.id) }))
      .sort((a, b) => a.depth - b.depth || a.name.localeCompare(b.name));
  }, [modal, graph]);

  const moveMemberChoices = useMemo<TeamChoice[]>(() => {
    if (!graph || modal?.kind !== "move-member") return [];
    return moveTargets(graph, modal.teamId)
      .map((t) => ({ id: t.id, name: t.name, depth: ancestorsOf(graph, t.id).length, members: teamMemberCount(graph, t.id) }))
      .sort((a, b) => a.depth - b.depth || a.name.localeCompare(b.name));
  }, [modal, graph]);

  const selectedTeam = graph && effectiveId ? teamById(graph, effectiveId) : undefined;
  const deleteTarget = graph && modal?.kind === "delete" ? teamById(graph, modal.teamId) : undefined;

  // The topbar actions survey the org (the tree names every team) and manage invitations, so they
  // render only once the viewer is known to manage the selected team — a plain member gets the bare
  // "you don't manage this team" surface and nothing else. The detail-error state keeps them so the
  // "This team is gone" recovery can still reach the Team Tree.
  // A sub-team admin reaches the tree from any page they land on, including a team they don't
  // manage, so a typed link to a team above them is never a dead end.
  const showOrgActions = detail.status === "ready" || detail.status === "error" || leads.length > 0;

  return (
    <div className={styles.ptm} data-measure-surface="product">
      <TopbarSubtitle>{graph ? `${graph.orgName}` : "Organization"}</TopbarSubtitle>
      {showOrgActions || inviteCount > 0 ? (
        <TopbarActions>
          {/* The reader's OWN invitations to join a team — not the org's
              outgoing ones, which belong beside the people they are for and sit
              with the roster on the team itself. The key says whose they are,
              and is offered only while there is one to answer: a doorway to an
              empty list is a doorway nobody should be shown. It does not depend
              on managing anything — a plain member is exactly who gets invited.
              An invitation email still deep-links to `?tab=invites`, which opens
              the panel whether or not this key is on screen. */}
          {inviteCount > 0 ? (
            <Button
              variant="secondary"
              leftSection={<Icon name="mail" size={18} style={{ color: "var(--alkSecondaryText)" }} />}
              onClick={() => setInvitesOpen(true)}
              aria-haspopup="dialog"
            >
              Your invitations
              <Pill numeric tone="brand" style={{ marginLeft: "var(--alkSpace2)" }}>{inviteCount}</Pill>
            </Button>
          ) : null}
          {showOrgActions ? (
            <Button
              variant="secondary"
              leftSection={<Icon name="branch" size={18} style={{ color: "var(--alkSecondaryText)" }} />}
              onClick={() => setTreeOpen(true)}
              disabled={status !== "ready"}
              aria-haspopup="dialog"
            >
              Team tree
            </Button>
          ) : null}
        </TopbarActions>
      ) : null}

      <div className={styles.body}>
        {status === "loading" ? <DetailSkeleton /> : null}
        {status === "error" ? (
          <section className={shell.detail} aria-label="Couldn’t load teams">
            <EmptyState
              tone="alert"
              icon={<Icon name="alert" size={48} />}
              title="We couldn’t load your teams"
              body={errorMessage ?? "Try again."}
              action={
                <Button variant="secondary" onClick={retry}>
                  Try again
                </Button>
              }
            />
          </section>
        ) : null}
        {status === "ready" && graph && !effectiveId && landing?.kind === "pick" ? (
          <TeamPicker teams={landing.teams} onSelect={selectTeam} />
        ) : null}
        {status === "ready" && graph && effectiveId ? (
          !selectedTeam ? (
            <section className={shell.detail} aria-label="Team not found">
              <EmptyState
                icon={<Icon name="refresh" size={48} />}
                title="This team is gone"
                body="It was removed, or the link is stale. Pick another team from the team tree."
                action={
                  <Button variant="secondary" onClick={() => selectTeam(graph.rootId)}>
                    Go to {graph.orgName}
                  </Button>
                }
              />
            </section>
          ) : (
            <TeamDetail
              key={effectiveId}
              graph={graph}
              teamId={effectiveId}
              detail={detail}
              actions={detailActions}
              onRetry={retry}
              leads={leads.filter((t) => t.id !== effectiveId)}
            />
          )
        ) : null}
      </div>

      {/* The Team Tree — an on-demand right-side panel (SidePanel owns scrim, slide, focus trap, Esc). */}
      {graph ? (
        <SidePanel
          open={treeOpen}
          onClose={() => setTreeOpen(false)}
          anchor="viewport"
          mode="modal"
          width={360}
          className={styles.treePanel}
          title="Team tree"
          headActions={
            orgAdmin ? (
              <Button
                iconOnly
                variant="secondary" fill="ghost"
                size="md"
                aria-label="Create team"
                title="Create team"
                onClick={() => openModal({ kind: "create", parentId: graph.rootId })}
              >
                <Icon name="plus" size={16} />
              </Button>
            ) : undefined
          }
        >
          <TeamTree graph={graph} selectedId={effectiveId} expanded={expanded} onSelect={selectTeam} onToggle={toggle} />
        </SidePanel>
      ) : null}

      {/* Your own pending invitations — a recipient-side panel to accept or decline. */}
      <SidePanel open={invitesOpen} onClose={() => setInvitesOpen(false)} anchor="viewport" mode="modal" width={440} title="Your invitations">
        <InvitesPanel onResolved={(text, ok) => pushToast(text, ok ? "success" : "error")} />
      </SidePanel>

      {/* The member drawer — usage for a team admin; account + budget controls for an org admin. */}
      <MemberDrawer target={planTarget} onClose={() => setPlanTarget(null)} onNotify={(text, ok) => pushToast(text, ok ? "success" : "error")} />

      {graph && modal?.kind === "create" ? (
        <CreateTeamModal open={modalOpen} parentName={modal.parentName} onClose={closeModal} onSubmit={(name) => createTeam(modal.parentId, name)} />
      ) : null}
      {graph && modal?.kind === "add" ? (
        <AddMemberModal
          open={modalOpen}
          directory={directory}
          loading={directoryQuery.isLoading}
          searchable={orgAdmin}
          excludeIds={new Set(directRoster(graph, modal.teamId).map((e) => e.person.id))}
          coveredBy={
            new Map(
              descentRoster(graph, modal.teamId)
                .filter((e) => e.directRole === null)
                .map((e) => [e.person.id, e.descentFrom!.name]),
            )
          }
          pendingInvites={invitesFor(graph, modal.teamId).map((inv) => inv.email)}
          teamName={teamName(modal.teamId)}
          onClose={closeModal}
          onSubmit={(personId, role) => addMember(modal.teamId, personId, role)}
          onInvite={(email, role) => sendInvite(modal.teamId, email, role)}
        />
      ) : null}
      {graph && modal?.kind === "rename" ? (
        <RenameModal open={modalOpen} currentName={teamName(modal.teamId)} onClose={closeModal} onSubmit={(name) => renameTeam(modal.teamId, name)} />
      ) : null}
      {graph && modal?.kind === "move-team" ? (
        <TeamPickModal
          open={modalOpen}
          title={`Move ${teamName(modal.teamId)}`}
          sub="Choose a new parent team. Its sub-teams and members move with it."
          confirmLabel="Move team"
          choices={moveTeamChoices}
          onClose={closeModal}
          onSubmit={(destId) => moveTeam(modal.teamId, destId)}
        />
      ) : null}
      {graph && modal?.kind === "move-member" ? (
        <MoveMemberModalPick open={modalOpen} who={modal.entry.person.name} choices={moveMemberChoices} onClose={closeModal} onSubmit={(destId) => moveMember(modal.teamId, modal.entry, destId)} />
      ) : null}
      {graph && modal?.kind === "delete" ? (
        <ConfirmDialog
          open={modalOpen}
          onClose={closeModal}
          onConfirm={() => deleteTeam(modal.teamId)}
          title={`Delete ${deleteTarget?.name ?? "team"}?`}
          consequence="It has no members or sub-teams. This can’t be undone."
          confirmLabel="Delete team"
          tone="destructive"
        />
      ) : null}

      {graph && modal?.kind === "remove" ? (
        <ConfirmDialog
          open={modalOpen}
          onClose={closeModal}
          onConfirm={() => confirmRemove(modal.teamId, modal.entry)}
          title={
            modal.entry.person.id === graph.viewerId
              ? `Leave ${teamName(modal.teamId)}?`
              : `Remove ${modal.entry.person.name} from ${teamName(modal.teamId)}?`
          }
          consequence={removalCascade(graph, modal.teamId, modal.entry.person.id === graph.viewerId)}
          confirmLabel={modal.entry.person.id === graph.viewerId ? "Leave team" : "Remove"}
          tone="destructive"
        />
      ) : null}

      {graph && modal?.kind === "deprovision" ? (
        <ConfirmDialog
          open={modalOpen}
          onClose={closeModal}
          onConfirm={() => deprovision(modal.teamId, modal.entry)}
          title={`Remove ${modal.entry.person.name} from the organization`}
          consequence={`${modal.entry.person.name} is a member of the organization itself, so this removes their access entirely rather than editing a team.`}
          confirmLabel="Deactivate account"
          tone="destructive"
        >
          <p>
            Their account is deactivated, every browser session and CLI token is signed out
            immediately, and any invitation they haven’t accepted is cancelled. To take them off a
            single team instead, remove them from that team.
          </p>
        </ConfirmDialog>
      ) : null}

      <ToastViewport toasts={toasts} onDismiss={dismiss} position="bottom-right" />
    </div>
  );
}

/** The landing for someone who leads several unrelated teams: one choice per team they lead. */
function TeamPicker({ teams, onSelect }: { teams: Team[]; onSelect: (id: string) => void }) {
  return (
    <section className={shell.detail} aria-label="Teams you administer">
      <EmptyState
        icon={<Icon name="branch" size={48} />}
        title="Choose a team"
        body="You administer these teams and every team under them."
        action={
          <Stack gap={3} align="stretch">
            {teams.map((t) => (
              <Button key={t.id} variant="secondary" onClick={() => onSelect(t.id)}>
                {t.name}
              </Button>
            ))}
          </Stack>
        }
      />
    </section>
  );
}
