// The bans register — the two lists that decide who may reach the product at all:
// banned accounts and banned email domains. A ban is answered everywhere as if the
// account never existed, so this page is the only place the state is legible; each
// section keeps its lifted rows under a disclosure so the record of what was done
// (and undone) stays on the page rather than only in the audit log.
//
// The server owns every rule: it normalizes a domain, refuses a self-ban or a staff
// account, and refuses a duplicate. The page sends what was typed and surfaces the
// answer verbatim — it never pre-judges a value the backend is the authority on.

import { useMemo, useState, type ReactNode } from "react";

import { Button, Callout, Card, Collapse, ConfirmDialog, Stack, Table, TextInput, ToastViewport, Toolbar, useToasts } from "@alkera/ui";

import styles from "./bans.module.css";
import shared from "../admin.module.css";
import { Icon } from "../../../../app/icons";
import { useAdminUsers, type AdminUser } from "../../../../api/admin/admin";
import {
  useBanDomain,
  useBanUser,
  useDomainBans,
  useLiftDomainBan,
  useLiftUserBan,
  useUserBans,
  type DomainBan,
  type UserBan,
} from "../../../../api/admin/bans";
import { AdminShell } from "../AdminShell";
import { Reading } from "../shared/chrome";
import { BAN_EFFECT, BAN_KEYS } from "../shared/confirm";
import { dateTime } from "../shared/format";
import { RegisterError, TableSkeleton } from "../shared/states";
import { refusalSentence } from "../../../../api/errors";

type Notify = (text: string, ok: boolean) => void;

/** The API's own sentence for a refusal — "already banned", "a platform staff
 *  account cannot be banned", "enter a valid domain" — never a message this page
 *  invents, so the reader sees the real reason. */
const apiMessage = (err: unknown, fallback: string): string =>
  refusalSentence(err, { fallback });

export function AdminBansPage() {
  const { toasts, push, dismiss } = useToasts();
  const notify: Notify = (text, ok) => push({ message: text, tone: ok ? "success" : "danger" });

  return (
    <AdminShell subtitle="Accounts and email domains that can no longer reach the product">
      <Stack gap={7} align="stretch">
        <BannedUsersCard notify={notify} />
        <BannedDomainsCard notify={notify} />
      </Stack>
      <ToastViewport toasts={toasts} onDismiss={dismiss} position="bottom-right" />
    </AdminShell>
  );
}

/** The "banned by / when" tail both registers share. */
function ActorCells({ ban }: { ban: UserBan | DomainBan }) {
  return (
    <>
      <td>{ban.created_by_email ? <Reading muted>{ban.created_by_email}</Reading> : <span className="alk-caption">—</span>}</td>
      <td><Reading muted>{dateTime(ban.created_at)}</Reading></td>
    </>
  );
}

/** The disclosure that holds a register's lifted rows — closed by default (the
 *  live list is what an operator acts on) but never dropped: a lifted ban is the
 *  record that someone undid it. */
