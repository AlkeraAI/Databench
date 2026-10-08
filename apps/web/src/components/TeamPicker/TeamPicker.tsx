// Who owns the thing being created: this person, or one of their org's teams.
//
// The control is the org's team tree with one extra row above it — "Just me" —
// so the two answers sit in one list and the choice is a single selection rather
// than a mode switch beside a picker. Only the teams the caller administers are
// selectable; the rest stay on screen, visibly refused, because a tree with its
// unselectable branches removed is no longer the shape of the org the person is
// picking inside.
//
// The tree shape, connectors, keyboard model and selection treatment all come
// from the shared `Tree` (the same one the org's Team Tree renders); this only
// maps teams onto its node model and decides which rows are selectable.

import { useMemo, useState } from "react";

import { Tree, type TreeNode } from "@alkera/ui";

import styles from "./TeamPicker.module.css";

/** The shape this picker needs off a team — a subset of the SDK's `TeamRead`, so
 *  either the portal's `useTeams()` rows or a test's literals satisfy it. */
export interface PickableTeam {
  id: string;
  name: string;
  parent_team_id?: string | null;
  is_root?: boolean;
}

/** What the picker is set to. "me" is the person themselves; "team" names one. */
export type TeamPickerValue = { kind: "me" } | { kind: "team"; teamId: string };

/** The id the "Just me" row carries inside the tree. Not a team id, and no team
 *  can collide with it: team ids are UUIDs. */
export const ME_NODE_ID = "__me__";

export const NOT_AN_ADMIN_REASON = "You aren't an admin of this team";

export interface TeamPickerProps {
  value: TeamPickerValue;
  onChange: (value: TeamPickerValue) => void;
  /** Every team in the caller's org. Order does not matter; the tree is rebuilt
   *  from `parent_team_id`. */
  teams: PickableTeam[];
  /** The teams the caller may pick — the ones they administer, descent included.
   *  Everything else renders refused. */
  adminTeamIds: Set<string>;
  /** The label on the personal row. Named because the sentence differs by
   *  surface ("Just me" when adding, "Only you" when explaining). */
  meLabel?: string;
  /** Height of the scrolling tree body. `sm` fits inside a dialog beside a form;
   *  `md` is the standalone size. */
  size?: "sm" | "md";
  /** Accessible name for the tree. */
  ariaLabel?: string;
  /** Renders every row refused — the picker still shows the shape, and nothing
   *  in it can be chosen (a dialog mid-save). */
  disabled?: boolean;
}

/** Roots first, then each team's children, both alphabetical — a stable order
 *  that does not depend on how the rows arrived. */
function childrenOf(teams: PickableTeam[], parentId: string | null): PickableTeam[] {
  return teams
    .filter((t) => (t.parent_team_id ?? null) === parentId)
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** The org tree as `Tree` nodes, each row selectable only where the caller
 *  administers it. Cycle-safe: a team already placed is never placed again, so a
 *  malformed parent chain yields a truncated tree rather than an endless one. */
function toNodes(
  teams: PickableTeam[],
  adminTeamIds: Set<string>,
  disabled: boolean,
  seen: Set<string> = new Set(),
  parentId: string | null = null,
): TreeNode[] {
  return childrenOf(teams, parentId)
    .filter((team) => !seen.has(team.id))
    .map((team) => {
      seen.add(team.id);
      const refused = disabled || !adminTeamIds.has(team.id);
      return {
        id: team.id,
        label: team.name,
        emphasized: team.is_root,
        disabled: refused,
        disabledReason: disabled ? undefined : NOT_AN_ADMIN_REASON,
        trailing: refused && !disabled ? <span className={styles.reason}>Can&apos;t add here</span> : undefined,
        children: toNodes(teams, adminTeamIds, disabled, seen, team.id),
      };
    });
}

/**
 * The owner picker. Selection is controlled: the caller owns `value` and reacts
 * to `onChange`; expansion is local, because which branches are open is the
 * reader's business and not the form's.
 */
export function TeamPicker({
  value,
  onChange,
  teams,
  adminTeamIds,
  meLabel = "Just me",
  size = "md",
  ariaLabel = "Who this belongs to",
  disabled = false,
}: TeamPickerProps) {
  const nodes = useMemo<TreeNode[]>(
    () => [
      {
        id: ME_NODE_ID,
        label: meLabel,
        emphasized: true,
        disabled,
        trailing: <span className={styles.reason}>Only you</span>,
      },
      ...toNodes(teams, adminTeamIds, disabled),
    ],
    [teams, adminTeamIds, meLabel, disabled],
  );

  // Every branch starts open. The org tree is small, and a picker whose answer
  // is three levels down behind two closed twists reads as having no answer.
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const expanded = useMemo(() => {
    const all = new Set<string>();
    const walk = (list: TreeNode[]) => {
      for (const n of list) {
        if (!collapsed.has(n.id)) all.add(n.id);
        if (n.children?.length) walk(n.children);
      }
    };
    walk(nodes);
    return all;
  }, [nodes, collapsed]);

  return (
    <div className={styles.picker} data-size={size}>
      <Tree
        nodes={nodes}
        ariaLabel={ariaLabel}
        selectedId={value.kind === "me" ? ME_NODE_ID : value.teamId}
        expanded={expanded}
        onSelect={(id) => onChange(id === ME_NODE_ID ? { kind: "me" } : { kind: "team", teamId: id })}
        onToggle={(id) =>
          setCollapsed((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
          })
        }
      />
    </div>
  );
}
