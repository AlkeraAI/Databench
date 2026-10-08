import type { ReactNode } from "react";

import { cx } from "../../cx";
import { Spinner } from "../../icons";
import { Meter } from "../Meter";
import { Skeleton } from "../Skeleton";

export type LoadingForm = "spinner" | "skeleton" | "meter";
export type LoadingSize = "sm" | "md" | "lg";

export interface LoadingIndicatorProps {
  /** What the wait looks like: a `spinner` (default) for an unknown span, `skeleton` for
   *  content about to take the same shape, `meter` for a wait with a measured fraction. */
  form?: LoadingForm;
  /** What is being waited on ("Connecting to Notion..."). Shown beneath a spinner or meter,
   *  and the announced name in every form. */
  label?: string;
  size?: LoadingSize;
  /** The `meter` form's fraction, 0..1. The other forms ignore it. */
  value?: number;
  className?: string;
}

const SPINNER_PX: Record<LoadingSize, number> = { sm: 16, md: 24, lg: 32 };
const SKELETON_ROWS: Record<LoadingSize, number> = { sm: 1, md: 3, lg: 5 };

function mark(form: LoadingForm, size: LoadingSize, value: number): ReactNode {
  if (form === "spinner") return <Spinner size={SPINNER_PX[size]} />;
  if (form === "meter") return <Meter value={value} size={size === "sm" ? "sm" : "md"} />;
  return Array.from({ length: SKELETON_ROWS[size] }, (_, i) => (
    <Skeleton key={i} height={size === "lg" ? 16 : 12} width={i === 0 ? "60%" : "100%"} />
  ));
}

/**
 * LoadingIndicator — one wait, said one way. `role="status"` carries the label to assistive tech,
 * so a screen reader hears what is being waited on instead of finding a silent gap. The forms
 * compose the marks the estate already has (Spinner, Skeleton, Meter); this adds the
 * announcement and the centring, never a second spinner.
 */
export function LoadingIndicator({ form = "spinner", label, size = "md", value = 0, className }: LoadingIndicatorProps) {
  return (
    <div className={cx("alk-loading", className)} data-form={form} role="status" aria-label={label ?? "Loading"}>
      {mark(form, size, value)}
      {label && form !== "skeleton" ? <p className="alk-loading__label">{label}</p> : null}
    </div>
  );
}
