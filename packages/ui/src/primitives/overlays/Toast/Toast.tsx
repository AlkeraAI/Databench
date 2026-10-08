import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ComponentType,
  type ReactNode,
  type CSSProperties,
} from "react";
import { createPortal } from "react-dom";

import { cx } from "../../cx";
import { Button } from "../../controls/Button";
import { AlertCircleIcon, AlertTriangleIcon, CheckCircleIcon, CloseIcon, InfoIcon, type IconProps } from "../../icons";

/** A toast's tone — drives the leading glyph + the live-region politeness. Toast owns its own
 *  icon + text (it does NOT compose Callout): a toast is a floating card, not an inline notice, so
 *  it shouldn't inherit — and then have to neutralise — a message box's styling. `brand` is the
 *  confirmed/verified reading (Callout's brand vocabulary), the rest mirror the status tones. */
export type ToastTone = "brand" | "info" | "success" | "warning" | "danger";

const TONE_ICON: Record<ToastTone, ComponentType<IconProps>> = {
  brand: CheckCircleIcon,
  info: InfoIcon,
  success: CheckCircleIcon,
  warning: AlertCircleIcon,
  danger: AlertTriangleIcon,
};

/** The auto-dismiss used when none is given, and the fall-back for the one forbidden combination
 *  (an undismissable toast: no valid timer AND no close button). */
export const DEFAULT_TOAST_DURATION = 5000;

/** Where a corner-stacked viewport pins. */
export type ToastPosition =
  | "top-left"
  | "top-center"
  | "top-right"
  | "bottom-left"
  | "bottom-center"
  | "bottom-right";

export interface ToastOptions {
  /** Stable id — supply to update/de-dupe a toast; otherwise the queue assigns one. */
  id?: string;
  /** Tone — drives the leading icon + a11y politeness (danger asserts). Default `info`. */
  tone?: ToastTone;
  title?: ReactNode;
  /** The message body. */
  message?: ReactNode;
  /** Override the tone's default leading glyph. */
  icon?: ReactNode;
  /** Auto-dismiss after this many ms. Omit for the default; pass `null` / `0` for no timer (the
   *  toast then stays until its close button is used). */
  duration?: number | null;
  /** Show the close button (default true). */
  closable?: boolean;
  /** Force the live-region politeness; defaults from the tone (danger → assertive). */
  role?: "status" | "alert";
}

interface ToastEntry extends ToastOptions {
  id: string;
  /** Per-push remount key, set by {@link useToasts}: fresh even on a re-push of the same `id`, so the
   *  toast remounts (replays its entrance + restarts its timer) rather than inheriting the prior
   *  push's lifecycle. Optional so a toast can be constructed directly (it then keys by `id`). */
  instanceKey?: number;
}

/**
 * Resolve the effective auto-dismiss for a toast, encoding the one hard rule: a toast must always be
 * dismissable. A valid positive duration is honoured. Otherwise (omitted → the default; or an
 * explicit `null` / non-positive → "no timer"), the timer is dropped ONLY when a close button is
 * present; if it isn't, the toast would be undismissable, so we fall back to the default duration.
 *
 * @returns ms to auto-dismiss after, or `null` for "no timer — dismiss via the close button".
 */
export function resolveToastDuration(duration: number | null | undefined, closable: boolean): number | null {
  const d = duration === undefined ? DEFAULT_TOAST_DURATION : duration;
  if (typeof d === "number" && Number.isFinite(d) && d > 0) return d;
  // No valid timer: keep it open only if the close button can dismiss it; else fall back so it
  // can't get stuck on screen forever.
  return closable ? null : DEFAULT_TOAST_DURATION;
}

export interface UseToastsResult {
  toasts: ToastEntry[];
  /** Enqueue a toast; returns its id (so a caller can dismiss it early). */
  push: (toast: ToastOptions) => string;
  dismiss: (id: string) => void;
  clear: () => void;
}

