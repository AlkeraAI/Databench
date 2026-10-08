// One user's detail register — their platform role (admin-only to set), their seat
// balances and accessible pools, and their usage. A registrar grants credit from the
// header and reconciles the seat's remaining balance against its grant.

import { useEffect, useState, type ReactElement } from "react";
import { NavLink, useParams } from "react-router-dom";

import { Button, Callout, Card, ConfirmDialog, DescList, DescRow, EmptyState, Inline, Select, Stack, TextInput, ToastViewport, Toolbar, cx, useToasts } from "@alkera/ui";

import shared from "../admin.module.css";
import { useSpecificTitle } from "../../../../app/documentTitle";
import { Icon } from "../../../../app/icons";
import { useCurrentUser } from "../../../../api/auth";
import { isGone } from "../../../../api/gone";
import { NotFoundPage } from "../../../NotFoundPage";
import { useAdminUsers, useSetPlatformRoleMutation, useUserIpInfo, type AdminUser, type PlatformRole } from "../../../../api/admin/admin";
import { useBanUser, useLiftUserBan, useUserBans } from "../../../../api/admin/bans";
import { ADMIN_USER_CARDS } from "../../../../app/extensions/portal";
import { AdminShell } from "../AdminShell";
import { AccountRequestsCard } from "./AccountRequestsCard";
import { Dash, Reading } from "../shared/chrome";
import { BAN_EFFECT, BAN_KEYS } from "../shared/confirm";
import { dateTime, isEntityId, usd } from "../shared/format";
import { BannedPill, DisposableSeal, VerifiedPill } from "../shared/status";
import { RegisterError } from "../shared/states";
import { refusalSentence } from "../../../../api/errors";

const ROLE_OPTIONS: { value: PlatformRole; label: string }[] = [
  { value: "alkera_support", label: "Platform support" },
  { value: "alkera_admin", label: "Platform admin" },
];

/** The gone state's copy. Shared with the test so the copy and the assertions never drift. */
export const GONE_USER = {
  title: "This user is gone",
  body: "The account was removed, or the link is stale.",
  action: "Go to Users",
} as const;

/** The element behind `/admin/users/:userId`: an id that is not a user id costs no request. */
export function AdminUserRoute(): ReactElement {
  const { userId } = useParams<{ userId: string }>();
  if (!isEntityId(userId)) return <NotFoundPage />;
  return <AdminUserDetailPage />;
}

export function AdminUserDetailPage() {
  const { userId } = useParams<{ userId: string }>();
  const users = useAdminUsers();
  const user = users.data?.find((u) => u.id === userId);
  const viewer = useCurrentUser();
  const isAdmin = viewer.data?.platform_role === "alkera_admin";

  const { toasts, push, dismiss } = useToasts();
  const onDone = (text: string, ok: boolean) => push({ message: text, tone: ok ? "success" : "danger" });
  const [cards] = useState(() => ADMIN_USER_CARDS.items());
  // Which user this register is about — the tab says so before the error branch below
  // can return, since a hook may not sit behind a condition.
  useSpecificTitle(user?.email ?? user?.display_name ?? null);

  // A register that loaded without this id is an answer (the account is gone), not a load to
  // try again.
  if ((users.isError && isGone(users.error)) || (users.data && !user)) {
    return (
      <AdminShell subtitle="User">
        <EmptyState
          icon={<Icon name="refresh" size={48} />}
          title={GONE_USER.title}
          body={GONE_USER.body}
          action={
            <NavLink to="/admin/users" className="alk-link">
              {GONE_USER.action}
            </NavLink>
          }
        />
      </AdminShell>
    );
  }
  if (users.isError) {
    return (
      <AdminShell subtitle="User">
        <RegisterError what="this user" error={users.error} onRetry={() => void users.refetch()} />
      </AdminShell>
    );
  }

  const title = user?.email ?? "User";
  return (
    <AdminShell subtitle="User detail">
      <Stack gap={3} align="stretch" style={{ marginBottom: "var(--alkSpace7)" }}>
        <NavLink to="/admin/users" className={cx(shared.back, "alk-link", "alk-meta")}>
          <Icon name="back" size={14} /> Users
        </NavLink>
        <Inline gap={5}>
          <h1 className="alk-h1" style={{ margin: 0 }}>{title}</h1>
          {user?.banned ? <BannedPill reason={user.ban_reason} /> : null}
        </Inline>
      </Stack>

      <Stack gap={7} align="stretch">
        {user ? <AccountForensicsCard user={user} /> : null}
        {isAdmin && user ? <BanCard user={user} onDone={onDone} /> : null}
        {isAdmin && user ? <PlatformRoleCard userId={user.id} current={user.platform_role ?? null} onDone={onDone} /> : null}
        {user ? <AccountRequestsCard userId={user.id} isAdmin={isAdmin} onDone={onDone} /> : null}

        {userId
          ? cards.map(({ key, Card: UserCard }) => (
              <UserCard key={key} userId={userId} userEmail={user?.email ?? null} isAdmin={isAdmin} onDone={onDone} />
            ))
          : null}
      </Stack>

      <ToastViewport toasts={toasts} onDismiss={dismiss} position="bottom-right" />
    </AdminShell>
  );
}

