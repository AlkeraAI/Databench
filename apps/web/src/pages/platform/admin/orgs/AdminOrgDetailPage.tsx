// One organization, managed from one page. Tabs switch the band and ride the URL (`?tab=`) so a link
// lands on the band it names: Overview (plan, machines, sandbox limits, storage, sign-in, Slack),
// Members and teams, Activity (chats, errors and refusals, audit), Usage, and the tabs an installed
// extension adds through ADMIN_ORG_TABS (billing's Credits). Writes are platform-admin; a support-grade staffer reads, and never sees
// the admin-only Settings band (the danger zone).

import { useState, type ReactElement, type ReactNode } from "react";
import { NavLink, useParams, useSearchParams } from "react-router-dom";

import { Button, Card, ConfirmDialog, DescList, DescRow, EmptyState, GitHubMark, GoogleMark, Inline, Pill, type PillTone, SegmentedControl, Stack, Switch, Table, TextInput, ToastViewport, Toolbar, cx, useToasts } from "@alkera/ui";

import styles from "./AdminOrgDetailPage.module.css";
import shared from "../admin.module.css";
import { Icon } from "../../../../app/icons";
import { NotFoundPage } from "../../../NotFoundPage";
import { ApiError, refusalSentence } from "../../../../api/errors";
import { useCurrentUser } from "../../../../api/auth";
import { useAdminOrg, useAdminOrgAudit, useAdminOrgChats, useAdminOrgErrors, useAdminOrgMembers, useAdminOrgTeams, useDeleteOrgMutation, useRenameOrgMutation, useAdminOrgSettings, useUpdateOrgSettingsMutation, type OrgChatInsight, type OrgIssue } from "../../../../api/admin/admin";
import { useOrgComputeGrants, useRevokeOrgComputeMutation } from "../../../../api/admin/compute";
import { ADMIN_ORG_CARDS, ADMIN_ORG_TABS, PORTAL_ROUTES, placeAfter, type AdminOrgCard } from "../../../../app/extensions/portal";
import { AdminShell } from "../AdminShell";
import { Reading, IdCell } from "../shared/chrome";
import { count, date, isEntityId, orgLabel } from "../shared/format";
import { preciseUsd } from "../shared/precise";
import { GrantComputeModal } from "../shared/modals";
import { RenameModal } from "../shared/confirm";
import { RegisterError, TableSkeleton } from "../shared/states";
import { LiveEditingCard } from "./LiveEditingCard";
import { StorageCard } from "./StorageCard";

type OpenTab = "overview" | "members" | "activity" | "settings";

/** The two sign-in providers rendered in the Settings band. The name is both the visible label and the
 *  stem of each toggle's accessible name; `key` is the settings flag the toggle reads + writes — a test
 *  and the login screen read the same source. */
export const SIGN_IN_PROVIDERS = [
  { name: "Google", key: "allow_login_google" as const, mark: <GoogleMark size={18} /> },
  { name: "GitHub", key: "allow_login_github" as const, mark: <GitHubMark size={18} /> },
];

/** The accessible name of a provider's allow/block toggle. Shared with the test so the two never drift. */
export const providerToggleLabel = (name: string): string => `Allow sign-in with ${name}`;

/** Section-tab labels, in order. Settings (the danger zone) is platform-admin only. */
export const TAB_LABELS = {
  overview: "Overview",
  members: "Members & teams",
  activity: "Activity",
  settings: "Settings",
} as const;

const OPEN_TABS = Object.keys(TAB_LABELS) as OpenTab[];

/** Every tab in strip order, an installed extension's placed after the tab it names. */
function orgTabs(): { key: string; label: string }[] {
  return placeAfter(
    OPEN_TABS.map((key) => ({ key, label: TAB_LABELS[key] as string })),
    ADMIN_ORG_TABS.items().map((tab) => ({ item: { key: tab.key, label: tab.label }, after: tab.after, label: tab.key })),
    (tab) => tab.key,
  );
}

/** The open cards on the overview tab, in order. A card for the higher grade only renders
 *  nothing for support staff. */
