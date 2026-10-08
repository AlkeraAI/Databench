import { useMemo } from "react";

import { Tree, type TreeNode } from "@alkera/ui";

import { childrenOf, memberCountLabel, teamMemberCount, type Graph, type Team } from "../data/model";
import { administers, isOrgAdmin } from "../data/permissions";

interface TreeProps {
  graph: Graph;
  /** Undefined while the viewer is choosing between the teams they lead. */
  selectedId: string | undefined;
  expanded: Set<string>;
  onSelect: (id: string) => void;
  onToggle: (id: string) => void;
}

const roots = (g: Graph): Team[] => g.teams.filter((t) => t.parentId === null).sort((a, b) => a.name.localeCompare(b.name));

/** Map the org graph onto the shared Tree's node model — roots first, each carrying its direct
 *  member count (the people holding a row on it; admins reaching it by descent hold none) as the
 *  trailing reading and its descent as children. Root teams read emphasized. For someone who is not
 *  an org admin the tree shows what descent grants: the teams they administer open, every other
 *  team stays on screen (the shape of the org) but cannot be opened. */
function toNodes(graph: Graph): TreeNode[] {
  const restricted = !isOrgAdmin(graph);
  const build = (team: Team): TreeNode => {
    const count = teamMemberCount(graph, team.id);
    const closed = restricted && !administers(graph, team.id);
    return {
      id: team.id,
      label: team.name,
      emphasized: team.isRoot,
      disabled: closed || undefined,
      disabledReason: closed ? "You don’t administer this team" : undefined,
      trailing: (
        <span className="alk-num" aria-label={memberCountLabel(count)}>
          {count}
        </span>
      ),
      children: childrenOf(graph, team.id).map(build),
    };
  };
  return roots(graph).map(build);
}

/** The Team Tree — the org's navigable index, opened on demand inside the side panel (the drawer
 *  chrome — header, close, scroll body — is the SidePanel's). The tree shape, connectors, keyboard
 *  model, and selection treatment all come from the shared `Tree`; this only maps the graph onto it. */
export function TeamTree({ graph, selectedId, expanded, onSelect, onToggle }: TreeProps) {
  const nodes = useMemo(() => toNodes(graph), [graph]);
  return <Tree nodes={nodes} selectedId={selectedId} expanded={expanded} onSelect={onSelect} onToggle={onToggle} ariaLabel="Teams" />;
}
