import type { ReactElement } from "react";

import { cx } from "../../cx";
import { Button } from "../../controls/Button";
import { Pill, type PillTone } from "../Pill";

/** What the reader may do about a status. The host knows how to perform each
 *  kind; the words on the button are the server's. */
export interface StatusActionData {
  kind: string;
  label: string;
}

/** Where one thing stands, as the server wrote it. Every word a person reads
 *  (`label`, `sentence`, the action's `label`) arrives here; nothing in the UI
 *  maps a state to copy or to a colour. `state` and `reason_code` are for
 *  styling hooks and tests only. */
export interface StatusFactData {
  subject: string;
  state: string;
  label: string;
  tone: string;
  reason_code?: string;
  sentence: string;
  since?: string | null;
  action?: StatusActionData | null;
}

/** The one tone table in the product. A tone this build does not know draws
 *  as neutral. */
const PILL_TONE: Readonly<Record<string, PillTone>> = {
  neutral: "neutral",
  info: "info",
  success: "success",
  warning: "warning",
  danger: "danger",
  muted: "catNeutral",
};

function toneOf(status: StatusFactData): string {
  return status.tone in PILL_TONE ? status.tone : "neutral";
}

/** Whether the status is one a healthy or resting thing has: nothing is
 *  owed and nothing is wrong. Decided on tone, never on which state it is. */
export function statusIsResting(status: StatusFactData | null | undefined): boolean {
  if (!status) return true;
  const tone = toneOf(status);
  return tone === "muted" || tone === "success";
}

/** Whether the status needs its sentence said where there is room for one: it
 *  carries a reason, or something is wrong. A plain "Working" or "Asleep" says
 *  everything in its word. */
export function statusNeedsSaying(status: StatusFactData | null | undefined): boolean {
  if (!status) return false;
  const tone = toneOf(status);
  return Boolean(status.reason_code) || tone === "warning" || tone === "danger";
}

export interface StatusPillProps {
  /** The fact to draw; nothing is drawn for a thing with no status. */
  status: StatusFactData | null | undefined;
  /** `pill` is the labelled chip. `dot` is a list row's mark, with the label
   *  as its accessible name. `line` is the sentence with its action, for a
   *  banner or a header that has the room. */
  variant?: "pill" | "dot" | "line";
  /** A `dot` in a long list stays out of the way: a resting or healthy status
   *  (muted or success) draws nothing, so the marks that remain are the ones
   *  worth reading. */
  quiet?: boolean;
  /** The action kinds the host can perform. An action of any other kind draws
   *  no button. */
  actions?: readonly string[];
  onAction?: (kind: string) => void;
  className?: string;
}

/** A status the server decided, drawn. */
export function StatusPill({
  status,
  variant = "pill",
  quiet = false,
  actions,
  onAction,
  className,
}: StatusPillProps): ReactElement | null {
  if (!status) return null;
  const tone = toneOf(status);
  const hooks = { "data-subject": status.subject, "data-state": status.state };
  if (variant === "dot") {
    if (quiet && statusIsResting(status)) return null;
    return (
      <span
        className={cx("alk-status-dot", className)}
        role="img"
        aria-label={status.label}
        title={status.sentence}
        data-tone={tone}
        {...hooks}
      >
        <span className="alk-status-dot__mark" aria-hidden="true" />
      </span>
    );
  }
  if (variant === "line") {
    const action = status.action;
    const offered = action && onAction && (actions ?? []).includes(action.kind) ? action : null;
    return (
      <div className={cx("alk-status-line", className)} role="status" data-tone={tone} {...hooks}>
        <strong className="alk-status-line__label">{status.label}</strong>
        <span className="alk-status-line__sentence">{status.sentence}</span>
        {offered ? (
          <Button size="sm" variant="secondary" onClick={() => onAction?.(offered.kind)}>
            {offered.label}
          </Button>
        ) : null}
      </div>
    );
  }
  return (
    <Pill
      tone={PILL_TONE[tone]}
      dot
      shape="rect"
      className={cx("alk-status-pill", className)}
      title={status.sentence}
      {...hooks}
    >
      {status.label}
    </Pill>
  );
}