const OPEN_ORG_CARDS: AdminOrgCard[] = [
  { key: "sandbox", after: null, Card: ({ orgId, isAdmin, onDone }) => <SandboxLimitsCard orgId={orgId} isAdmin={isAdmin} onDone={onDone} /> },
  { key: "compute", after: null, Card: ({ orgId, isAdmin, onDone }) => <ComputeTab orgId={orgId} isAdmin={isAdmin} onDone={onDone} /> },
  { key: "storage", after: null, Card: ({ orgId, isAdmin }) => <StorageCard orgId={orgId} isAdmin={isAdmin} /> },
  { key: "live-editing", after: null, Card: ({ orgId, isAdmin }) => <LiveEditingCard orgId={orgId} isAdmin={isAdmin} /> },
  { key: "settings", after: null, Card: ({ orgId, isAdmin, onDone }) => (isAdmin ? <SettingsTab orgId={orgId} onDone={onDone} /> : null) },
];

/** The overview tab's cards, an installed extension's placed after the card it names. */
function orgCards(): AdminOrgCard[] {
  return placeAfter(
    OPEN_ORG_CARDS,
    ADMIN_ORG_CARDS.items().map((card) => ({ item: card, after: card.after, label: card.key })),
    (card) => card.key,
  );
}

/** The sandbox-limits card's fixed strings, and the bounds the server holds. Shared with the test. */
export const SANDBOX_LABELS = {
  title: "Chat sandbox limits",
  vcpu: "vCPUs per chat",
  memory: "Memory per chat (MB)",
  placeholder: "Box default",
  save: "Save limits",
  adminOnly: "Only a platform admin can change sandbox limits.",
  invalid: "Use 1 to 64 vCPUs and 512 to 262144 MB, or leave a field empty for the box default.",
} as const;
export const SANDBOX_BOUNDS = { vcpu: [1, 64], memory: [512, 262144] } as const;

/** The activity tab's fixed strings. Shared with the test. */
export const ACTIVITY_LABELS = {
  chats: "Chats",
  chatsEmpty: "No chats yet.",
  issues: "Errors and refusals",
  issuesEmpty: "No errors or refusals in the last week.",
  audit: "Audit events",
  auditEmpty: "No audit events yet.",
} as const;

export const ISSUE_KIND_LABELS: Record<OrgIssue["kind"], string> = {
  error: "Error",
  refusal: "Model refusal",
  denied: "Permission denied",
  machine_refused: "Machine refused",
};

export const USAGE_EMPTY = "No usage yet.";

/** The not-found surface for an org id the register no longer answers for. The register's own list is
 *  the way back, the way the Team Tree is on the teams page. */
export const GONE_ORG = {
  title: "This organization is gone",
  body: "It was removed, or the link is stale.",
  action: "Go to Organizations",
} as const;

/** The page after a delete from its own Settings band: the org is gone because this admin deleted it,
 *  which the stale-link answer would misread. */
export const deletedTitle = (name: string) => `Deleted ${name}`;

/** The element behind `/admin/orgs/:orgId`.
 *
 *  It stands in front of the page rather than inside it so an id that is not an org's costs no
 *  request: every read the detail page makes on mount is keyed by that id, and none of them are owed
 *  to an address the register could never have written. */
export function AdminOrgRoute(): ReactElement {
  const { orgId } = useParams<{ orgId: string }>();
  if (!isEntityId(orgId)) return <NotFoundPage />;
  return <AdminOrgDetailPage />;
}

/** The two irreversible actions in the merged danger zone, and the confirmation title that names the
 *  org before it's destroyed — shared with the test so the label and the title format never drift. */
export const DANGER_LABELS = { rename: "Rename organization", delete: "Delete organization" } as const;
export const deleteConfirmTitle = (orgName: string): string => `Delete ${orgName}?`;

/** What the admin types back before an org is deleted: its name, or, for an org that has no name
 *  yet, the short id its label shows. Never empty, so the gate can never stand open. */
export const deleteConfirmWord = (org: { id: string; name: string }): string =>
  org.name.trim() ? org.name.trim() : org.id.slice(0, 8);

/** The plan band's fixed strings — the two plan readings and the conversion action. Shared with the
 *  test so the badge text and the button label never drift. */
/** The compute band's fixed strings. Shared with the test so the copy and the
 *  assertions never drift. */
export const COMPUTE_LABELS = {
  title: "Compute",
  empty: "No compute grant. Machines this org starts or registers are refused until one is written.",
  grant: "Grant compute",
  revoke: "Revoke",
  adminOnly: "Only a platform admin can grant or revoke compute.",
  anyType: "Any machine type",
} as const;

