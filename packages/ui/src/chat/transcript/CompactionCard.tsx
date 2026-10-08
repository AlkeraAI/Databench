// Compaction, as its own transcript block: the conversation being folded into
// a summary, and what the fold cost. Deliberately NOT a slash-command row —
// nobody typed this, and in a cloud chat nobody can.
//
// Running: one sentence plus the seconds it has been going, so ten (or eighty)
// seconds of quiet reads as work rather than as a stall. Settled: the turn
// count and the before/after context figures, with the stored summary behind a
// disclosure — the summary IS the agent's memory of everything above it, so it
// is readable in place instead of only from a detail page.

import { useEffect, useState } from "react";

import { Markdown } from "../../primitives/render";
import { WorkingDots } from "../indicators";
import s from "./compaction.module.css";

export interface CompactionCardProps {
  /** The fold is still running: the harness said `compacting` and no summary
   *  has landed yet. */
  running?: boolean;
  /** When the fold began (ISO). Drives the live elapsed read while running. */
  startedAt?: string;
  /** Turns the summary replaced. */
  summarisedTurns?: number;
  /** Conversation tokens before / after the fold. */
  tokensBefore?: number;
  tokensAfter?: number;
  /** The stored summary, shown when the reader opens the disclosure. */
  summary?: string;
  /** Open the summary on its own page, where the shell has one. */
  onOpen?: () => void;
}

/** `1234` → `1.2k`, `71854` → `72k`. Small counts stay exact. */
export function compactTokens(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "0";
  if (value < 1000) return String(Math.round(value));
  const thousands = value / 1000;
  if (thousands < 10) return `${(Math.round(thousands * 10) / 10).toFixed(1)}k`;
  return `${Math.round(thousands)}k`;
}

/** Seconds since `startedAt`, as the card reads them out: `9s`, `1m 25s`. */
export function elapsedLabel(startedAt: string | undefined, now: number): string | null {
  if (!startedAt) return null;
  const from = Date.parse(startedAt);
  if (Number.isNaN(from)) return null;
  const seconds = Math.max(0, Math.floor((now - from) / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

/** The settled line's facts, in the order they answer "what just happened":
 *  what it did, how much it folded, what it bought. Each part is omitted when
 *  the wire did not carry it, rather than shown as a zero. */
export function compactionFacts(props: CompactionCardProps): string[] {
  const facts: string[] = [];
  const turns = props.summarisedTurns ?? 0;
  if (turns > 0) facts.push(`${turns} turn${turns === 1 ? "" : "s"} summarised`);
  if (typeof props.tokensBefore === "number" && typeof props.tokensAfter === "number") {
    facts.push(`${compactTokens(props.tokensBefore)} → ${compactTokens(props.tokensAfter)} tokens`);
  } else if (typeof props.tokensBefore === "number") {
    facts.push(`${compactTokens(props.tokensBefore)} tokens folded`);
  }
  return facts;
}

/** Ticks once a second while a fold is running, so the elapsed read moves. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

export function CompactionCard(props: CompactionCardProps) {
  const { running, startedAt, summary, onOpen } = props;
  const [open, setOpen] = useState(false);
  const now = useNow(Boolean(running));
  const elapsed = running ? elapsedLabel(startedAt, now) : null;
  const facts = running ? [] : compactionFacts(props);
  const hasSummary = Boolean(summary && summary.trim());

  if (running) {
    return (
      <div className={s.card} data-running="" role="status">
        <p className={s.head}>
          <span className="chat-shimmer">Compacting the conversation…</span>
          {elapsed ? <span className={`${s.elapsed} chat-num`}>{elapsed}</span> : <WorkingDots />}
        </p>
      </div>
    );
  }

  return (
    <div className={s.card}>
      <p className={s.head}>
        <span className={s.title}>Conversation compacted</span>
        {facts.map((fact) => (
          <span key={fact} className={`${s.fact} chat-num`}>
            {fact}
          </span>
        ))}
      </p>
      {hasSummary || onOpen ? (
        <p className={s.actions}>
          {hasSummary ? (
            <button
              type="button"
              className={s.disclose}
              aria-expanded={open}
              onClick={() => setOpen((was) => !was)}
            >
              {open ? "Hide summary" : "Show summary"}
            </button>
          ) : null}
          {onOpen ? (
            <button type="button" className={s.disclose} onClick={onOpen}>
              Open summary
            </button>
          ) : null}
        </p>
      ) : null}
      {open && hasSummary ? (
        <div className={s.summary}>
          <Markdown content={summary ?? ""} />
        </div>
      ) : null}
    </div>
  );
}