/** Owns the toast queue. The host renders one {@link ToastViewport} per position with `toasts` +
 *  `onDismiss` from here. Ids are a monotonic counter (no clock / randomness, so tests are stable). */
export function useToasts(): UseToastsResult {
  const [toasts, setToasts] = useState<ToastEntry[]>([]);
  const seq = useRef(0);
  const keySeq = useRef(0);

  const push = useCallback((toast: ToastOptions) => {
    const id = toast.id ?? `toast-${seq.current++}`;
    // A fresh instanceKey on every push — including a re-push of the same id — so React remounts the
    // toast (replaying its entrance and RESTARTING its auto-dismiss timer, for the Saving… → Saved
    // pattern) instead of letting it inherit the prior push's remaining lifetime. The supplied id
    // still de-dupes (remove-then-append keeps one toast, moved to the newest slot).
    const instanceKey = keySeq.current++;
    setToasts((prev) => [...prev.filter((t) => t.id !== id), { ...toast, id, instanceKey }]);
    return id;
  }, []);

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const clear = useCallback(() => setToasts([]), []);

  return { toasts, push, dismiss, clear };
}

/** Exit fallback — the toast normally unmounts on its slot's `transitionend` (the row collapse in
 *  toast.css: fade, then a delayed grid-row close). A fixed timer racing that transition snaps the
 *  last pixels of the neighbors' slide, so the timer is only a safety net at ~2× the choreography,
 *  for a transition that never ends (hidden tab, cancelled transition). */
export const TOAST_EXIT_FALLBACK_MS = 600;

interface ToastItemProps {
  toast: ToastEntry;
  onDismiss: (id: string) => void;
}

/**
 * One toast. Owns its own enter → open → closed lifecycle (rather than usePresence, which doesn't
 * animate a node that is born open), its auto-dismiss timer, and the close button. The card sits in
 * a collapsible SLOT (a 0fr↔1fr grid row, the Collapse technique) that opens before the card fades
 * in and closes after it fades out — so the rest of the stack slides to its new place instead of
 * snapping when a toast enters or leaves. The auto-dismiss pauses while the pointer is over the
 * toast and resumes from the time that remained — so a toast the reader is hovering can't disappear
 * mid-read.
 */
