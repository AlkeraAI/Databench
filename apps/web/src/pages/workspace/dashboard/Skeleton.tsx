import { Card, Inline, Skeleton, Stack } from "@alkera/ui";

import shell from "./dashboard.module.css";

// Loading state — a skeleton that mirrors the real grid so content resolves into place instead of
// reflowing. Built from the ui Skeleton primitive + the dashboard grid spans; the card
// min-heights (in dashboard.module.css, gated on aria-busy) lift each block to its resolved height so the
// swap doesn't jump. aria-busy on the grid so a screen reader hears "loading".
//
// StatSkel reuses StatCard's internal classes (.alk-statcard*) and CardSkel renders a real ui
// Card with skeleton bars in its header slots, so each placeholder inherits the graduated component's
// exact padding / min-height / flex and the swap stays jump-free. That couples this skeleton to
// StatCard/Card's markup on purpose — if those restructure, re-mirror here.

function Bar({ w, h = 12 }: { w: string; h?: number }) {
  return <Skeleton width={w} height={h} aria-hidden="true" />;
}

function StatSkel() {
  return (
    <article className={`alk-statcard ${shell.statPlace}`} data-measure-col>
      <div className="alk-statcard__head">
        <Bar w="56%" />
        <Bar w="34px" />
      </div>
      <div className="alk-statcard__main">
        <Bar w="46%" h={30} />
        <Bar w="44px" h={44} />
      </div>
      <Bar w="78%" />
    </article>
  );
}

function CardSkel({
  className,
  footer,
  children,
}: {
  className: string;
  footer?: React.ReactNode;
  children: React.ReactNode;
}) {
  // A real ui Card carrying skeleton bars in its title / sub / actions slots (and the footer slot
  // when the loaded panel pins one), so the placeholder inherits the resolved surface (border, radius,
  // padding) + header layout the loaded panel uses — the swap to the real content stays jump-free.
  return (
    <Card
      className={className}
      title={<Bar w="160px" h={16} />}
      sub={<Bar w="220px" />}
      actions={<Bar w="180px" h={28} />}
      footer={footer}
      data-measure-col
    >
      {children}
    </Card>
  );
}

/** `stats` cards and `panels` side panels after the chats, as many as the resolved page will hold. */
export function OverviewSkeleton({ stats, panels }: { stats: number; panels: number }) {
  return (
    <div
      className={shell.grid}
      data-measure-grid
      data-cols="12"
      aria-busy="true"
      aria-label="Loading dashboard"
    >
      {Array.from({ length: stats }, (_, i) => (
        <StatSkel key={i} />
      ))}
      <CardSkel className={shell.chats}>
        <Stack align="stretch" gap={6}>
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <Inline key={i} gap={5} wrap={false} justify="space-between">
              <Bar w="52%" />
              <Bar w="56px" />
            </Inline>
          ))}
        </Stack>
      </CardSkel>
      {Array.from({ length: panels }, (_, p) => (
        <CardSkel key={p} className={shell.models} footer={<Bar w="92px" />}>
          <Stack align="stretch" gap={7}>
            {[0, 1, 2, 3, 4].map((i) => (
              <Inline key={i} gap={5} wrap={false} justify="space-between">
                <Bar w="40%" />
                <Bar w="46%" />
              </Inline>
            ))}
          </Stack>
        </CardSkel>
      ))}
    </div>
  );
}