export const revokeConfirmTitle = (what: string): string => `Revoke the ${what} grant?`;

/** What a grant admits, as the band names it.
 *
 *  The wildcard is decided by the ABSENCE of a machine type id, never by a
 *  blank display name: a typed grant whose name failed to come back must not
 *  read as "Any machine type", which is the opposite — and far more expensive —
 *  grant. A typed grant with no name falls back to its provider code.
 */
export const machineTypeLabel = (g: {
  machine_type_id?: string | null;
  machine_type?: string;
  machine_type_display_name?: string;
}): string =>
  g.machine_type_id == null
    ? COMPUTE_LABELS.anyType
    : g.machine_type_display_name || g.machine_type || g.machine_type_id;

export function AdminOrgDetailPage() {
  const { orgId } = useParams<{ orgId: string }>();
  const org = useAdminOrg(orgId);
  const viewer = useCurrentUser();
  const isAdmin = viewer.data?.platform_role === "alkera_admin";

  const [params, setParams] = useSearchParams();
  const asked = params.get("tab");
  const tabs = orgTabs().filter((t) => t.key !== "settings" || isAdmin);
  const [overviewCards] = useState(orgCards);
  const tab: string = asked !== null && tabs.some((t) => t.key === asked) ? asked : "overview";
  const extensionTab = ADMIN_ORG_TABS.items().find((t) => t.key === tab);
  const setTab = (next: string) => {
    const updated = new URLSearchParams(params);
    updated.set("tab", next);
    setParams(updated, { replace: true });
  };
  const { toasts, push, dismiss } = useToasts();
  const onDone = (text: string, ok: boolean) => push({ message: text, tone: ok ? "success" : "danger" });
  // Set once this admin deletes the org from the Settings band. The delete refreshes every read, the
  // org's own read then 404s, and the page would otherwise say the link is stale.
  const [deleted, setDeleted] = useState<string | null>(null);


  // Nothing under the tabs is worth reading for an org the register may not answer for: every band is
  // keyed by this id, so mounting them while the org read is in flight multiplies one 404 across the
  // page — and leaves their skeletons shimmering under a shell that names an organization we do not
  // have. The bands wait for the org itself.
  if (deleted !== null) {
    return (
      <AdminShell subtitle="Organization">
        <EmptyState
          icon={<Icon name="check" size={48} />}
          title={deletedTitle(deleted)}
          action={
            <NavLink to="/admin/orgs" className="alk-link">
              {GONE_ORG.action}
            </NavLink>
          }
        />
      </AdminShell>
    );
  }

  if (org.isPending) {
    return (
      <AdminShell subtitle="Organization">
        <TableSkeleton rows={6} cols={4} />
      </AdminShell>
    );
  }

  // A 404 is the register's answer, not a flake: there is nothing to retry and no shell to hang the
  // tabs on, so the page resolves to the same not-found the teams page gives a stale team link. Every
  // other failure keeps the retry state.
  if (org.isError) {
    const gone = org.error instanceof ApiError && org.error.status === 404;
    return (
      <AdminShell subtitle="Organization">
        {gone ? (
          <EmptyState
            icon={<Icon name="refresh" size={48} />}
            title={GONE_ORG.title}
            body={GONE_ORG.body}
            action={
              <NavLink to="/admin/orgs" className="alk-link">
                {GONE_ORG.action}
              </NavLink>
            }
          />
        ) : (
          <RegisterError what="this organization" error={org.error} onRetry={() => void org.refetch()} />
        )}
      </AdminShell>
    );
  }

  const memberLabel =
    org.data == null
      ? null
      : `${count(org.data.member_count)} ${org.data.member_count === 1 ? "member" : "members"}`;

  return (
    <AdminShell subtitle="Organization detail">
      <Stack gap={3} align="stretch" style={{ marginBottom: "var(--alkSpace7)" }}>
        <NavLink to="/admin/orgs" className={cx(shared.back, "alk-link", "alk-meta")}>
          <Icon name="back" size={14} /> Organizations
        </NavLink>
        <Inline gap={5}>
          <h1 className="alk-h1" style={{ margin: 0 }}>{org.data ? orgLabel(org.data) : "Organization"}</h1>
          {memberLabel == null ? null : <Pill tone="neutral">{memberLabel}</Pill>}
        </Inline>
      </Stack>

      {/* Six sections do not fit a phone's width at a readable size: the strip scrolls sideways
          instead of cutting every label to three letters. */}
      <div className={styles.sections} data-testid="org-sections">
        <SegmentedControl options={tabs} value={tab} onChange={setTab} label="Organization sections" />
      </div>

      {tab === "overview" ? (
        <Stack gap={7} align="stretch">
          {overviewCards.map(({ key, Card: OverviewCard }) => (
            <OverviewCard key={key} orgId={orgId!} orgName={org.data?.name ?? "this org"} isAdmin={isAdmin} onDone={onDone} />
          ))}
        </Stack>
      ) : null}
      {extensionTab ? (
        <extensionTab.Section orgId={orgId!} orgName={org.data?.name ?? "this org"} isAdmin={isAdmin} onDone={onDone} />
      ) : null}
      {tab === "members" ? (
        <Stack gap={7} align="stretch">
          <MembersTab orgId={orgId!} />
          <TeamsCard orgId={orgId!} />
        </Stack>
      ) : null}
      {tab === "activity" ? <ActivityTab orgId={orgId!} /> : null}
      {tab === "settings" && isAdmin ? (
        <DangerTab orgId={orgId!} org={org.data} onDone={onDone} onDeleted={setDeleted} />
      ) : null}

      <ToastViewport toasts={toasts} onDismiss={dismiss} position="bottom-right" />
    </AdminShell>
  );
}