function ToastItem({ toast, onDismiss }: ToastItemProps) {
  const [state, setState] = useState<"enter" | "open" | "closed">("enter");
  const [paused, setPaused] = useState(false);
  const closable = toast.closable ?? true;
  const duration = resolveToastDuration(toast.duration, closable);
  const Glyph = TONE_ICON[toast.tone ?? "info"];

  const onDismissRef = useRef(onDismiss);
  onDismissRef.current = onDismiss;
  const slotRef = useRef<HTMLDivElement | null>(null);

  // Auto-dismiss time still owed, in ms (null = sticky, no timer). It counts DOWN across hover
  // pauses: each pause banks the elapsed time so the resume picks up where it left off rather than
  // restarting the full duration. Re-initialised per mount, so a re-push (which remounts via
  // instanceKey) gets a fresh clock.
  const remainingRef = useRef<number | null>(duration);

  // Entrance: paint the hidden start-state, then flip to open next frame so the CSS transitions in.
  // Only from `enter` — a dismissal racing this frame must not be resurrected to open.
  useEffect(() => {
    const frame = requestAnimationFrame(() => setState((s) => (s === "enter" ? "open" : s)));
    return () => cancelAnimationFrame(frame);
  }, []);

  // Auto-dismiss countdown. While hovered (`paused`) the timer is torn down and the cleanup banks the
  // elapsed time against `remainingRef`; on un-hover the effect re-runs and resumes from what's left.
  // Sticky toasts (remaining == null) never start a timer, so hover is a no-op for them.
  useEffect(() => {
    if (paused) return;
    const remaining = remainingRef.current;
    if (remaining == null) return;
    const startedAt = Date.now();
    const timer = window.setTimeout(() => setState("closed"), remaining);
    return () => {
      window.clearTimeout(timer);
      remainingRef.current = Math.max(0, remaining - (Date.now() - startedAt));
    };
  }, [paused]);

  // Once closing, drop the toast from the queue when its slot's row collapse actually finishes —
  // unmounting on a fixed timer races the CSS and snaps the last pixels of the neighbors' slide.
  useEffect(() => {
    if (state !== "closed") return;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const done = () => onDismissRef.current(toast.id);
    if (reduce) {
      const timer = window.setTimeout(done, 0);
      return () => window.clearTimeout(timer);
    }
    const slot = slotRef.current;
    // The event and the fallback race for the same dismissal; whichever lands first wins once.
    let dismissed = false;
    const once = () => {
      if (dismissed) return;
      dismissed = true;
      done();
    };
    const onEnd = (e: TransitionEvent) => {
      if (e.target === slot && e.propertyName === "grid-template-rows") once();
    };
    slot?.addEventListener("transitionend", onEnd);
    const timer = window.setTimeout(once, TOAST_EXIT_FALLBACK_MS);
    return () => {
      slot?.removeEventListener("transitionend", onEnd);
      window.clearTimeout(timer);
    };
  }, [state, toast.id]);

  return (
    <div ref={slotRef} className="alk-toast-slot" data-state={state}>
      <div className="alk-toast-slot__inner">
        <div
          className="alk-toast"
          data-state={state}
          onMouseEnter={() => setPaused(true)}
          onMouseLeave={() => setPaused(false)}
        >
          <div
            className="alk-toast__main"
            data-tone={toast.tone ?? "info"}
            role={toast.role ?? (toast.tone === "danger" ? "alert" : "status")}
          >
            <span className="alk-toast__icon" aria-hidden="true">
              {toast.icon ?? <Glyph size={16} />}
            </span>
            <div className="alk-toast__content">
              {toast.title != null ? <p className="alk-toast__title">{toast.title}</p> : null}
              {toast.message != null ? <div className="alk-toast__text">{toast.message}</div> : null}
            </div>
          </div>
          {closable ? (
            <Button
              iconOnly
              variant="secondary" fill="ghost"
              className="alk-toast__close"
              size="sm"
              aria-label="Dismiss"
              onClick={() => setState("closed")}
            >
              <CloseIcon size={16} />
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export interface ToastViewportProps {
  toasts: ToastEntry[];
  onDismiss: (id: string) => void;
  /** Which corner / edge the stack pins to. Default `bottom-right`. */
  position?: ToastPosition;
  /** Cap how many are shown at once; older toasts overflow out of view (still queued). A capped-out
   *  toast is unmounted, so its auto-dismiss timer pauses until it surfaces again. */
  max?: number;
  /** Accessible name for the region landmark. */
  label?: string;
  /** Extra class/style on the viewport root — a host may need to re-pin or inset it. */
  className?: string;
  style?: CSSProperties;
}

/**
 * ToastViewport — the fixed, corner-pinned stacking container for one position.
 *
 * Stacking shadows never compound: every toast is opaque, and the DOM order always runs visual
 * top-to-bottom so the nearer toast paints over the one behind it and occludes its (downward)
 * shadow — only the stack's outer edge casts a shadow against the page. For a top-pinned stack the
 * newest belongs at the top, so the list is reversed for rendering while the top-to-bottom paint
 * order (and thus the occlusion) is preserved.
 */
export function ToastViewport({
  toasts,
  onDismiss,
  position = "bottom-right",
  max,
  label = "Notifications",
  className,
  style,
}: ToastViewportProps) {
  if (toasts.length === 0) return null;
  const shown = max != null ? toasts.slice(-max) : toasts;
  const ordered = position.startsWith("top") ? [...shown].reverse() : shown;
  return createPortal(
    <div className={cx("alk-toast-viewport", className)} style={style} data-position={position} role="region" aria-label={label}>
      {ordered.map((toast) => (
        <ToastItem key={toast.instanceKey ?? toast.id} toast={toast} onDismiss={onDismiss} />
      ))}
    </div>,
    document.body,
  );
}