function LiftedHistory({ count, label, children }: { count: number; label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  if (count === 0) return null;
  return (
    <div className={styles.history}>
      <Button
        variant="secondary"
        fill="ghost"
        leftSection={<Icon name={open ? "chevronDown" : "chevronRight"} size={14} />}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        {`Lifted (${count})`}
      </Button>
      <Collapse open={open} aria-label={label}>
        <div className={styles.historyBody}>{children}</div>
      </Collapse>
    </div>
  );
}

// ---- banned users ---------------------------------------------------------

function BannedUsersCard({ notify }: { notify: Notify }) {
  const bans = useUserBans();
  const lift = useLiftUserBan();
  const [error, setError] = useState<string | null>(null);

  const active = useMemo(() => (bans.data ?? []).filter((b) => b.active), [bans.data]);
  const lifted = useMemo(() => (bans.data ?? []).filter((b) => !b.active), [bans.data]);

  if (bans.isError) {
    return (
      <RegisterError
        what="banned users"
        error={bans.error}
        onRetry={() => void bans.refetch()}
      />
    );
  }

  const onLift = (ban: UserBan) => {
    setError(null);
    lift.mutate(ban.user_id, {
      onSuccess: () => notify(`Ban lifted for ${ban.user_email}.`, true),
      onError: (err) => {
        const message = apiMessage(err, "Could not lift the ban.");
        setError(message);
        notify(message, false);
      },
    });
  };

  return (
    <Card title="Banned users" icon={<Icon name="triBarred" size={16} />} headingLevel={2}>
      <Stack gap={5} align="stretch">
        <BanUserForm notify={notify} banned={new Set(active.map((b) => b.user_id))} />
        {error ? <Callout tone="danger" role="alert">{error}</Callout> : null}
        {!bans.data ? (
          <TableSkeleton rows={3} cols={4} />
        ) : active.length === 0 ? (
          <p className="alk-caption">No accounts are banned.</p>
        ) : (
          <Table responsive stackPrimary={0} colWidths={[undefined, undefined, "14rem", "12rem", "7rem"]} columns={["User", "Reason", "Banned by", "When", ""]}>
            {active.map((ban) => (
              <tr key={ban.id}>
                <td>
                  <Stack gap={1} align="start">
                    <span className="alk-strong">{ban.user_display_name}</span>
                    <Reading muted>{ban.user_email}</Reading>
                  </Stack>
                </td>
                <td>{ban.reason ? <span>{ban.reason}</span> : <span className="alk-caption">No reason recorded</span>}</td>
                <ActorCells ban={ban} />
                <td>
                  <Button variant="secondary" fill="outline" onClick={() => onLift(ban)} loading={lift.isPending && lift.variables === ban.user_id}>
                    Lift
                  </Button>
                </td>
              </tr>
            ))}
          </Table>
        )}
        <LiftedHistory count={lifted.length} label="Lifted user bans">
          <Table responsive colWidths={[undefined, undefined, "12rem", "14rem"]} columns={["User", "Reason", "Lifted", "Lifted by"]}>
            {lifted.map((ban) => (
              <tr key={ban.id}>
                <td><Reading muted>{ban.user_email}</Reading></td>
                <td><span className="alk-caption">{ban.reason || "—"}</span></td>
                <td><Reading muted>{ban.lifted_at ? dateTime(ban.lifted_at) : "—"}</Reading></td>
                <td><Reading muted>{ban.lifted_by_email ?? "—"}</Reading></td>
              </tr>
            ))}
          </Table>
        </LiftedHistory>
      </Stack>
    </Card>
  );
}

/** Pick an account off the users register, then confirm. The search runs over the
 *  register the admin console already loads, so a ban starts from the same list the
 *  operator was just reading rather than from a bare id field. */
function BanUserForm({ notify, banned }: { notify: Notify; banned: Set<string> }) {
  const users = useAdminUsers();
  const ban = useBanUser();
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<AdminUser | null>(null);
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q || picked) return [];
    return (users.data ?? [])
      .filter((u) => !banned.has(u.id))
      .filter((u) => u.email.toLowerCase().includes(q) || u.display_name.toLowerCase().includes(q) || u.id.toLowerCase().includes(q))
      .slice(0, 6);
  }, [users.data, query, picked, banned]);

  const reset = () => {
    setPicked(null);
    setQuery("");
    setReason("");
  };

  const submit = () => {
    if (!picked) return;
    setError(null);
    ban.mutate(
      { userId: picked.id, reason: reason.trim() },
      {
        onSuccess: () => {
          notify(`${picked.email} is banned.`, true);
          setConfirming(false);
          reset();
        },
        onError: (err) => {
          const message = apiMessage(err, "Could not ban the user.");
          setError(message);
          setConfirming(false);
        },
      },
    );
  };

  return (
    <Stack gap={3} align="stretch">
      <Toolbar
        end={
          <Button leftSection={<Icon name="lock" size={16} />} disabled={!picked} onClick={() => setConfirming(true)}>
            Ban user
          </Button>
        }
      >
        <div className={shared.formRow} style={{ display: "flex", gap: "var(--alkSpace5)", flex: "1 1 auto" }}>
          <TextInput
            label="User"
            value={picked ? `${picked.display_name} · ${picked.email}` : query}
            onChange={(e) => {
              setPicked(null);
              setQuery(e.target.value);
            }}
            placeholder="Search name, email, or id"
            aria-label="Search for a user to ban"
          />
          <TextInput label="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why this account is banned" />
        </div>
      </Toolbar>
      {matches.length > 0 ? (
        <ul className={styles.picker} aria-label="Matching users">
          {matches.map((u) => (
            <li key={u.id}>
              <button type="button" className={styles.pickerRow} onClick={() => setPicked(u)}>
                <span className="alk-strong">{u.display_name}</span>
                <Reading muted>{u.email}</Reading>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {error ? <Callout tone="danger" role="alert">{error}</Callout> : null}
      <ConfirmDialog
        open={confirming}
        onClose={() => setConfirming(false)}
        title={`Ban ${picked?.email ?? "this account"}?`}
        consequence={BAN_EFFECT}
        confirmLabel={BAN_KEYS.account}
        tone="destructive"
        busy={ban.isPending}
        onConfirm={submit}
      />
    </Stack>
  );
}

// ---- banned domains -------------------------------------------------------

function BannedDomainsCard({ notify }: { notify: Notify }) {
  const bans = useDomainBans();
  const lift = useLiftDomainBan();
  const [error, setError] = useState<string | null>(null);

  const active = useMemo(() => (bans.data ?? []).filter((b) => b.active), [bans.data]);
  const lifted = useMemo(() => (bans.data ?? []).filter((b) => !b.active), [bans.data]);

  if (bans.isError) {
    return (
      <RegisterError
        what="banned domains"
        error={bans.error}
        onRetry={() => void bans.refetch()}
      />
    );
  }

  const onLift = (ban: DomainBan) => {
    setError(null);
    lift.mutate(ban.domain, {
      onSuccess: () => notify(`Ban lifted for ${ban.domain}.`, true),
      onError: (err) => {
        const message = apiMessage(err, "Could not lift the ban.");
        setError(message);
        notify(message, false);
      },
    });
  };

  return (
    <Card title="Banned domains" icon={<Icon name="mail" size={16} />} headingLevel={2}>
      <Stack gap={5} align="stretch">
        <p className="alk-caption" style={{ margin: 0 }}>
          Every account on the domain is refused. Platform staff accounts are exempt, so banning your own
          domain cannot lock the console out.
        </p>
        <BanDomainForm notify={notify} />
        {error ? <Callout tone="danger" role="alert">{error}</Callout> : null}
        {!bans.data ? (
          <TableSkeleton rows={3} cols={4} />
        ) : active.length === 0 ? (
          <p className="alk-caption">No domains are banned.</p>
        ) : (
          <Table responsive stackPrimary={0} colWidths={[undefined, undefined, "14rem", "12rem", "7rem"]} columns={["Domain", "Reason", "Banned by", "When", ""]}>
            {active.map((ban) => (
              <tr key={ban.id}>
                <td><Reading>{ban.domain}</Reading></td>
                <td>{ban.reason ? <span>{ban.reason}</span> : <span className="alk-caption">No reason recorded</span>}</td>
                <ActorCells ban={ban} />
                <td>
                  <Button variant="secondary" fill="outline" onClick={() => onLift(ban)} loading={lift.isPending && lift.variables === ban.domain}>
                    Lift
                  </Button>
                </td>
              </tr>
            ))}
          </Table>
        )}
        <LiftedHistory count={lifted.length} label="Lifted domain bans">
          <Table responsive colWidths={[undefined, undefined, "12rem", "14rem"]} columns={["Domain", "Reason", "Lifted", "Lifted by"]}>
            {lifted.map((ban) => (
              <tr key={ban.id}>
                <td><Reading muted>{ban.domain}</Reading></td>
                <td><span className="alk-caption">{ban.reason || "—"}</span></td>
                <td><Reading muted>{ban.lifted_at ? dateTime(ban.lifted_at) : "—"}</Reading></td>
                <td><Reading muted>{ban.lifted_by_email ?? "—"}</Reading></td>
              </tr>
            ))}
          </Table>
        </LiftedHistory>
      </Stack>
    </Card>
  );
}

function BanDomainForm({ notify }: { notify: Notify }) {
  const ban = useBanDomain();
  const [domain, setDomain] = useState("");
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The only thing refused here is an empty field. Normalization and the
  // "is this even a domain?" judgement belong to the server, which is the one
  // authority both the ban check and the signup refusal read.
  const typed = domain.trim();

  const submit = () => {
    setError(null);
    ban.mutate(
      { domain, reason: reason.trim() },
      {
        onSuccess: (created) => {
          notify(`${created.domain} is banned.`, true);
          setConfirming(false);
          setDomain("");
          setReason("");
        },
        onError: (err) => {
          const message = apiMessage(err, "Could not ban the domain.");
          setError(message);
          setConfirming(false);
        },
      },
    );
  };

  return (
    <Stack gap={3} align="stretch">
      <Toolbar
        end={
          <Button leftSection={<Icon name="lock" size={16} />} disabled={typed.length === 0} onClick={() => setConfirming(true)}>
            Ban domain
          </Button>
        }
      >
        <div className={shared.formRow} style={{ display: "flex", gap: "var(--alkSpace5)", flex: "1 1 auto" }}>
          <TextInput label="Domain" value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="acme.com" />
          <TextInput label="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why this domain is banned" />
        </div>
      </Toolbar>
      {error ? <Callout tone="danger" role="alert">{error}</Callout> : null}
      <ConfirmDialog
        open={confirming}
        onClose={() => setConfirming(false)}
        title={`Ban everyone at ${typed || "this domain"}?`}
        consequence={`Every account on the domain is refused. ${BAN_EFFECT}`}
        confirmLabel={BAN_KEYS.domain}
        tone="destructive"
        busy={ban.isPending}
        onConfirm={submit}
      />
    </Stack>
  );
}