function MembersTab({ orgId }: { orgId: string }) {
  const members = useAdminOrgMembers(orgId);
  if (members.isError) return <RegisterError what="this org's members" error={members.error} onRetry={() => void members.refetch()} />;
  if (!members.data) return <TableSkeleton rows={6} cols={4} />;
  // The roster is materialized onto every ancestor team, so a member in a nested team appears once per
  // ancestor — collapse to one row per user (preferring the admin grade) so the table matches the
  // org's member count instead of listing the same person two or three times.
  const roster = new Map<string, (typeof members.data)[number]>();
  for (const m of members.data) {
    const prev = roster.get(m.user_id);
    if (!prev || (m.role === "admin" && prev.role !== "admin")) roster.set(m.user_id, m);
  }
  const unique = [...roster.values()];
  if (unique.length === 0) return <Card title="Members" icon={<Icon name="users" size={16} />} headingLevel={2}><p className="alk-caption">No members yet.</p></Card>;
  return (
    <Card title="Members" icon={<Icon name="users" size={16} />} headingLevel={2}>
      <Table responsive colWidths={["14rem", undefined, "8rem", "8rem"]} columns={["Name", "Email", "Role", "ID"]} pageSize={20}>
        {unique.map((m) => (
          <tr key={m.user_id}>
            <td><NavLink to={`/admin/users/${m.user_id}`} className="alk-link">{m.display_name}</NavLink></td>
            <td><Reading muted>{m.email}</Reading></td>
            <td><Pill tone={m.role === "admin" ? "brand" : "neutral"} shape="rect">{m.role_display || m.role}</Pill></td>
            <td><IdCell id={m.user_id} /></td>
          </tr>
        ))}
      </Table>
    </Card>
  );
}

/** The org's commercial plan, and the one place an operator converts it.
 *
 *  Enterprise is contract-billed: it is NOT a Stripe product, so a conversion never goes through
 *  checkout — it enrolls the org onto its Enterprise plan (which then converts the org's paid seats
 *  and refunds their unused Stripe time). Reading the plan is staff-grade; converting is
 *  platform-admin, matching the endpoint. Figure edits (seat credit, spend cap) stay on the
 *  Enterprise register — re-enrolling would restart the billing anchor, so it is not an edit verb. */
/** The org's standing permission to run machines. Without a live grant every box the customer
 *  registers is refused with `no_compute_grant`, so this band is the last step of onboarding a new
 *  org — and the first place to look when their machines will not start. Reading is staff-grade;
 *  granting and revoking are platform-admin, matching the endpoints. */
