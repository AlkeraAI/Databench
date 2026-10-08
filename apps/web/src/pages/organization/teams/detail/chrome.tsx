import { Fragment } from "react";

import { Icon } from "../../../../app/icons";
import { ancestorsOf, teamById, type Graph } from "../data/model";
import styles from "./chrome.module.css";
import { cx } from "@alkera/ui";

/**
 * The parentage breadcrumb — the position spine. It carries the full ancestor chain (which the
 * single-node masthead can't), and it is the org's position record now that the tree is an
 * on-demand panel rather than a persistent rail. The current team is the (non-interactive) leaf.
 * It names other teams, so the detail withholds it from a viewer who doesn't manage the team.
 */
export function Breadcrumb({
  graph,
  teamId,
  onSelect,
}: {
  graph: Graph;
  teamId: string;
  onSelect: (id: string) => void;
}) {
  const ancestors = ancestorsOf(graph, teamId);
  const current = teamById(graph, teamId);
  return (
    <nav className={styles.crumbs} aria-label="Team ancestry">
      {ancestors.map((a) => (
        <Fragment key={a.id}>
          <button type="button" className="alk-link" onClick={() => onSelect(a.id)}>
            {a.name}
          </button>
          <span className={styles.crumbsSep} aria-hidden="true">
            <Icon name="chevronRight" size={13} />
          </span>
        </Fragment>
      ))}
      <span className={cx("alk-strong", "alk-truncate")} style={{ maxWidth: "40ch" }} aria-current="page">
        {current?.name}
      </span>
    </nav>
  );
}
