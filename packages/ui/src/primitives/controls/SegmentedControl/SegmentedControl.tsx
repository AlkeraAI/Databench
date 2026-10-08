import type { CSSProperties, HTMLAttributes, KeyboardEvent, ReactNode } from "react";

import { cx } from "../../cx";
import { Pill } from "../../display/Pill";
import { Tooltip, type TooltipTriggerProps } from "../../overlays/Tooltip";
import type { ControlSize } from "../../sizes";

// Segmented control — a small set of mutually exclusive options. The active marker slides between
// states instead of teleporting. Tablist semantics; the outer box shares the large-control height
// (--alkControlLg) with the button + search bar so the three line up in a toolbar. A label that
// outgrows its tab truncates with an ellipsis rather than widening the control. Styling props
// (className/style/data-*) forward to the root, so a toolbar can size/place it like its neighbours.
// An option's label is a node, so a caller can ride an icon alongside the text; an optional `title`
// keeps the hover/AT tooltip a plain string when the label is markup. A `count` rides a trailing
// numeric Pill that brightens to the brand tint on the active segment (a scope switch with tallies).
// `semantics="radio"` announces it as a radio group instead, for a control that sets a value (a unit,
// a mode) rather than switching the view: a screen reader reading tabs expects panels to follow.
export interface SegmentedOption {
  key: string;
  label: ReactNode;
  title?: string;
  /** A rich hover/focus tooltip on the option (the Tooltip primitive, not the native `title`) — for
   *  a longer explanation than the label carries. Falls back to `title` when unset. */
  tooltip?: ReactNode;
  /** A trailing count chip beside the label — brand-tinted on the active segment, neutral otherwise. */
  count?: number;
}

export interface SegmentedControlProps extends Omit<HTMLAttributes<HTMLDivElement>, "onChange"> {
  options: readonly SegmentedOption[];
  value: string;
  onChange: (k: string) => void;
  label: string;
  /** Visual shape. `rect` (default) is the rounded-rectangle toolbar control; `pill` is the
   *  fully-rounded bordered variant. Overflow handling (centered, ellipsis-truncated labels) is
   *  identical for both — it lives in the base, not the caller. */
  shape?: "rect" | "pill";
  /** Control height — `sm` 28 / `md` 32 / `lg` 38 (default). Lines up with a button / search bar of
   *  the same size in a toolbar row. */
  size?: ControlSize;
  /** What assistive tech is told the control is. `tabs` (default) for a switch between views; `radio`
   *  for a value picked in place (a unit beside an amount), a radio group with one tab stop that the
   *  arrow keys move and pick along. */
  semantics?: "tabs" | "radio";
}

const PREVIOUS = new Set(["ArrowLeft", "ArrowUp"]);
const NEXT = new Set(["ArrowRight", "ArrowDown"]);

export function SegmentedControl({ options, value, onChange, label, shape = "rect", size = "lg", semantics = "tabs", className, style, ...rest }: SegmentedControlProps) {
  const radio = semantics === "radio";
  // No fallback to 0: an unmatched value parks no marker (and aria-selected is false on every tab),
  // so the visual never disagrees with the selection state.
  const active = options.findIndex((o) => o.key === value);
  const rootStyle: CSSProperties = { ["--n" as string]: options.length, ...style };
  return (
    <div
      {...rest}
      className={cx("alk-seg", className)}
      data-shape={shape !== "rect" ? shape : undefined}
      data-size={size !== "lg" ? size : undefined}
      role={radio ? "radiogroup" : "tablist"}
      aria-label={label}
      style={rootStyle}
      onKeyDown={radio ? (e) => moveRadio(e, options, active, onChange) : undefined}
    >
      {active >= 0 ? (
        <span className="alk-seg__ind" style={{ transform: `translateX(${active * 100}%)` }} aria-hidden="true" />
      ) : null}
      {options.map((o) => {
        const nativeTitle = o.tooltip == null ? (o.title ?? (typeof o.label === "string" ? o.label : undefined)) : undefined;
        const button = (trigger?: TooltipTriggerProps) => (
          <button
            key={o.key}
            ref={trigger?.ref as ((el: HTMLButtonElement | null) => void) | undefined}
            type="button"
            role={radio ? "radio" : "tab"}
            aria-selected={radio ? undefined : value === o.key}
            aria-checked={radio ? value === o.key : undefined}
            // One tab stop for the group: the checked option, or the first when none is.
            tabIndex={radio ? (o.key === (options[active] ?? options[0])?.key ? 0 : -1) : undefined}
            aria-describedby={trigger?.["aria-describedby"]}
            data-on={value === o.key || undefined}
            onClick={() => onChange(o.key)}
            onPointerEnter={trigger?.onPointerEnter}
            onPointerLeave={trigger?.onPointerLeave}
            onFocus={trigger?.onFocus}
            onBlur={trigger?.onBlur}
          >
            <span className="alk-seg__label" title={nativeTitle}>
              {o.label}
            </span>
            {o.count != null ? (
              <Pill numeric tone={value === o.key ? "brand" : "neutral"} className="alk-seg__count">
                {o.count}
              </Pill>
            ) : null}
          </button>
        );
        return o.tooltip != null ? (
          <Tooltip key={o.key} label={o.tooltip} wrap maxWidth={260}>
            {(trigger) => button(trigger)}
          </Tooltip>
        ) : (
          button()
        );
      })}
    </div>
  );
}

/** Arrow keys in a radio group move to the previous / next option and pick it, wrapping at the ends,
 *  as a native radio group does. */
function moveRadio(e: KeyboardEvent<HTMLDivElement>, options: readonly SegmentedOption[], active: number, onChange: (k: string) => void) {
  const step = PREVIOUS.has(e.key) ? -1 : NEXT.has(e.key) ? 1 : 0;
  if (step === 0 || options.length === 0) return;
  e.preventDefault();
  const from = active >= 0 ? active : 0;
  const to = (from + step + options.length) % options.length;
  onChange(options[to].key);
  const radios = e.currentTarget.querySelectorAll<HTMLButtonElement>('[role="radio"]');
  radios[to]?.focus();
}