function ComputeTab({ orgId, isAdmin, onDone }: { orgId: string; isAdmin: boolean; onDone: (t: string, ok: boolean) => void }) {
  const grants = useOrgComputeGrants(orgId);
  const revoke = useRevokeOrgComputeMutation(orgId);
  const [grantOpen, setGrantOpen] = useState(false);
  const [pending, setPending] = useState<{ id: string; what: string } | null>(null);

  const confirmRevoke = () => {
    if (pending == null) return;
    const { id, what } = pending;
    revoke.mutate(id, {
      onSuccess: () => { onDone(`Revoked the ${what} grant.`, true); setPending(null); },
      onError: (e) => { onDone(refusalSentence(e, { fallback: "could not revoke this grant" }), false); setPending(null); },
    });
  };

  return (
    <Card
      title={COMPUTE_LABELS.title}
      icon={<Icon name="monitor" size={16} />}
      headingLevel={2}
      actions={isAdmin ? <Button variant="secondary" onClick={() => setGrantOpen(true)}>{COMPUTE_LABELS.grant}</Button> : null}
    >
      {grants.isError ? (
        <RegisterError what="this org's compute grants" error={grants.error} onRetry={() => void grants.refetch()} />
      ) : !grants.data ? (
        <TableSkeleton rows={2} cols={4} />
      ) : grants.data.length === 0 ? (
        <p className="alk-caption">{COMPUTE_LABELS.empty}</p>
      ) : (
        <Table responsive colWidths={["16rem", "6rem", "8rem", undefined, "7rem"]} columns={["Machine type", "Ceiling", "Rate / min", "Expires", ""]}>
          {grants.data.map((g) => (
            <tr key={g.id}>
              <td><Reading>{machineTypeLabel(g)}</Reading></td>
              <td><Reading>{count(g.ceiling)}</Reading></td>
              <td><Reading muted>{preciseUsd(g.rate_per_minute_nanos)}</Reading></td>
              <td><Reading muted>{date(g.expires_at)}</Reading></td>
              <td>
                {isAdmin ? (
                  <Button variant="secondary" fill="ghost" onClick={() => setPending({ id: g.id, what: machineTypeLabel(g) })}>
                    {COMPUTE_LABELS.revoke}
                  </Button>
                ) : null}
              </td>
            </tr>
          ))}
        </Table>
      )}
      {!isAdmin ? <p className="alk-caption">{COMPUTE_LABELS.adminOnly}</p> : null}
      {isAdmin ? (
        <>
          <GrantComputeModal open={grantOpen} orgId={orgId} onClose={() => setGrantOpen(false)} onDone={onDone} />
          <ConfirmDialog
            open={pending !== null}
            title={pending ? revokeConfirmTitle(pending.what) : ""}
            consequence="Machines already running keep their minutes; nothing new is admitted under this grant."
            confirmLabel={COMPUTE_LABELS.revoke}
            tone="destructive"
            busy={revoke.isPending}
            onConfirm={confirmRevoke}
            onClose={() => setPending(null)}
          />
        </>
      ) : null}
    </Card>
  );
}

/** The org's team tree, flat: each team with its parent and member count. */
function TeamsCard({ orgId }: { orgId: string }) {
  const teams = useAdminOrgTeams(orgId);
  if (teams.isError) return <RegisterError what="this org's teams" error={teams.error} onRetry={() => void teams.refetch()} />;
  if (!teams.data) return <TableSkeleton rows={3} cols={3} />;
  const names = new Map(teams.data.map((t) => [t.id, t.name]));
  return (
    <Card title="Teams" icon={<Icon name="users" size={16} />} headingLevel={2}>
      {teams.data.length === 0 ? (
        <p className="alk-caption">No teams yet.</p>
      ) : (
        <Table responsive pageSize={20} colWidths={[undefined, undefined, "8rem"]} columns={["Team", "Parent", <span key="m" className={shared.readingEnd}>Members</span>]}>
          {teams.data.map((t) => (
            <tr key={t.id}>
              <td>{t.name}</td>
              <td><Reading muted>{t.parent_team_id ? (names.get(t.parent_team_id) ?? "—") : "—"}</Reading></td>
              <td><Reading align="end">{count(t.member_count ?? 0)}</Reading></td>
            </tr>
          ))}
        </Table>
      )}
    </Card>
  );
}

/** Parse one limit field: empty is the box default (`null`), anything else must be a whole number in
 *  bounds, or the form is invalid (`undefined`). */
