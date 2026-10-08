// The transcript's block wrapper and turn separator. Every transcript block,
// the host's own and any slotted surface's, wraps in TranscriptBlock; the
// separator between turns is the perforated tear-line.

import type { ReactNode } from "react";

import "./transcript.css";

/** The readability-dot mechanism, defined once. A user turn carries no left
 *  dot. Every other kind gets a quiet gutter dot placed by static CSS on the
 *  center of its first row's line box. A surface participates by composing
 *  with this wrapper, never by hand-styling a dot of its own. The gutter
 *  geometry, the per-kind first-row defaults, the override tokens
 *  (`--chat-first-row-lh`, `--chat-dot-lead`), and the `[data-gutter-mark]`
 *  suppression contract are documented on the ::before rule in transcript.css.
 *  `live` opts the block into the arrival rise animation. */
/** The block kinds transcript.css keys first-row metrics off. A new kind lands
 *  here and in that file together, so a typo can't silently lose the dot.
 *  "plan" and "question" carry no default there: each sheet feeds the metric
 *  off its own root (`:has(.chat-plan-doc)`, `:has(.chat-asked-card)`), as any slotted
 *  surface may. */
export type TranscriptBlockKind =
  | "user"
  | "thinking"
  | "prose"
  | "slash"
  | "compaction"
  | "activity"
  | "notice"
  | "plan"
  | "question";

export function TranscriptBlock({
  kind,
  live,
  dimmed,
  dimNote,
  entryId,
  children,
}: {
  kind: TranscriptBlockKind;
  live?: boolean;
  /** History the agent no longer holds — a /clear, or the turns a compaction
   *  summarised. It stays readable and scrollable; only its weight drops, so
   *  the reader can tell at a glance what the agent can still see. */
  dimmed?: boolean;
  /** Why this block left the agent's context, said once at the top of the run
   *  rather than on every block in it. */
  dimNote?: string;
  /** The entry this block renders, so the panel can find it again by id
   *  when a page lands above it and hold the viewport on it. */
  entryId?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={`chat-block${live ? " chat-block--live" : ""}`}
      data-block={kind}
      data-dim={dimmed ? "" : undefined}
      data-entry-id={entryId}
    >
      {dimNote ? <p className="chat-block-dimnote">{dimNote}</p> : null}
      {children}
    </div>
  );
}

/** Perforated tear-line, the turn separator. */
export function Tear() {
  return <div className="chat-tear" aria-hidden="true" />;
}
