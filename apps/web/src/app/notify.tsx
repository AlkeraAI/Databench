import { useCallback, useMemo, type ReactNode } from "react";

import { ToastViewport, useToasts } from "@alkera/ui";

import { refusalSentence } from "../api/errors";

/**
 * How a page reports the outcome of a write through its toast viewport.
 *
 * Two verbs, not one callback with an optional tone: a single `flash(message)` that defaulted to the
 * success tone drew every refusal on the profile page — a 403 from two-factor setup, a rejected
 * save — under a green check. A caller now has to say which one happened.
 */
export interface Notify {
  /** A write that landed. */
  success: (message: string) => void;
  /** A write that did not. Drawn in the danger tone and announced assertively. */
  error: (message: string) => void;
}

/** The server's own sentence for a refusal it explained, or the caller's written fallback. A 5xx's
 *  body and a network failure's message ("Failed to fetch") are written for a developer, so they,
 *  and every value that is not an ApiError, resolve to the fallback. */
export function errorSentence(e: unknown, fallback: string): string {
  return refusalSentence(e, { fallback });
}

/** A page's toast state and the two verbs that feed it. Render `viewport` once in the page. */
export function useNotify(): { notify: Notify; viewport: ReactNode } {
  const { toasts, push, dismiss } = useToasts();
  const success = useCallback((message: string) => void push({ message, tone: "success" }), [push]);
  const error = useCallback((message: string) => void push({ message, tone: "danger" }), [push]);
  const notify = useMemo<Notify>(() => ({ success, error }), [success, error]);
  const viewport = <ToastViewport toasts={toasts} onDismiss={dismiss} position="bottom-center" />;
  return { notify, viewport };
}
