import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";

import { FieldShell, useFieldIds } from "../Field";
import { useDismiss, useFloating, usePresence, type FloatingResult } from "../../../hooks";
import { CalendarIcon, ChevronLeftIcon, ChevronRightIcon, CloseIcon } from "../../icons";
import type { ControlSize } from "../../sizes";

const EXIT_MS = 120;

/** One calendar day, month 1–12 — deliberately not a `Date`, so no timezone can shift it. */
interface IsoDay {
  year: number;
  month: number;
  day: number;
}

interface MonthView {
  year: number;
  month: number;
}

/** The month on display plus the day the roving tabindex sits on. */
interface CalendarPosition {
  view: MonthView;
  day: number;
}

const ISO_DAY_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

function parseIsoDay(value: string): IsoDay | null {
  const match = ISO_DAY_RE.exec(value);
  if (!match) return null;
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])];
  if (month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) return null;
  return { year, month, day };
}

function toIso({ year, month, day }: IsoDay): string {
  return `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function daysInMonth(year: number, month: number): number {
  return new Date(year, month, 0).getDate();
}

function today(): IsoDay {
  const now = new Date();
  return { year: now.getFullYear(), month: now.getMonth() + 1, day: now.getDate() };
}

/** Step the focused day by `delta`, turning the month page when it walks off an edge. */
function rollDay({ view, day }: CalendarPosition, delta: number): CalendarPosition {
  const next = day + delta;
  if (next >= 1 && next <= daysInMonth(view.year, view.month)) return { view, day: next };
  const rolled = new Date(view.year, view.month - 1, next);
  return {
    view: { year: rolled.getFullYear(), month: rolled.getMonth() + 1 },
    day: rolled.getDate(),
  };
}

/** Turn the page a whole month, keeping the focused day (clamped to the shorter month). */
function turnMonth({ view, day }: CalendarPosition, delta: 1 | -1): CalendarPosition {
  const next = new Date(view.year, view.month - 1 + delta, 1);
  const turned = { year: next.getFullYear(), month: next.getMonth() + 1 };
  return { view: turned, day: Math.min(day, daysInMonth(turned.year, turned.month)) };
}

const displayFmt = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
});
const monthFmt = new Intl.DateTimeFormat("en-US", { month: "long", year: "numeric" });
const dayLabelFmt = new Intl.DateTimeFormat("en-US", {
  month: "long",
  day: "numeric",
  year: "numeric",
});

function formatDay(day: IsoDay, formatter: Intl.DateTimeFormat): string {
  return formatter.format(new Date(day.year, day.month - 1, day.day));
}

const WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

export interface DateInputProps {
  label?: ReactNode;
  description?: ReactNode;
  error?: ReactNode;
  /** Control height — `sm` 28 / `md` 32 / `lg` 38 (default), the shared field scale. */
  size?: ControlSize;
  /** Class/style for the field wrapper (TextInput/Select parity) — a form-row basis lands here. */
  rootClassName?: string;
  rootStyle?: CSSProperties;
  /** The picked day as `YYYY-MM-DD`, or `""` for unset. */
  value?: string;
  defaultValue?: string;
  /** Fires with the next `YYYY-MM-DD`, or `""` when cleared. */
  onChange?: (value: string) => void;
  placeholder?: string;
  /** Show a clear ✕ while a day is picked (default true — a filter field must be unsettable). */
  clearable?: boolean;
  disabled?: boolean;
  required?: boolean;
  /** Show the required mark beside the label. Default `true`; see `FieldShellProps.requiredMark`. */
  requiredMark?: boolean;
  id?: string;
  /** Submitted through a hidden input when set, so the field works in a plain form. */
  name?: string;
  "aria-label"?: string;
}

/**
 * DateInput — a single-day picker on the shared field scale: a trigger that reads like an input
 * and a portaled calendar panel on the floating-surface system. Never the browser's native
 * date control, so the field renders identically across platforms and themes.
 */
export function DateInput({
  label,
  description,
  error,
  size = "lg",
  rootClassName,
  rootStyle,
  value,
  defaultValue,
  onChange,
  placeholder = "Any date",
  clearable = true,
  disabled,
  required,
    requiredMark,
  id: idProp,
  name,
  "aria-label": ariaLabel,
}: DateInputProps) {
  const { id, descId, errId, describedBy } = useFieldIds(idProp, { description, error });
  const isControlled = value !== undefined;
  const [internal, setInternal] = useState(defaultValue ?? "");
  const currentValue = isControlled ? (value ?? "") : internal;
  const selected = parseIsoDay(currentValue);

  const [open, setOpen] = useState(false);
  const { mounted, state } = usePresence(open, EXIT_MS);
  const wrapRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const float = useFloating({ open, side: "bottom" });

  const seed = selected ?? today();
  const [calendar, setCalendar] = useState<CalendarPosition>(() => ({
    view: { year: seed.year, month: seed.month },
    day: seed.day,
  }));
  // Reseed on every open so the panel always lands on the picked (or current) month.
  useEffect(() => {
    if (open) setCalendar({ view: { year: seed.year, month: seed.month }, day: seed.day });
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps -- reseed only when the panel opens

  // The panel is a dialog: focus follows it in, and the day grid is one roving tab stop.
  useEffect(() => {
    if (!open) return;
    panelRef.current?.querySelector<HTMLButtonElement>(`[data-day="${calendar.day}"]`)?.focus();
  }, [open, calendar]);

  const commit = useCallback(
    (next: string) => {
      if (!isControlled) setInternal(next);
      onChange?.(next);
    },
    [isControlled, onChange],
  );

  const exit = useCallback(() => {
    setOpen(false);
    triggerRef.current?.focus();
  }, []);

  const pick = useCallback(
    (day: number) => {
      commit(toIso({ year: calendar.view.year, month: calendar.view.month, day }));
      exit();
    },
    [commit, calendar, exit],
  );

  const onDismiss = useCallback((target?: Node) => {
    if (target && panelRef.current?.contains(target)) return;
    setOpen(false);
  }, []);
  useDismiss(wrapRef, open, onDismiss);

  const setPanel = useCallback(
    (el: HTMLDivElement | null) => {
      panelRef.current = el;
      float.setFloating(el);
    },
    [float],
  );

  const showClear = Boolean(clearable && selected && !disabled);
  const triggerProps = {
    id,
    size,
    open,
    panelId,
    selected,
    placeholder,
    disabled,
    invalid: Boolean(error),
    describedBy,
    ariaLabel,
    showClear,
    triggerRef,
    setReference: float.setReference,
    onToggle: () => setOpen((v) => !v),
  };
  const panelProps = {
    wrapRef,
    panelId,
    label,
    ariaLabel,
    float,
    presence: state,
    setPanel,
    calendar,
    selected,
    onTurn: (delta: 1 | -1) => setCalendar((c) => turnMonth(c, delta)),
    onMove: (delta: number) => setCalendar((c) => rollDay(c, delta)),
    onPick: pick,
    onExit: exit,
  };
  return (
    <FieldShell
      htmlFor={id}
      label={label}
      description={description}
      descId={descId}
      error={error}
      errId={errId}
      required={required}
      requiredMark={requiredMark}
      className={rootClassName}
      style={rootStyle}
    >
      <div className="alk-dateinput-root" ref={wrapRef}>
        {name ? <input type="hidden" name={name} value={currentValue} /> : null}
        <DateTrigger {...triggerProps} />
        {showClear ? <ClearDateButton onClear={() => commit("")} /> : null}
        {mounted ? <CalendarPortal {...panelProps} /> : null}
      </div>
    </FieldShell>
  );
}

function ClearDateButton({ onClear }: { onClear: () => void }) {
  return (
    <button type="button" className="alk-input__affix-btn" aria-label="Clear date" onClick={onClear}>
      <CloseIcon size={13} />
    </button>
  );
}

function DateTrigger({
  id,
  size,
  open,
  panelId,
  selected,
  placeholder,
  disabled,
  invalid,
  describedBy,
  ariaLabel,
  showClear,
  triggerRef,
  setReference,
  onToggle,
}: {
  id: string;
  size: ControlSize;
  open: boolean;
  panelId: string;
  selected: IsoDay | null;
  placeholder: string;
  disabled: boolean | undefined;
  invalid: boolean;
  describedBy: string | undefined;
  ariaLabel: string | undefined;
  showClear: boolean;
  triggerRef: RefObject<HTMLButtonElement | null>;
  setReference: (el: HTMLElement | null) => void;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      id={id}
      ref={(el) => {
        triggerRef.current = el;
        setReference(el);
      }}
      className="alk-dateinput__trigger"
      disabled={disabled}
      aria-haspopup="dialog"
      aria-expanded={open}
      aria-controls={panelId}
      aria-label={ariaLabel}
      aria-invalid={invalid || undefined}
      aria-describedby={describedBy}
      data-size={size !== "lg" ? size : undefined}
      data-placeholder={selected ? undefined : ""}
      data-clearable={showClear || undefined}
      onClick={() => !disabled && onToggle()}
    >
      <CalendarIcon size={15} className="alk-dateinput__lead" />
      <span className="alk-dateinput__value">
        {selected ? formatDay(selected, displayFmt) : placeholder}
      </span>
    </button>
  );
}

function CalendarPortal({
  wrapRef,
  panelId,
  label,
  ariaLabel,
  float,
  presence,
  setPanel,
  calendar,
  selected,
  onTurn,
  onMove,
  onPick,
  onExit,
}: {
  wrapRef: RefObject<HTMLDivElement | null>;
  panelId: string;
  label: ReactNode;
  ariaLabel: string | undefined;
  float: FloatingResult;
  presence: string;
  setPanel: (el: HTMLDivElement | null) => void;
  calendar: CalendarPosition;
  selected: IsoDay | null;
  onTurn: (delta: 1 | -1) => void;
  onMove: (delta: number) => void;
  onPick: (day: number) => void;
  onExit: () => void;
}) {
  const name = typeof label === "string" ? label : ariaLabel;
  const target =
    (wrapRef.current?.closest("[data-alkera-color-scheme]") as HTMLElement | null) ?? document.body;
  // Tab leaves the calendar back to the field (the Select's convention) — the panel is a late
  // body sibling, so an unhandled Tab would carry focus out of the app with the panel still open.
  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key !== "Tab") return;
    e.preventDefault();
    onExit();
  };
  return createPortal(
    <div
      ref={setPanel}
      id={panelId}
      role="dialog"
      aria-label={name ? `${name} calendar` : "Calendar"}
      className="alk-floating-surface alk-dateinput__panel"
      style={float.floatingStyles}
      data-side={float.side}
      data-state={presence}
      onKeyDown={onKeyDown}
    >
      <CalendarHeader view={calendar.view} onTurn={onTurn} />
      <CalendarGrid calendar={calendar} selected={selected} onMove={onMove} onPick={onPick} />
    </div>,
    target,
  );
}

function CalendarHeader({ view, onTurn }: { view: MonthView; onTurn: (delta: 1 | -1) => void }) {
  return (
    <div className="alk-dateinput__head">
      <button type="button" className="alk-dateinput__nav" aria-label="Previous month" onClick={() => onTurn(-1)}>
        <ChevronLeftIcon size={15} />
      </button>
      <span className="alk-dateinput__month" aria-live="polite">
        {monthFmt.format(new Date(view.year, view.month - 1, 1))}
      </span>
      <button type="button" className="alk-dateinput__nav" aria-label="Next month" onClick={() => onTurn(1)}>
        <ChevronRightIcon size={15} />
      </button>
    </div>
  );
}

const GRID_DELTAS: Record<string, number> = {
  ArrowLeft: -1,
  ArrowRight: 1,
  ArrowUp: -7,
  ArrowDown: 7,
};

function CalendarGrid({
  calendar,
  selected,
  onMove,
  onPick,
}: {
  calendar: CalendarPosition;
  selected: IsoDay | null;
  onMove: (delta: number) => void;
  onPick: (day: number) => void;
}) {
  const { view, day: focusDay } = calendar;
  const lead = new Date(view.year, view.month - 1, 1).getDay();
  const total = daysInMonth(view.year, view.month);
  const onKeyDown = (e: ReactKeyboardEvent) => {
    const delta = GRID_DELTAS[e.key];
    if (delta === undefined) return;
    e.preventDefault();
    onMove(delta);
  };
  const headers = WEEKDAYS.map((name) => (
    <span key={name} className="alk-dateinput__weekday" aria-hidden="true">
      {name}
    </span>
  ));
  const padding = Array.from({ length: lead }, (_, i) => <span key={`pad-${i}`} aria-hidden="true" />);
  const days = Array.from({ length: total }, (_, i) => (
    <DayCell key={i + 1} day={i + 1} view={view} selected={selected} focusDay={focusDay} onPick={onPick} />
  ));
  return (
    <div className="alk-dateinput__grid" onKeyDown={onKeyDown}>
      {headers}
      {padding}
      {days}
    </div>
  );
}

function DayCell({
  day,
  view,
  selected,
  focusDay,
  onPick,
}: {
  day: number;
  view: MonthView;
  selected: IsoDay | null;
  focusDay: number;
  onPick: (day: number) => void;
}) {
  const cell = { year: view.year, month: view.month, day };
  const isSelected =
    selected !== null && toIso(selected) === toIso(cell);
  const isToday = toIso(today()) === toIso(cell);
  return (
    <button
      type="button"
      className="alk-dateinput__day"
      data-day={day}
      data-on={isSelected || undefined}
      data-today={isToday || undefined}
      tabIndex={day === focusDay ? 0 : -1}
      aria-pressed={isSelected}
      aria-current={isToday ? "date" : undefined}
      aria-label={formatDay(cell, dayLabelFmt)}
      onClick={() => onPick(day)}
    >
      {day}
    </button>
  );
}
