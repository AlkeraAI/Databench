import { Card, Skeleton, Stack, Toolbar } from "@alkera/ui";
import shell from "../teams.module.css";
import styles from "./Skeleton.module.css";

// Loading state — a skeleton mirroring the full-width layout (masthead + the register spread: a
// roster folio over a context column of plates) so content resolves into place instead of
// reflowing. aria-hidden via the wrapper's aria-busy.

function Bar({ w, h = 12 }: { w: string | number; h?: number }) {
  return <Skeleton width={w} height={h} style={{ borderRadius: "var(--alkRadiusSm)" }} />;
}

function PlateSkeleton({ rows }: { rows: number }) {
  return (
    <Card>
      <Stack gap={5} align="stretch">
        <Bar w={120} h={13} />
        <Bar w="90%" h={14} />
        {Array.from({ length: rows }).map((_, i) => (
          <Bar key={i} w="100%" h={32} />
        ))}
      </Stack>
    </Card>
  );
}

/** The spread placeholder — roster folio + context plates, no masthead. Shown
 *  inside TeamDetail while the selected team's roster loads (the breadcrumb +
 *  masthead are already painted from the team list). */
export function SpreadSkeleton() {
  return (
    <Stack gap={7} align="stretch" aria-busy="true" aria-label="Loading roster">
      <Card style={{ overflow: "hidden" }} bodyClassName={shell.folioBody}>
        <Toolbar style={{ padding: "var(--alkSpace7)" }} end={<Bar w={180} h={34} />}>
          <Bar w={220} h={34} />
        </Toolbar>
        {/* gap 0 — the ruled rows must sit border-to-row, so the hairlines read as one ledger. */}
        <Stack gap="0px" align="stretch" style={{ padding: "0 var(--alkSpace3) var(--alkSpace3)" }}>
          {[0, 1, 2, 3, 4].map((i) => (
            <div className={styles.trow} key={i}>
              <Skeleton width={32} height={32} circle />
              <Bar w="26%" />
              <Bar w={92} h={24} />
              <Bar w="18%" />
              <Bar w={64} />
            </div>
          ))}
        </Stack>
      </Card>
      <Stack gap={7} align="stretch">
        <PlateSkeleton rows={1} />
        <PlateSkeleton rows={2} />
        <PlateSkeleton rows={1} />
      </Stack>
    </Stack>
  );
}

/** The whole-detail placeholder — breadcrumb + masthead + spread — for the page's
 *  initial load, before any team is known. */
export function DetailSkeleton() {
  return (
    <section className={shell.detail} aria-busy="true" aria-label="Loading team">
      <div className={shell.detailTop}>
        <Bar w={220} h={14} />
      </div>
      <div className={shell.mast}>
        <span style={{ flex: "1 1 auto" }}>
          <Bar w={220} h={28} />
        </span>
        <Bar w={132} h={36} />
      </div>
      <SpreadSkeleton />
    </section>
  );
}
