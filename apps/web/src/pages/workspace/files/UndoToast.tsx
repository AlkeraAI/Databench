/**
 * The undo prompt shown after a destructive write.
 *
 * It is a prompt, not the mechanism: the history lives in `undo.ts` and Cmd+Z is bound
 * to the page, so dismissing this — or letting it time out — never costs the person the
 * ability to undo. That is why the button and the keystroke call the same
 * controller rather than the toast owning any state of its own.
 *
 * `role="status"` and not `alert`: an undo offer is information, and an assertive live
 * region would interrupt a screen reader mid-sentence for every file moved.
 */

import { useEffect, useRef, useState } from "react";

import { filesErrorCopy } from "@/lib/files/errors";
import { entryKey, type UndoController } from "./undo";
import { FILES_UNDO_TOAST_MS, useLimits } from "@/lib/limits";

/** How long the prompt stays up. Long enough to read a sentence and reach the button,
 *  and irrelevant to whether the action can still be undone. A host narrows it through
 *  `LimitsProvider`; this is what it is without one. */
export const TOAST_MS = FILES_UNDO_TOAST_MS;

export interface UndoToastProps {
  controller: UndoController;
  /** Test seam and reduced-motion escape: 0 keeps the toast up until dismissed.
   *  Unset, the host's limit decides. */
  timeoutMs?: number;
}

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function UndoToast({ controller, timeoutMs: timeoutProp }: UndoToastProps) {
  const limits = useLimits();
  // `??`, not `||`: a passed 0 means "stay up", and must not fall through to the limit.
  const timeoutMs = timeoutProp ?? limits.filesUndoToastMs;
  const { pending, undo, running, canUndo } = controller;
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const reduced = useRef(prefersReducedMotion());

  // A step that named no operation still has an identity — see `entryKey`.
  const stepKey = entryKey(pending);

  // A new step re-opens the prompt, even if the previous one was dismissed.
  useEffect(() => {
    setDismissed(null);
    setFailure(null);
  }, [stepKey]);

  const showing = pending !== undefined && dismissed !== stepKey;

  useEffect(() => {
    if (!showing || timeoutMs <= 0) return;
    const timer = window.setTimeout(() => setDismissed(stepKey), timeoutMs);
    return () => window.clearTimeout(timer);
  }, [showing, timeoutMs, stepKey]);

  if (!showing || !pending) return null;

  return (
    <div
      className="alk-files__toast"
      role="status"
      aria-live="polite"
      data-reduced-motion={reduced.current ? "true" : undefined}
    >
      <span className="alk-files__toast-label">{pending.label}</span>
      {failure ? <span className="alk-files__toast-error">{failure}</span> : null}
      <button
        type="button"
        className="alk-button alk-files__toast-undo"
        disabled={running || !canUndo}
        onClick={() => {
          setFailure(null);
          void undo().catch((error: unknown) => {
            // The refusal is the interesting part — "held", "leased" — so it replaces
            // the toast's own copy instead of vanishing with the prompt.
            setFailure(filesErrorCopy(error).title);
          });
        }}
      >
        {running ? "Undoing…" : "Undo"}
      </button>
      <button
        type="button"
        className="alk-button alk-files__toast-dismiss"
        onClick={() => setDismissed(stepKey)}
        aria-label="Dismiss"
      >
        Dismiss
      </button>
    </div>
  );
}