/** The account's ban state and the one action that changes it. A domain ban is NOT
 *  liftable here — it belongs to the domain, not to this account — so the card names
 *  the domain and sends the operator to the bans register rather than offering a lift
 *  that would 404. */
function BanCard({ user, onDone }: { user: AdminUser; onDone: (t: string, ok: boolean) => void }) {
  const bans = useUserBans();
  const ban = useBanUser();
  const lift = useLiftUserBan();
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  // The row's `banned` flag covers BOTH registers. An account ban shows up in the
  // user-ban register, so its absence while the row reads banned means the account's
  // email domain is what caught them.
  const accountBan = bans.data?.find((b) => b.user_id === user.id && b.active) ?? null;
  const byDomain = user.banned && !accountBan;
  const domain = user.email.split("@")[1] ?? "";

  const fail = (err: unknown, fallback: string) => {
    const message = refusalSentence(err, { fallback });
    setError(message);
    setConfirming(false);
    onDone(message, false);
  };

  return (
    <Card title="Ban" icon={<Icon name="triBarred" size={16} />} headingLevel={2}>
      <Stack gap={5} align="stretch">
        <p className="alk-caption" style={{ margin: 0 }}>
          {byDomain
            ? `Banned by domain ${domain}. The ban covers every account at that domain, so it is lifted on the bans register, not here.`
            : accountBan
              ? `Banned${accountBan.reason ? ` (${accountBan.reason})` : ""}. ${BAN_EFFECT}`
              : BAN_EFFECT}
        </p>
        {error ? <Callout tone="danger" role="alert">{error}</Callout> : null}
        {byDomain ? (
          <NavLink to="/admin/bans" className="alk-link">Open the bans register</NavLink>
        ) : accountBan ? (
          <Toolbar
            end={
              <Button
                variant="secondary"
                loading={lift.isPending}
                onClick={() => {
                  setError(null);
                  lift.mutate(user.id, {
                    onSuccess: () => onDone("Ban lifted.", true),
                    onError: (err) => fail(err, "Could not lift the ban."),
                  });
                }}
              >
                Lift ban
              </Button>
            }
          />
        ) : (
          <Toolbar
            end={
              <Button variant="destructive" onClick={() => setConfirming(true)}>
                Ban
              </Button>
            }
          >
            <TextInput label="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why this account is banned" />
          </Toolbar>
        )}
      </Stack>
      <ConfirmDialog
        open={confirming}
        onClose={() => setConfirming(false)}
        title={`Ban ${user.email}?`}
        consequence={BAN_EFFECT}
        confirmLabel={BAN_KEYS.account}
        tone="destructive"
        busy={ban.isPending}
        onConfirm={() => {
          setError(null);
          ban.mutate(
            { userId: user.id, reason: reason.trim() },
            {
              onSuccess: () => {
                setConfirming(false);
                setReason("");
                onDone(`${user.email} is banned.`, true);
              },
              onError: (err) => fail(err, "Could not ban the user."),
            },
          );
        }}
      />
    </Card>
  );
}