export function parseLimit(raw: string, [min, max]: readonly [number, number]): number | null | undefined {
  const text = raw.trim();
  if (text === "") return null;
  if (!/^\d+$/.test(text)) return undefined;
  const n = Number(text);
  return n >= min && n <= max ? n : undefined;
}

/** How big each of the org's chat sandboxes is. Empty is the box's own default. The box reads the
 *  figures off its chat listing, so a change applies to the next sandbox it opens. */
function SandboxLimitsCard({ orgId, isAdmin, onDone }: { orgId: string; isAdmin: boolean; onDone: (t: string, ok: boolean) => void }) {
  const settings = useAdminOrgSettings(orgId);
  const update = useUpdateOrgSettingsMutation(orgId);
  const [vcpu, setVcpu] = useState<string | null>(null);
  const [memory, setMemory] = useState<string | null>(null);

  if (settings.isError) return <RegisterError what="this org's sandbox limits" error={settings.error} onRetry={() => void settings.refetch()} />;
  if (!settings.data) return <TableSkeleton rows={1} cols={2} />;
  const stored = { vcpu: settings.data.sandbox_vcpu ?? null, memory: settings.data.sandbox_memory_mb ?? null };
  const vcpuText = vcpu ?? (stored.vcpu == null ? "" : String(stored.vcpu));
  const memoryText = memory ?? (stored.memory == null ? "" : String(stored.memory));
  const nextVcpu = parseLimit(vcpuText, SANDBOX_BOUNDS.vcpu);
  const nextMemory = parseLimit(memoryText, SANDBOX_BOUNDS.memory);
  const invalid = nextVcpu === undefined || nextMemory === undefined;
  const dirty = !invalid && (nextVcpu !== stored.vcpu || nextMemory !== stored.memory);
  const show = (n: number | null, unit: string) => (n == null ? SANDBOX_LABELS.placeholder : `${count(n)} ${unit}`);

  const submit = () => {
    if (invalid) return;
    update.mutate(
      { sandbox_vcpu: nextVcpu, sandbox_memory_mb: nextMemory },
      {
        onSuccess: () => {
          setVcpu(null);
          setMemory(null);
          onDone("Sandbox limits saved.", true);
        },
        onError: (e) => onDone(refusalSentence(e, { fallback: "Could not save the sandbox limits." }), false),
      },
    );
  };

  return (
    <Card title={SANDBOX_LABELS.title} icon={<Icon name="monitor" size={16} />} headingLevel={2}>
      {!isAdmin ? (
        <Stack gap={3} align="stretch">
          <DescList>
            <DescRow label={SANDBOX_LABELS.vcpu}><Reading>{show(stored.vcpu, "vCPU")}</Reading></DescRow>
            <DescRow label={SANDBOX_LABELS.memory}><Reading>{show(stored.memory, "MB")}</Reading></DescRow>
          </DescList>
          <p className="alk-caption">{SANDBOX_LABELS.adminOnly}</p>
        </Stack>
      ) : (
        <Stack gap={5} align="stretch">
          <Inline gap={5}>
            <TextInput label={SANDBOX_LABELS.vcpu} requiredMark={false} inputMode="numeric" placeholder={SANDBOX_LABELS.placeholder} value={vcpuText} onChange={(e) => setVcpu(e.target.value)} />
            <TextInput label={SANDBOX_LABELS.memory} requiredMark={false} inputMode="numeric" placeholder={SANDBOX_LABELS.placeholder} value={memoryText} onChange={(e) => setMemory(e.target.value)} />
          </Inline>
          {invalid ? <p className="alk-caption" role="alert">{SANDBOX_LABELS.invalid}</p> : null}
          <Toolbar>
            <Button onClick={submit} disabled={!dirty || update.isPending}>{SANDBOX_LABELS.save}</Button>
          </Toolbar>
        </Stack>
      )}
    </Card>
  );
}

const MACHINE_WORD_TONE: Record<string, PillTone> = {
  ready: "success",
  awake: "success",
  refused: "danger",
  unreachable: "danger",
  stranded: "warning",
};

/** The word a chat row says about its machine, and its tone. The machine's state now decides it
 *  (`machine_status`, judged on the server from the machine's own row); what the box last said
 *  about the chat's session (`mirror_state`) is read only while that box is serving, since a box
 *  that was released or lost said its last "awake" about a session that no longer runs. */
