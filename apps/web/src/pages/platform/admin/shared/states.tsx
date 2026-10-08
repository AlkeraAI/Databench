// First-class EMPTY / ERROR / LOADING surfaces for the admin registers, built on
// the shared @alkera/ui EmptyState + Skeleton so every register resolves the
// same way the rest of the portal does. The bench marks + copy stay register-
// specific; the plate is shared.

import type { ReactNode } from "react";

import { Button, Card, EmptyState, Inline, Skeleton, Stack } from "@alkera/ui";

import styles from "./states.module.css";
import { refusalSentence } from "../../../../api/errors";
import { Icon } from "../../../../app/icons";
import { RegisterMark, SpilledMark } from "./glyphs";

/** A register with no entries yet. States the one next move (a Button), or nothing
 *  for a read-only register that simply has no rows. */
export function RegisterEmpty({ title, body, action, mark }: { title: string; body: string; action?: ReactNode; mark?: ReactNode }) {
  return <EmptyState icon={mark ?? <RegisterMark size={48} />} title={title} body={body} action={action} />;
}

/** A failed fetch: a plain headline, the server's own explanation (when it wrote one) under a
 *  disclosure, and a retry. A transport failure or a 5xx carries nothing a reader can use, so it
 *  shows the headline alone. */
export function RegisterError({ what, error, onRetry }: { what: string; error: unknown; onRetry: () => void }) {
  const explained = refusalSentence(error, { fallback: "" });
  return (
    <EmptyState
      tone="alert"
      icon={<SpilledMark size={48} />}
      title={`We couldn’t load ${what}`}
      body={explained ? "Try again. If it keeps happening, the detail below can help support." : "Try again."}
      details={explained || undefined}
      action={
        <Button variant="secondary" leftSection={<Icon name="refresh" size={16} />} onClick={onRetry}>
          Try again
        </Button>
      }
    />
  );
}

/**
 * A register's loading skeleton — a header band over shimmer rows that match the
 * final table's column rhythm, so the layout doesn't jump when data lands. `cols`
 * sets the column count; `rows` the row count.
 */
export function TableSkeleton({ rows = 6, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <Card role="status" aria-busy="true" aria-label="Loading">
      <Stack gap={5} align="stretch">
        <Inline gap={7} className={styles.tableSkelHead}>
          {Array.from({ length: cols }, (_, i) => (
            <Skeleton key={`h-${i}`} height={12} style={{ flex: "1 1 0", borderRadius: "var(--alkRadiusSm)" }} />
          ))}
        </Inline>
        {Array.from({ length: rows }, (_, r) => (
          <Inline gap={7} key={`r-${r}`}>
            {Array.from({ length: cols }, (_, c) => (
              <Skeleton key={`c-${r}-${c}`} height={14} style={{ flex: "1 1 0", borderRadius: "var(--alkRadiusSm)" }} />
            ))}
          </Inline>
        ))}
      </Stack>
    </Card>
  );
}

/** A stat-strip skeleton — four tiles matching the overview's KPI row. */
export function StatStripSkeleton({ tiles = 4 }: { tiles?: number }) {
  return (
    <div className={styles.statSkel} role="status" aria-busy="true" aria-label="Loading">
      {Array.from({ length: tiles }, (_, i) => (
        <Card key={i} bodyClassName={styles.statSkelTile}>
          <Stack gap={5} align="stretch">
            <Skeleton height={12} style={{ width: "45%", borderRadius: "var(--alkRadiusSm)" }} />
            <Skeleton height={28} style={{ width: "65%", borderRadius: "var(--alkRadiusSm)" }} />
            <Skeleton height={11} style={{ width: "55%", borderRadius: "var(--alkRadiusSm)" }} />
          </Stack>
        </Card>
      ))}
    </div>
  );
}