function PlatformRoleCard({ userId, current, onDone }: { userId: string; current: PlatformRole | null; onDone: (t: string, ok: boolean) => void }) {
  const setRole = useSetPlatformRoleMutation();
  const [value, setValue] = useState<string>(current ?? "");
  useEffect(() => setValue(current ?? ""), [current]);
  const changed = value !== (current ?? "");

  const save = () => {
    setRole.mutate(
      { userId, platformRole: value === "" ? null : (value as PlatformRole) },
      { onSuccess: () => onDone("Platform role saved.", true), onError: (e) => onDone(refusalSentence(e, { fallback: "Could not save the role." }), false) },
    );
  };

  return (
    <Card title="Platform role" icon={<Icon name="shield" size={16} />} headingLevel={2}>
      <p className="alk-caption">Grants platform staff access across every tenant. Leave as a regular user for an ordinary account.</p>
      <Toolbar
        style={{ marginTop: "var(--alkSpace5)" }}
        end={<Button variant="secondary" disabled={!changed} loading={setRole.isPending} onClick={save}>Save</Button>}
      >
        <Select label="Role" value={value} onChange={(e) => setValue(e.target.value)}>
          <option value="">Regular user</option>
          {ROLE_OPTIONS.map((r) => (
            <option key={r.value} value={r.value}>{r.label}</option>
          ))}
        </Select>
      </Toolbar>
    </Card>
  );
}

function IpReading({ label, ip, info }: { label: string; ip: string | null | undefined; info: { city?: string | null; region?: string | null; country?: string | null; network?: string | null; error?: string | null } | null | undefined }) {
  if (!ip) return <DescRow label={label}><Dash /></DescRow>;
  const geo = info
    ? info.error
      ? info.error
      : [
          [info.city, info.country].filter(Boolean).join(", ") || null,
          info.network ?? null,
        ]
          .filter(Boolean)
          .join(" · ") || null
    : null;
  return (
    <DescRow label={label}>
      <Inline gap={3}>
        <Reading>{ip}</Reading>
        {geo ? <Reading muted>{geo}</Reading> : null}
      </Inline>
    </DescRow>
  );
}

/** The abuse-forensics readout: verification state, signup instant, and the
 *  recorded IPs with best-effort geo/network context (VPS/hosting operators
 *  here are the strong bot signal). */
function AccountForensicsCard({ user }: { user: AdminUser }) {
  const ipInfo = useUserIpInfo(user.id, Boolean(user.signup_ip || user.last_login_ip));
  return (
    <Card title="Account signals" icon={<Icon name="compass" size={16} />} headingLevel={2}>
      <DescList>
        <DescRow label="Email">
          <Inline gap={3}>
            <Reading>{user.email}</Reading>
            <VerifiedPill verifiedAt={user.email_verified_at} />
            {user.disposable_email ? <DisposableSeal /> : null}
          </Inline>
        </DescRow>
        <DescRow label="Signed up"><Reading muted>{dateTime(user.created_at)}</Reading></DescRow>
        <DescRow label="This month"><Reading>{usd(user.mtd_billed_nanos)}</Reading></DescRow>
        <IpReading label="Signup IP" ip={user.signup_ip} info={ipInfo.data?.signup} />
        <IpReading label="Last login IP" ip={user.last_login_ip} info={ipInfo.data?.last_login} />
      </DescList>
    </Card>
  );
}