function chatMachineWord(c: Pick<OrgChatInsight, "machine_status" | "mirror_state" | "machine_refusal">): {
  word: string;
  tone: PillTone;
} {
  const word = c.machine_refusal
    ? "refused"
    : c.machine_status === "ready" && c.mirror_state === "awake"
      ? "awake"
      : c.machine_status;
  return { word, tone: MACHINE_WORD_TONE[word] ?? "neutral" };
}

/** What is going on inside the org: its chats and the machine serving each, the week's errors and
 *  refusals, and the audit trail. Read-only, staff-grade. */
function ActivityTab({ orgId }: { orgId: string }) {
  const chats = useAdminOrgChats(orgId);
  // A machine's page exists only where the product registers the fleet console.
  const machinePages = PORTAL_ROUTES.items().some((route) => route.path === "/admin/machines/:machineId");
  const issues = useAdminOrgErrors(orgId);
  const audit = useAdminOrgAudit(orgId);
  return (
    <Stack gap={7} align="stretch">
      <Card title={ACTIVITY_LABELS.chats} icon={<Icon name="monitor" size={16} />} headingLevel={2}>
        {chats.isError ? (
          <RegisterError what="this org's chats" error={chats.error} onRetry={() => void chats.refetch()} />
        ) : !chats.data ? (
          <TableSkeleton rows={3} cols={4} />
        ) : chats.data.items.length === 0 ? (
          <p className="alk-caption">{ACTIVITY_LABELS.chatsEmpty}</p>
        ) : (
          <Table responsive pageSize={20} colWidths={[undefined, "14rem", "14rem", "10rem"]} columns={["Chat", "Owner", "Machine", "Last activity"]}>
            {chats.data.items.map((c) => (
              <tr key={c.id}>
                <td>{c.title || "Untitled"}</td>
                <td><Reading muted>{c.owner_email}</Reading></td>
                <td>
                  <Inline gap={2}>
                    {c.machine_id && machinePages ? (
                      <NavLink to={`/admin/machines/${encodeURIComponent(c.machine_id)}`} className="alk-link">{c.machine_name ?? "Machine"}</NavLink>
                    ) : c.machine_id ? (
                      <Reading>{c.machine_name ?? "Machine"}</Reading>
                    ) : (
                      <Reading muted>None</Reading>
                    )}
                    <Pill tone={chatMachineWord(c).tone} shape="rect">
                      {chatMachineWord(c).word}
                    </Pill>
                  </Inline>
                </td>
                <td><Reading muted>{date(c.last_activity_at ?? c.created_at)}</Reading></td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <Card title={ACTIVITY_LABELS.issues} icon={<Icon name="alert" size={16} />} headingLevel={2}>
        {issues.isError ? (
          <RegisterError what="this org's errors" error={issues.error} onRetry={() => void issues.refetch()} />
        ) : !issues.data ? (
          <TableSkeleton rows={3} cols={4} />
        ) : issues.data.items.length === 0 ? (
          <p className="alk-caption">{ACTIVITY_LABELS.issuesEmpty}</p>
        ) : (
          <Table responsive pageSize={20} colWidths={["10rem", "14rem", undefined, "10rem"]} columns={["Kind", "Chat", "Detail", "When"]}>
            {issues.data.items.map((i, n) => (
              <tr key={`${i.chat_id}-${i.at}-${n}`}>
                <td><Pill tone={i.kind === "error" || i.kind === "machine_refused" ? "danger" : "warning"} shape="rect">{ISSUE_KIND_LABELS[i.kind]}</Pill></td>
                <td>{i.chat_title || "Untitled"}</td>
                <td><Reading muted>{i.detail}</Reading></td>
                <td><Reading muted>{date(i.at)}</Reading></td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <Card title={ACTIVITY_LABELS.audit} icon={<Icon name="key" size={16} />} headingLevel={2}>
        {audit.isError ? (
          <RegisterError what="this org's audit trail" error={audit.error} onRetry={() => void audit.refetch()} />
        ) : !audit.data ? (
          <TableSkeleton rows={3} cols={3} />
        ) : audit.data.items.length === 0 ? (
          <p className="alk-caption">{ACTIVITY_LABELS.auditEmpty}</p>
        ) : (
          <Table responsive pageSize={20} colWidths={["16rem", "16rem", undefined, "10rem"]} columns={["Action", "Actor", "Target", "When"]}>
            {audit.data.items.map((e) => (
              <tr key={e.id}>
                <td>{e.action}</td>
                <td><Reading muted>{e.actor_email || "System"}</Reading></td>
                <td><Reading muted>{e.target ?? "—"}</Reading></td>
                <td><Reading muted>{date(e.created_at)}</Reading></td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </Stack>
  );
}

function SettingsTab({ orgId, onDone }: { orgId: string; onDone: (t: string, ok: boolean) => void }) {
  const settings = useAdminOrgSettings(orgId);
  const update = useUpdateOrgSettingsMutation(orgId);
  if (settings.isError) return <RegisterError what="this org's settings" error={settings.error} onRetry={() => void settings.refetch()} />;
  if (!settings.data) return <TableSkeleton rows={2} cols={2} />;
  const s = settings.data;
  const toggle = (key: "allow_login_google" | "allow_login_github") => (on: boolean) => {
    update.mutate({ [key]: on }, { onSuccess: () => onDone("Settings saved.", true), onError: () => onDone("Could not save the settings.", false) });
  };
  return (
    <Card title="Sign-in providers" icon={<Icon name="key" size={16} />} headingLevel={2}>
      <Stack gap={5} align="stretch">
        {SIGN_IN_PROVIDERS.map((p) => (
          <ProviderToggle key={p.key} name={p.name} mark={p.mark} allowed={s[p.key]} onToggle={toggle(p.key)} />
        ))}
      </Stack>
    </Card>
  );
}

/** One sign-in provider row — the settings page's provider-row design (a chipped brand mark, the
 *  label, and the ui Switch pushed right). The label doubles as the toggle's accessible name. */
function ProviderToggle({ name, mark, allowed, onToggle }: {
  name: string;
  mark: ReactNode;
  allowed: boolean;
  onToggle: (on: boolean) => void;
}) {
  const label = providerToggleLabel(name);
  return (
    <Inline gap={5} wrap={false} data-provider={name}>
      <span className={styles.providerLogo} aria-hidden="true">{mark}</span>
      {/* The auto margin pushes the Switch to the row's right edge. */}
      <span className="alk-strong" style={{ marginRight: "auto" }}>{label}</span>
      <Switch checked={allowed} aria-label={label} onChange={(e) => onToggle(e.target.checked)} />
    </Inline>
  );
}

function DangerTab({
  orgId,
  org,
  onDone,
  onDeleted,
}: {
  orgId: string;
  org: { id: string; name: string } | undefined;
  onDone: (t: string, ok: boolean) => void;
  onDeleted: (shown: string) => void;
}) {
  const orgName = org?.name ?? "";
  const shown = org ? orgLabel(org) : "this org";
  const rename = useRenameOrgMutation();
  const del = useDeleteOrgMutation();
  const [renameOpen, setRenameOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  return (
    <Card title="Danger zone" icon={<Icon name="alert" size={16} />} headingLevel={2}>
      <DescList>
        <DescRow label="Rename"><Button variant="secondary" onClick={() => setRenameOpen(true)}>{DANGER_LABELS.rename}</Button></DescRow>
        <DescRow label="Delete"><Button variant="destructive" fill="outline" disabled={!org} onClick={() => setDeleteOpen(true)}>{DANGER_LABELS.delete}</Button></DescRow>
      </DescList>
      <RenameModal
        open={renameOpen}
        onClose={() => setRenameOpen(false)}
        title="Rename organization"
        label="Org name"
        current={orgName}
        busy={rename.isPending}
        onSubmit={(name) => rename.mutate({ orgId, name }, { onSuccess: () => { onDone(`Renamed to ${name}.`, true); setRenameOpen(false); }, onError: () => onDone("Could not rename.", false) })}
      />
      <ConfirmDialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        title={deleteConfirmTitle(shown)}
        consequence="This permanently deletes the organization and everything in it. It can't be undone."
        confirmLabel={DANGER_LABELS.delete}
        tone="destructive"
        requireTyped={org ? deleteConfirmWord(org) : undefined}
        confirmDisabled={!org}
        busy={del.isPending}
        onConfirm={() => del.mutate(orgId, { onSuccess: () => { setDeleteOpen(false); onDeleted(shown); }, onError: (e) => onDone(refusalSentence(e, { fallback: "Could not delete." }), false) })}
      />
    </Card>
  );
}
