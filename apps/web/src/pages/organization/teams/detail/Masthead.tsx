import type { ReactNode } from "react";

import { Inline, cx } from "@alkera/ui";

import { ActionsMenu, type MenuItem } from "./actions";
import { teamById, type Graph } from "../data/model";
import shell from "../teams.module.css";

/** The team masthead — one identity row: the team name (Newsreader) on the left, the primary action
 *  plus an overflow button sized to match it (both 40px, one optical baseline) on the right. The
 *  member count lives on the roster plate's reading, so the title row stays a single line. Governance
 *  and provenance moved off the masthead into the context column's register plates. */
export function Masthead({
  graph,
  teamId,
  primaryAction,
  menuItems,
}: {
  graph: Graph;
  teamId: string;
  primaryAction: ReactNode | null;
  menuItems: MenuItem[];
}) {
  const team = teamById(graph, teamId);
  if (!team) return null;

  // The act cluster appears only when the viewer can manage the team; otherwise
  // the masthead is the name alone (no empty menu, no dead primary).
  const hasActions = primaryAction != null || menuItems.length > 0;

  return (
    <header className={shell.mast}>
      {/* An <h2> in the h1's type: the app masthead is the page's one <h1>. */}
      <h2 className={cx("alk-h1", "alk-truncate")} style={{ margin: 0, flex: "1 1 auto", minWidth: 0 }}>
        {team.name}
      </h2>
      {hasActions ? (
        <Inline gap={3} wrap={false} style={{ flex: "0 0 auto" }}>
          {primaryAction}
          {menuItems.length > 0 ? <ActionsMenu items={menuItems} label={`Manage ${team.name}`} variant="header" /> : null}
        </Inline>
      ) : null}
    </header>
  );
}
