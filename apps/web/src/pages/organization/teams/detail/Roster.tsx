import { Identity, Table, cx } from "@alkera/ui";
import { Icon } from "../../../../app/icons";
import { ActionsMenu, RoleChip, RoleSelect, type MenuItem } from "./actions";
import { descentCountLabel, memberCountLabel, type Role, type RosterEntry } from "../data/model";
import styles from "./Roster.module.css";

interface RosterProps {
  entries: RosterEntry[];
  teamName: string;
  /** The current user's id, so the viewer's own row reads "You" + "Leave team". */
  viewerId: string;
  /** Whether the viewer may remove this person's row here (never themself from the org root). */
  canRemove: (personId: string) => boolean;
  /** Whether the viewer administers any other team to move a member to. */
  canMove: boolean;
  onChangeRole: (personId: string, role: Role) => void;
  onRemove: (entry: RosterEntry) => void;
  onMove: (entry: RosterEntry) => void;
  onSelectTeam: (teamId: string) => void;
  /** Open the admin's read-only usage view for a member. Omitted when unavailable. */
  onViewPlan?: (entry: RosterEntry) => void;
}

// The ledger columns (Member flexes; Role / Standing / Joined / Actions hold fixed widths). The empty
// trailing header is the per-row overflow menu — no label, so its stacked card cell runs full width.
const COLUMNS = ["Member", "Role", "Standing", "Joined", ""];
// Joined holds a fixed-format date ("30 Jun 2026"): ~92px of tabular figures + the cells' 2×16px
// padding — 100px truncated it to "30 Jun …".
const COL_WIDTHS: Array<string | number | undefined> = [undefined, 176, 200, 128, 64];

/** The roster ledger — the shared `Table`, showing ONE listing at a time (direct members, or
 *  members by descent; the two can overlap). A direct member's role is edited inline unless descent
 *  decides it: an admin reaching the team from above holds admin here whatever the row says, so the
 *  control is locked and the fact stated on the row — the role is changed on the team it comes
 *  from. A by-descent entry is read-only here and stamped with that team, as a link. The Table
 *  stacks each row into a labelled card below its narrow threshold, so the stamp stays legible. */
export function Roster({ entries, teamName, viewerId, canRemove, canMove, onChangeRole, onRemove, onMove, onSelectTeam, onViewPlan }: RosterProps) {
  const listing = entries[0]?.listing ?? "direct";
  const count = listing === "descent" ? descentCountLabel(entries.length) : memberCountLabel(entries.length);
  return (
    <Table style={{ width: "100%", padding: "var(--alkSpace3)" }} columns={COLUMNS} colWidths={COL_WIDTHS} responsive pageSize={10} footerStart={count}>
      {entries.map((entry) => {
        const { person, role, directRole, descentFrom } = entry;
        const isYou = person.id === viewerId;
        const hasRow = directRole !== null;
        // The member drawer (usage; account + budget for an org admin) is offered on EVERY row.
        const view: MenuItem[] = onViewPlan
          ? [{ key: "plan", label: "View member", icon: "user" as const, onClick: () => onViewPlan(entry) }]
          : [];
        const actions: MenuItem[] = hasRow
          ? [
              ...view,
              ...(canMove ? [{ key: "move", label: "Move to team…", icon: "move" as const, onClick: () => onMove(entry) }] : []),
              ...(canRemove(person.id)
                ? [
                    {
                      key: "remove",
                      label: isYou ? "Leave this team" : "Remove from team",
                      icon: "trash" as const,
                      danger: true,
                      separated: true,
                      onClick: () => onRemove(entry),
                    },
                  ]
                : []),
            ]
          : [...view, { key: "open", label: `Open ${descentFrom!.name}`, icon: "descend", onClick: () => onSelectTeam(descentFrom!.id) }];

        return (
          <tr key={`${entry.listing}:${person.id}`}>
            <td>
              <Identity
                initials={person.initials}
                name={person.name}
                nameTrailing={
                  isYou ? (
                    <span className="alk-meta" style={{ marginLeft: "var(--alkSpace3)", fontWeight: "var(--alkWeightSemibold)" }}>
                      You
                    </span>
                  ) : null
                }
                secondary={person.email}
              />
            </td>
            <td>
              {descentFrom ? (
                // Locked: descent decides the role here. The fact rides the control itself.
                <span className={styles.src} style={{ flexDirection: "column", alignItems: "flex-start", gap: 2 }}>
                  <button
                    type="button"
                    disabled
                    className={styles.locked}
                    aria-label={`Role for ${person.name}: Admin, by descent from ${descentFrom.name}`}
                    title={`Admin by descent from ${descentFrom.name}. Change it there.`}
                  >
                    <RoleChip role="admin" />
                  </button>
                  <span className="alk-meta">
                    by descent from{" "}
                    <button type="button" className="alk-link" onClick={() => onSelectTeam(descentFrom.id)}>
                      {descentFrom.name}
                    </button>
                  </span>
                </span>
              ) : (
                <RoleSelect value={role} who={person.name} onChange={(r) => onChangeRole(person.id, r)} />
              )}
            </td>
            <td>
              {entry.listing === "direct" ? (
                <span className={styles.src} style={{ color: "var(--alkPrimaryText)" }}>Direct member</span>
              ) : (
                <span className={styles.src}>
                  <Icon name="descend" size={15} style={{ flex: "0 0 auto" }} />
                  {hasRow ? "also a direct member · " : ""}from{" "}
                  <button type="button" className={cx("alk-link", "alk-truncate")} onClick={() => onSelectTeam(descentFrom!.id)}>
                    {descentFrom!.name}
                  </button>
                </span>
              )}
            </td>
            <td className="alk-num" style={{ color: "var(--alkSecondaryText)", whiteSpace: "nowrap" }}>
              {entry.joined}
            </td>
            {/* The overflow menu sits flush to the row's trailing edge (the Dropdown is inline-flex, so
                text-align carries it); the stacked-card grid overrides this at narrow widths. */}
            <td style={{ textAlign: "right" }}>
              <ActionsMenu items={actions} label={`Actions for ${person.name} in ${teamName}`} />
            </td>
          </tr>
        );
      })}
    </Table>
  );
}
