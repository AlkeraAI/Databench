// The organizations register — every house on the platform. A search reconciles by
// name or id; the table links each org into its detail register. A registrar mints a
// new house (and its first admin) through the create modal.

import { useMemo, useState, type CSSProperties } from "react";

import { Button, Card, Pill, Table, TextInput, useAbbreviate } from "@alkera/ui";

import shared from "../admin.module.css";
import { TopbarActions } from "../../../../app/Topbar";
import { Icon } from "../../../../app/icons";
import { useAdminOrgs } from "../../../../api/admin/admin";
import { AdminShell } from "../AdminShell";
import { LinkCell, Reading } from "../shared/chrome";
import { date } from "../shared/format";
import { CreateOrgModal } from "../shared/modals";
import { RegisterEmpty, RegisterError, TableSkeleton } from "../shared/states";

export function AdminOrgsPage() {
  const orgs = useAdminOrgs();
  const [query, setQuery] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const abbrev = useAbbreviate();

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = orgs.data ?? [];
    if (!q) return all;
    return all.filter((o) => o.name.toLowerCase().includes(q) || o.id.toLowerCase().includes(q));
  }, [orgs.data, query]);

  const newButton = (
    <Button leftSection={<Icon name="plus" size={16} />} onClick={() => setCreateOpen(true)}>
      New org
    </Button>
  );

  return (
    <AdminShell subtitle="Organizations on the platform">
      <TopbarActions>{newButton}</TopbarActions>

      {orgs.isError ? (
        <RegisterError what="organizations" error={orgs.error} onRetry={() => void orgs.refetch()} />
      ) : !orgs.data ? (
        <TableSkeleton rows={5} cols={4} />
      ) : orgs.data.length === 0 ? (
        <RegisterEmpty
          title="No organizations yet"
          body="Create the first house and its initial admin to start enrolling accounts."
          action={newButton}
        />
      ) : (
        <Card
          title="Organizations"
          // The shell's masthead owns the h1; the register is the section under it, not two levels down.
          headingLevel={2}
          icon={<Icon name="building" size={16} />}
          actions={<TextInput type="search" collapsible rootStyle={{ "--alk-search-w": "22rem" } as CSSProperties} value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search by name or id" aria-label="Search organizations" />}
        >
          {filtered.length === 0 ? (
            <p className="alk-caption">No matches for “{query}”.</p>
          ) : (
            <Table responsive colWidths={[undefined, "5rem", "7.5rem", "7rem"]} columns={["Name", <span key="m" className={shared.readingEnd}>Members</span>, "Created", "ID"]} pageSize={20}>
              {filtered.map((o) => (
                <tr key={o.id}>
                  <td><LinkCell to={`/admin/orgs/${o.id}`}>{o.name}</LinkCell></td>
                  <td>
                    <span className={shared.readingEnd}>
                      <Pill tone="neutral">{abbrev(o.member_count)}</Pill>
                    </span>
                  </td>
                  <td><Reading>{date(o.created_at)}</Reading></td>
                  <td><Reading>{o.id.slice(0, 8)}</Reading></td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      )}

      <CreateOrgModal open={createOpen} onClose={() => setCreateOpen(false)} />
    </AdminShell>
  );
}
