import { Link } from "react-router-dom";

import { Meter, MiniBars, Pill, type PillTone, Ring, Sparkline, StatCard as UIStatCard, Tooltip } from "@alkera/ui";

import { Icon } from "../../../app/icons";
import shell from "./dashboard.module.css";
import type { Stat, StatTrend, StatViz as StatVizData } from "./model";
import styles from "./components.module.css";

// Picks the in-card viz from the stat's data and renders the matching ui viz component.
function VizGlyph({ viz }: { viz: StatVizData }) {
  if (viz.kind === "ring") return <Ring pct={viz.pct} />;
  if (viz.kind === "spark") return <Sparkline points={viz.points} />;
  return <MiniBars values={viz.values} />;
}

// Trend sentiment → the shared status ink: a positive trend reads success (green), a negative one
// danger (red), a flat one muted — colour keyed to sentiment, NOT direction (a falling backlog is a
// green down-arrow). The Pill's `plain` variant is a chip-less inline mark (no fill/border), so the
// trend rides the card head as toned word + glyph without growing the line.
const TREND_TONE: Record<StatTrend["tone"], PillTone> = {
  pos: "success",
  neg: "danger",
  flat: "neutral",
};

// Maps a dashboard Stat onto the ui StatCard, and makes the whole card a link to the page its
// metric belongs to. The head carries a real period-over-period trend chip when the card's endpoint
// exposes a prior period to compute one against — never a fabricated delta. The viz is a hover-read:
// the shared Tooltip decodes what it draws, and the viz swallows its own click so reading the
// breakdown never fires the card's nav.
export function StatCard({ s }: { s: Stat }) {
  // Hoisted so the Tooltip's render prop still sees the narrowed viz (a closure can't carry the
  // narrowing of a property read).
  const viz = s.viz;
  return (
    <Link
      to={s.to}
      className={`${shell.statPlace} ${styles.statLink} alk-rowlink`}
      data-measure-col
      aria-label={`${s.label}: ${s.value}`}
    >
      <UIStatCard
        label={s.label}
        value={s.value}
        note={
          // The note, then the lines that keep it honest: where a percentage comes from, what it
          // leaves out, and any consequence the reader has to know now.
          <>
            {s.note ? <span>{s.note}</span> : null}
            {s.meter ? (
              <Meter className={styles.meter} value={s.meter.value} tone={s.meter.tone} label={s.meter.label} />
            ) : null}
            {(s.captions ?? []).map((c) => (
              <span key={c} className={styles.caption}>
                {c}
              </span>
            ))}
            {s.notice ? <span className={`${styles.notice} alk-danger`}>{s.notice}</span> : null}
          </>
        }
        trend={
          s.trend ? (
            <Pill tone={TREND_TONE[s.trend.tone]} variant="plain" className="alk-tnum" icon={<Icon name={s.trend.dir} size={14} />}>
              {s.trend.delta}
            </Pill>
          ) : undefined
        }
        viz={
          // A whole-viz decode tooltip (one reading of what the glyph draws), on the shared Tooltip —
          // it portals above the card, opens on hover OR keyboard focus, and animates in/out. The
          // click is swallowed so reading the decode never fires the card's nav. A card with no
          // measurement behind it (no ceiling to divide by) carries no viz at all.
          viz ? (
          <Tooltip label={viz.tip}>
            {(t) => (
              <span
                {...t}
                // A focus stop of its own so a keyboard user can tab to the viz and read its decode:
                // the Tooltip opens on focus and wires the decode as this element's description
                // (aria-describedby), while the inner viz SVG supplies the graphic's name. The card's
                // own click/Enter stays the nav.
                tabIndex={0}
                className={styles.viz}
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                }}
                onKeyDown={(e) => {
                  // The viz is a read, not an action — swallow Enter/Space so it never triggers the
                  // card link's navigation from within.
                  if (e.key === "Enter" || e.key === " ") e.stopPropagation();
                }}
              >
                <VizGlyph viz={viz} />
              </span>
            )}
          </Tooltip>
          ) : undefined
        }
      />
    </Link>
  );
}
