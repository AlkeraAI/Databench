import { useId } from "react";

export interface FieldIds {
  /** The control's id; the label's `htmlFor`. */
  id: string;
  descId?: string;
  errId?: string;
  /** Space-joined ids for the control's `aria-describedby`, or undefined. */
  describedBy?: string;
}

/** Derive the stable id set that wires a control to its label, description, and
 *  error. An explicit `id` wins; otherwise one is generated. The description and
 *  error ids exist only when their content does, so `aria-describedby` never points
 *  at an absent node. */
export function useFieldIds(
  idProp: string | undefined,
  parts: { description?: unknown; error?: unknown },
): FieldIds {
  const auto = useId();
  const id = idProp ?? auto;
  const descId = parts.description ? `${id}-desc` : undefined;
  const errId = parts.error ? `${id}-err` : undefined;
  const describedBy = [descId, errId].filter(Boolean).join(" ") || undefined;
  return { id, descId, errId, describedBy };
}
