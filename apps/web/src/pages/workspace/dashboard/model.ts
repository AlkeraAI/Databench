import type { ReactNode } from "react";

// The dashboard's view-model — the SEAM between the page's components and where
// the data comes from. Components consume this normalized shape via the
// `useDashboardData()` hook (provider.tsx); they never touch the SDK or React
// Query directly. The shipping app injects a LIVE provider that fetches and
// adapts the real endpoints; the preview harness injects fixtures through the
// SAME context to inspect every state. This is what makes the data source
// swappable. The open overview holds the connections card and the recent
// chats; an installed extension adds its own cards and panels through
// OVERVIEW_SOURCES (app/extensions/portal.ts).

/** A small in-card visualization, chosen by the stat's data shape. `tip` is a
 *  short, human-readable decode of what the viz encodes ("4 models · 1 doc"),
 *  shown on viz hover — a read that never navigates (the card is the click target). */
export type StatViz =
  | { kind: "ring"; pct: number; tip: string }
  | { kind: "spark"; points: number[]; tip: string }
  | { kind: "bars"; values: number[]; tip: string };

/** A period-over-period trend chip: a directional arrow + a delta, colored by sentiment (so a
 *  falling backlog can show a green down-arrow). Only present on a card whose endpoint actually
 *  exposes a prior period to compute a real delta against — never fabricated. */
export interface StatTrend {
  /** Arrow direction. */
  dir: "up" | "down" | "flat";
  /** The delta as shown ("+9%", "-6%"). */
  delta: string;
  /** Sentiment → color: positive (green), negative (red), or flat (muted). */
  tone: "pos" | "neg" | "flat";
}

/** A proportion bar under a card's value — a reading against a real ceiling. Omitted when nothing
 *  bounds the measurement, so the card never draws a gauge with no scale behind it. */
export interface StatMeter {
  /** Filled fraction, 0..1. */
  value: number;
  /** `danger` once the reading is at or past its ceiling. */
  tone: "brand" | "danger";
  /** Accessible name for the gauge. */
  label: string;
}

export interface Stat {
  /** Unique on the overview; another card is placed after it by this key. */
  key: string;
  label: string;
  value: string;
  note?: string;
  /** Extra lines under the note that say where the number comes from and what it does NOT cover —
   *  the honest half of a percentage that only speaks for some of the caller's pools. */
  captions?: string[];
  /** A one-line consequence the reader has to know now (over the storage ceiling). */
  notice?: string;
  /** The page this metric belongs to — the whole card is a link here. */
  to: string;
  /** A real period-over-period trend, when the card's endpoint exposes a prior period. Omitted
   *  (not faked) when the obfuscated wire gives no comparable prior value. */
  trend?: StatTrend;
  /** A proportion bar under the value, when there is a ceiling to read against. */
  meter?: StatMeter;
  /** Omitted when the card has no number to draw — a plan with no ceiling anywhere has no share to
   *  ring, and an invented one would promise a measurement that does not exist. */
  viz?: StatViz;
}

/** One recent conversation, as the overview lists it. */
export interface ChatSummary {
  id: string;
  title: string;
  /** A coarse relative label ("2h ago") off the chat's last update. */
  updated: string;
}

/** The resolved dashboard view-model — everything the page renders. */
export interface DashboardData {
  greetingName: string;
  orgName: string;
  stats: Stat[];
  chats: ChatSummary[];
  /** Panels an installed extension adds after the chats panel, each placing itself on the grid. */
  panels: { key: string; node: ReactNode }[];
}

/** The seam's resolution state. There is no separate EMPTY state: every card on the overview reads
 *  correctly at zero (no connections, no bytes, no chats) and the chats panel's start action IS the
 *  first-run affordance, so a brand-new seat gets the working page rather than a dead end. */
export type DashboardStatus = "loading" | "error" | "ready";

export interface DashboardState {
  status: DashboardStatus;
  data: DashboardData | null;
  /** A user-safe failure message when `status === "error"`. */
  errorMessage: string | null;
  /** Re-run the underlying fetch (live provider wires this to refetch). */
  retry: () => void;
}
