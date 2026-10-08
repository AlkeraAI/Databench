// The "Connections" plate on the team detail — what THIS team has configured,
// and the way through to changing it.
//
// Adding and editing live on the Connections page (`/connections`), where one
// dialog serves a person's own connections and their teams' alike. Keeping a
// second add dialog here would mean two flows to hold in step for one save, so
// this plate reads the team's rows and links across with the team already
// picked.

import { Button, Card, Inline, Pill, Spinner, Stack, Table } from "@alkera/ui";
import { LogoChip, presentBadge, type BadgeSource } from "@alkera/ui/connections";
import { IconPlugConnected, IconSettings } from "@tabler/icons-react";
import { useNavigate } from "react-router-dom";

import { useConnectionForms, useTeamConnections, type TeamConnection } from "../../../api/teamConnections";

const COLUMNS = ["Connection", "Integration", "Delivery", "Status"];
const COL_WIDTHS: Array<string | number | undefined> = [undefined, 188, 120, 188];

/** The path that opens the Connections page with this team already picked, so an
 *  admin arriving from a team does not have to find it again in the picker. */
export const manageConnectionsHref = (teamId: string) => `/connections?team=${teamId}`;

export function ConnectionsPlate({ teamId }: { teamId: string; teamName: string }) {
  const navigate = useNavigate();
  const admin = useTeamConnections(teamId);
  const forms = useConnectionForms(true);
  const rows: TeamConnection[] = admin.data ?? [];
  const titleOf = (plugin: string) =>
    (forms.data?.connectors ?? []).find((d) => d.name === plugin)?.title ?? plugin;

  return (
    <Card
      variant="panel"
      headerDivider
      icon={<IconPlugConnected size={15} stroke={1.8} />}
      title="Connections"
      headingLevel={3}
      actions={
        rows.length > 0 ? (
          <Pill numeric tone="brand">
            {rows.length}
          </Pill>
        ) : undefined
      }
    >
      <Stack gap={3} align="stretch">
        {/* A heading over an empty frame reads as something that failed to load.
            The plate says which it is, then offers the one way to change it. */}
        {rows.length === 0 ? (
          admin.isLoading ? null : (
            <p className="alk-caption">Nothing this team's chats can query yet.</p>
          )
        ) : (
          <Table style={{ width: "100%" }} columns={COLUMNS} colWidths={COL_WIDTHS} responsive stackAt={860} pageSize={10}>
            {rows.map((c) => (
              <tr key={c.id}>
                <td>
                  <div className="alk-strong">{c.handle}</div>
                  {c.created_by_name ? (
                    <div className="alk-caption">Added by {c.created_by_name}</div>
                  ) : null}
                </td>
                <td style={{ color: "var(--alkSecondaryText)", whiteSpace: "nowrap" }}>
                  <Inline gap={3} wrap={false}>
                    <LogoChip pluginId={c.plugin} size={24} />
                    {titleOf(c.plugin)}
                  </Inline>
                </td>
                <td style={{ color: "var(--alkSecondaryText)", whiteSpace: "nowrap" }}>
                  {c.auto_add ? "Auto-added" : "Suggested"}
                </td>
                <td>
                  <StatusPill connection={c} />
                </td>
              </tr>
            ))}
          </Table>
        )}
        <div>
          <Button
            variant="secondary"
            leftSection={<IconSettings size={15} stroke={1.8} />}
            onClick={() => navigate(manageConnectionsHref(teamId))}
          >
            Manage connections
          </Button>
        </div>
      </Stack>
    </Card>
  );
}

/** The server's derived badge, in the words every Alkera surface shows for it. */
function StatusPill({ connection }: { connection: TeamConnection }) {
  const { label, tone, busy } = presentBadge(connection as BadgeSource);
  return (
    <Pill tone={tone} icon={busy ? <Spinner size={13} /> : undefined}>
      {label}
    </Pill>
  );
}
