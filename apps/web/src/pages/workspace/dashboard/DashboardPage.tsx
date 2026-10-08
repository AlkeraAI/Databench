import { Fragment, useState } from "react";

import { OVERVIEW_ACTIONS, OVERVIEW_SOURCES } from "../../../app/extensions/portal";
import { TopbarActions } from "../../../app/Topbar";
import { StatCard } from "./components";
import { ChatsPanel } from "./panels";
import { useDashboardData } from "./provider";
import { OverviewSkeleton } from "./Skeleton";
import { DashboardError } from "./states";
import styles from "./dashboard.module.css";

/**
 * Dashboard — the web-portal overview on @alkera/ui tokens. The AppLayout shell provides the
 * sidebar, the "Overview" title, and the masthead; this page renders a 12-column grid of stat
 * cards, the chats panel, and any panels an installed extension adds. The open overview holds the
 * connections card and the chats; OVERVIEW_SOURCES adds cards and panels, OVERVIEW_ACTIONS adds
 * topbar controls.
 *
 * Data comes through the seam (`useDashboardData()` — provider.tsx). Loading and failure are
 * first-class — a skeleton, and an error state with a retry. A brand-new seat gets the grid too:
 * every card reads correctly at zero and the chats panel's start action is the first step.
 */
export function DashboardPage() {
  const { status, data, errorMessage, retry } = useDashboardData();
  const [actions] = useState(() => OVERVIEW_ACTIONS.items());
  const [placeholders] = useState(() =>
    OVERVIEW_SOURCES.items().reduce(
      (sum, source) => ({ stats: sum.stats + source.placeholders.stats, panels: sum.panels + source.placeholders.panels }),
      { stats: 1, panels: 0 },
    ),
  );

  return (
    <div className={styles.root} data-measure-surface="product">
      {actions.length > 0 ? (
        <TopbarActions>
          {actions.map(({ key, Action }) => (
            <Action key={key} />
          ))}
        </TopbarActions>
      ) : null}

      {status === "loading" ? <OverviewSkeleton stats={placeholders.stats} panels={placeholders.panels} /> : null}
      {status === "error" ? <DashboardError message={errorMessage} onRetry={retry} /> : null}
      {status === "ready" && data ? (
        // Content fades in as the skeleton resolves (compositor-only; shared alk-fade-in keyframe
        // from @alkera/ui theme/motion.css). Inline because the skeleton shares styles.grid and
        // must not fade; the page root's reduced-motion rule still wins over this inline animation.
        <div
          className={styles.grid}
          style={{ animation: "alk-fade-in var(--alkDurationSlow) var(--alkEaseStandard) both" }}
          data-measure-grid
          data-cols="12"
        >
          {data.stats.map((s) => (
            <StatCard key={s.key} s={s} />
          ))}
          <ChatsPanel chats={data.chats} />
          {data.panels.map((p) => (
            <Fragment key={p.key}>{p.node}</Fragment>
          ))}
        </div>
      ) : null}
    </div>
  );
}
