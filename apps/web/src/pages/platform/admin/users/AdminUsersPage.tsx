// The users register — every enrolled account with its abuse-forensics
// columns: verification state (plus a disposable-domain seal), signup date,
// month-to-date spend, and the recorded signup IP. A search reconciles by
// name, email, domain, IP, or id; the sort toggle flips between the newest
// signups (spam-wave triage) and the month's top spenders (damage triage).

import { useMemo, useState, type CSSProperties } from "react";

import { Card, Inline, SegmentedControl, Table, TextInput } from "@alkera/ui";

import shared from "../admin.module.css";
import { Icon } from "../../../../app/icons";
import { useAdminUsers } from "../../../../api/admin/admin";
import { AdminShell } from "../AdminShell";
import { IdCell, LinkCell, Reading, Dash } from "../shared/chrome";
import { date, usd } from "../shared/format";
import { BannedPill, DisposableSeal, PlatformRolePill, VerifiedPill } from "../shared/status";
import { RegisterEmpty, RegisterError, TableSkeleton } from "../shared/states";

const SORTS = [
  { key: "newest", label: "Newest" },
  { key: "spend", label: "Top spend" },
] as const;

export function AdminUsersPage() {
  const users = useAdminUsers();
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<string>("newest");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = users.data ?? [];
    const matched = !q
      ? all
      : all.filter(
          (u) =>
            u.display_name.toLowerCase().includes(q) ||
            u.email.toLowerCase().includes(q) ||
            u.id.toLowerCase().includes(q) ||
            (u.signup_ip ?? "").includes(q) ||
            (u.org_name ?? "").toLowerCase().includes(q),
        );
    if (sort === "spend") {
      return [...matched].sort((a, b) => b.mtd_billed_nanos - a.mtd_billed_nanos);
    }
    return matched; // the register arrives newest-first from the backend
  }, [users.data, query, sort]);

  return (
    <AdminShell subtitle="Accounts on the platform">
      {users.isError ? (
        <RegisterError what="users" error={users.error} onRetry={() => void users.refetch()} />
      ) : !users.data ? (
        <TableSkeleton rows={6} cols={7} />
      ) : users.data.length === 0 ? (
        <RegisterEmpty title="No users yet" body="Accounts appear here once they enroll." mark={<Icon name="users" size={40} />} />
      ) : (
        <Card
          title="Users"
          // The shell's masthead owns the h1; the register is the section under it, not two levels down.
          headingLevel={2}
          icon={<Icon name="users" size={16} />}
          actions={
            <Inline gap={3}>
              <SegmentedControl options={SORTS} value={sort} onChange={setSort} label="Sort users" />
              <TextInput type="search" collapsible rootStyle={{ "--alk-search-w": "22rem" } as CSSProperties} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search name, email, IP, org, or id" aria-label="Search users" />
            </Inline>
          }
        >
          {filtered.length === 0 ? (
            <p className="alk-caption">No matches for “{query}”.</p>
          ) : (
            <Table
              responsive
              stackPrimary={1}
              colWidths={["12rem", undefined, "8.5rem", "8rem", "7.5rem", "10rem", "9rem", "7rem"]}
              columns={["Name", "Email", "Verified", "Signed up", <span key="mtd" className="alk-num">This month</span>, "Signup IP", "Platform role", "ID"]}
              pageSize={25}
            >
              {filtered.map((u) => (
                <tr key={u.id} className={u.banned ? shared.bannedRow : undefined}>
                  <td><LinkCell to={`/admin/users/${u.id}`} fallback={u.email}>{u.display_name}</LinkCell></td>
                  <td>
                    <Inline gap={2}>
                      <Reading muted>{u.email}</Reading>
                      {u.banned ? <BannedPill reason={u.ban_reason} /> : null}
                      {u.disposable_email ? <DisposableSeal /> : null}
                    </Inline>
                  </td>
                  <td><VerifiedPill verifiedAt={u.email_verified_at} /></td>
                  <td><Reading muted>{date(u.created_at)}</Reading></td>
                  <td><Reading align="end">{u.mtd_billed_nanos > 0 ? usd(u.mtd_billed_nanos) : "—"}</Reading></td>
                  <td>{u.signup_ip ? <Reading muted>{u.signup_ip}</Reading> : <Dash />}</td>
                  <td>{u.platform_role ? <PlatformRolePill role={u.platform_role} /> : <Dash />}</td>
                  <td><IdCell id={u.id} /></td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      )}
    </AdminShell>
  );
}
