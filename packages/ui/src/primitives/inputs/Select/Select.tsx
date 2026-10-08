import {
  Children,
  forwardRef,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ChangeEvent as ReactChangeEvent,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type OptionHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
} from "react";
import { createPortal } from "react-dom";

import { cx } from "../../cx";
import { FieldShell, useFieldIds } from "../Field";
import { useDismiss, useFloating, usePresence } from "../../../hooks";
import { CheckIcon, ChevronDownIcon } from "../../icons";
import type { ControlSize } from "../../sizes";

/** Which edge the selected row's check rides. `"left"` keeps it in the leading mark column (today's
 *  default), `"right"` trails it and flushes labels left. It carries the Dropdown's `tickSide` name,
 *  and on a glyph-less list the two draw the same row. They part once glyphs are in play, for the
 *  reason on `optionIcons`. */
export type SelectTickSide = "left" | "right";

export interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, "size"> {
  label?: ReactNode;
  description?: ReactNode;
  error?: ReactNode;
  /** Show the required mark beside the label. Default `true`; see `FieldShellProps.requiredMark`. */
  requiredMark?: boolean;
  /** Control height — `sm` 28 / `md` 32 / `lg` 38 (default). Lines up with a button / input of the
   *  same size in a row. (Replaces the native `size` visible-rows attribute, unused by this listbox.) */
  size?: ControlSize;
  /** Class for the field wrapper; the trigger keeps `className`. Applied to the control's own root
   *  when the field renders bare (no label/description/error), so it is never dropped. */
  rootClassName?: string;
  /** Inline style for the same wrapper `rootClassName` targets — e.g. a flex basis in a form row
   *  (TextInput parity). */
  rootStyle?: CSSProperties;
  /** Per-option glyphs, keyed by option value. A row's glyph rides the LEADING mark slot. Under the
   *  default `tickSide` the check takes that same slot on the selected row (never both, so rows
   *  never double-indent), the same treatment as the Dropdown's menu rows. Under `tickSide="right"`
   *  the glyph keeps its column and the check trails the label, so a selected row shows both.
   *  `DropdownItem` instead pins the tick back to the left on any row carrying an icon. It can,
   *  because a menu's icons are per row. This map is read for the whole listbox, and a panel showing
   *  every row at once cannot walk the check between columns as the selection moves. The closed
   *  trigger leads with the selected option's glyph. */
  optionIcons?: Record<string, ReactNode>;
  /** What the closed trigger shows for an option, keyed by option value, when it should be shorter
   *  than the option's row in the open list (a name on the trigger, the name with its details in the
   *  list). An option it does not name shows its own label. */
  triggerLabels?: Record<string, ReactNode>;
  /** Side the check rides (default `"left"`). Use `"right"` to flush labels to the panel's leading
   *  edge, with the check pinned to the row's trailing edge. A label too long for the row truncates
   *  before it reaches the check. The closed trigger looks the same either way. */
  tickSide?: SelectTickSide;
}

/** One parsed `<option>` — its form value, its visible label, and whether it's pickable. */
interface ParsedOption {
  value: string;
  label: ReactNode;
  /** Flattened text of `label`, for type-ahead matching + the trigger's fallback text. */
  text: string;
  disabled: boolean;
}

const EXIT_MS = 120;

/** Flatten an option's children to plain text — the value used for the closed trigger label
 *  (when the rich label is just a string) and for type-ahead matching. */
function optionText(node: ReactNode): string {
  if (node == null || node === false || node === true) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(optionText).join("");
  if (isValidElement(node)) return optionText((node.props as { children?: ReactNode }).children);
  return "";
}

/** Walk the `<option>` children into a flat option list. Non-option children are ignored, so a
 *  stray whitespace string or a comment can't become a phantom row. */
function parseOptions(children: ReactNode): ParsedOption[] {
  const out: ParsedOption[] = [];
  Children.forEach(children, (child) => {
    if (!isValidElement(child) || child.type !== "option") return;
    const props = child.props as OptionHTMLAttributes<HTMLOptionElement>;
    const label = props.children ?? "";
    const value = props.value != null ? String(props.value) : optionText(label);
    out.push({ value, label, text: optionText(label), disabled: Boolean(props.disabled) });
  });
  return out;
}

/**
 * Select — a custom floating listbox over a hidden native `<select>`.
 *
 * The real `<select>` stays in the DOM (visually hidden, the `ref` target) so the field submits in
 * a form, fires `onChange`, and is the single source of truth for the value. The visible chrome is a
 * button + a portaled `role="listbox"` panel that shares the Dropdown's surface, shadow, and
 * open/close animation — never the OS menu. Callers keep the `<option>` children API unchanged.
 */
export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  {
    className,
    rootClassName,
    rootStyle,
    optionIcons,
    triggerLabels,
    tickSide = "left",
    size = "lg",
    children,
    label,
    description,
    error,
    required,
    requiredMark,
    disabled,
    id: idProp,
    name,
    value,
    defaultValue,
    onChange,
    "aria-describedby": ariaDescribedBy,
    "aria-label": ariaLabel,
    ...rest
  },
  ref,
) {
  const { id, descId, errId, describedBy } = useFieldIds(idProp, { description, error });
  const options = useMemo(() => parseOptions(children), [children]);
  const firstEnabled = options.find((o) => !o.disabled)?.value;

  // Value source of truth: controlled tracks `value`; uncontrolled keeps its own state seeded from
  // `defaultValue` (falling back to the first enabled option, matching native `<select>`).
  const isControlled = value !== undefined;
  const [internalValue, setInternalValue] = useState<string>(() => {
    const seed = defaultValue ?? value;
    return seed != null ? String(seed) : (firstEnabled ?? "");
  });
  const currentValue = isControlled && value != null ? String(value) : internalValue;
  const selected = options.find((o) => o.value === currentValue);

  const selectRef = useRef<HTMLSelectElement | null>(null);
  const setSelectRef = useCallback(
    (el: HTMLSelectElement | null) => {
      selectRef.current = el;
      if (typeof ref === "function") ref(el);
      else if (ref) ref.current = el;
    },
    [ref],
  );

  const [open, setOpen] = useState(false);
  const { mounted, state } = usePresence(open, EXIT_MS);
  const wrapRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const listId = useId();
  const float = useFloating({ open, side: "bottom", matchWidth: true });

  // Active descendant for keyboard navigation — the row Up/Down/Enter acts on. Reseeds to the
  // selected (or first enabled) row each time the panel opens.
  const [activeIndex, setActiveIndex] = useState(-1);
  useEffect(() => {
    if (!open) return;
    const sel = options.findIndex((o) => o.value === currentValue);
    const first = options.findIndex((o) => !o.disabled);
    setActiveIndex(sel >= 0 ? sel : first);
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps -- reseed only when the panel opens

  // Commit a pick by driving the real `<select>`: set its value, then dispatch a native `change` so
  // React's synthetic handler (`onNativeChange`) runs — updating uncontrolled state + relaying the
  // consumer's `onChange`. The hidden select stays the one source of truth for the value.
  const commit = useCallback((next: string) => {
    const el = selectRef.current;
    if (!el || el.value === next) return;
    const setNativeValue = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    if (setNativeValue) setNativeValue.call(el, next);
    else el.value = next;
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }, []);

  // The native select is the form control, so its `change` is what reaches the consumer. Always
  // supply a handler (even controlled with no `onChange`) so React never warns about a controlled
  // field without one; we relay to the consumer's `onChange` when present.
  const onNativeChange = useCallback(
    (e: ReactChangeEvent<HTMLSelectElement>) => {
      if (!isControlled) setInternalValue(e.currentTarget.value);
      onChange?.(e);
    },
    [isControlled, onChange],
  );

  const close = useCallback(() => setOpen(false), []);
  const portalTarget =
    (mounted ? (wrapRef.current?.closest("[data-alkera-color-scheme]") as HTMLElement | null) : null) ??
    document.body;

  // Escape routes ONLY through the shared overlay stack (registered by useDismiss while open), so
  // one press closes exactly the topmost surface. A trigger-level Escape close unregistered that
  // entry before the press reached the stack's document listener, and the same press fell through
  // to the page layer beneath, dismissing two surfaces at once. The stable callback registers the
  // entry once per open instead of re-pushing it on every render.
  const onDismiss = useCallback(
    (target?: Node) => {
      if (target && panelRef.current?.contains(target)) return;
      close();
    },
    [close],
  );
  useDismiss(wrapRef, open, onDismiss);

  const setPanel = useCallback(
    (el: HTMLDivElement | null) => {
      panelRef.current = el;
      float.setFloating(el);
    },
    [float],
  );

  const pick = useCallback(
    (index: number) => {
      const opt = options[index];
      if (!opt || opt.disabled) return;
      commit(opt.value);
      close();
    },
    [options, commit, close],
  );

  const moveActive = useCallback(
    (dir: 1 | -1) => {
      setActiveIndex((cur) => {
        const len = options.length;
        let i = cur;
        for (let step = 0; step < len; step += 1) {
          i = (i + dir + len) % len;
          if (!options[i]?.disabled) return i;
        }
        return cur;
      });
    },
    [options],
  );

  const onTriggerKeyDown = useCallback(
    (e: ReactKeyboardEvent) => {
      if (disabled) return;
      switch (e.key) {
        case "ArrowDown":
        case "ArrowUp":
          e.preventDefault();
          if (!open) setOpen(true);
          else moveActive(e.key === "ArrowDown" ? 1 : -1);
          break;
        case "Enter":
          if (open) {
            e.preventDefault();
            pick(activeIndex);
          }
          break;
        case " ":
          if (!open) {
            e.preventDefault();
            setOpen(true);
          }
          break;
        case "Home":
          if (open) {
            e.preventDefault();
            setActiveIndex(options.findIndex((o) => !o.disabled));
          }
          break;
        case "End":
          if (open) {
            e.preventDefault();
            for (let i = options.length - 1; i >= 0; i -= 1) {
              if (!options[i]?.disabled) {
                setActiveIndex(i);
                break;
              }
            }
          }
          break;
        case "Tab":
          if (open) close();
          break;
      }
    },
    [disabled, open, activeIndex, options, moveActive, pick, close],
  );

  const triggerText = selected ? selected.text : "";
  const placeholderish = !selected || triggerText === "";
  const selectedIcon = selected ? optionIcons?.[selected.value] : undefined;

  // The leading column earns its space only when something occupies it, so a trailing check over
  // rows that carry no glyph drops the column and the labels flush left. Once ANY row has a glyph
  // the column stays on every row, so labels hold their place as the selection moves and a selected
  // row keeps its glyph instead of trading it for the check. A map that fills no row is measured,
  // not assumed: an empty or non-matching `optionIcons` must not cost every label an indent.
  const tickRight = tickSide === "right";
  const anyGlyph = options.some((opt) => optionIcons?.[opt.value] != null);
  const leadSlot = !tickRight || anyGlyph;

  // Bare (no label/description/error) FieldShell renders children unwrapped — the root props land on
  // the control's own root instead, so a toolbar/form-row Select can still take a flex basis.
  const bare = !label && !description && !error;
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
      <div className={cx("alk-select-root", bare && rootClassName)} style={bare ? rootStyle : undefined} ref={wrapRef}>
        {/* The real form control: visually hidden, but the value source of truth + the ref target. */}
        <select
          ref={setSelectRef}
          id={`${id}-native`}
          name={name}
          required={required}
          disabled={disabled}
          aria-hidden="true"
          tabIndex={-1}
          className="alk-select__native"
          value={isControlled ? (value != null ? String(value) : "") : undefined}
          defaultValue={isControlled ? undefined : currentValue}
          onChange={onNativeChange}
          {...rest}
        >
          {children}
        </select>

        <button
          type="button"
          id={id}
          ref={float.setReference}
          className={cx("alk-select__trigger", className)}
          disabled={disabled}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={listId}
          aria-label={ariaLabel}
          aria-invalid={error ? true : undefined}
          aria-describedby={cx(ariaDescribedBy, describedBy) || undefined}
          data-size={size !== "lg" ? size : undefined}
          data-placeholder={placeholderish ? "" : undefined}
          data-open={open || undefined}
          onClick={() => !disabled && setOpen((v) => !v)}
          onKeyDown={onTriggerKeyDown}
        >
          {selectedIcon != null ? (
            <span className="alk-select__lead" aria-hidden="true">
              {selectedIcon}
            </span>
          ) : null}
          <span className="alk-select__value">{selected ? (triggerLabels?.[selected.value] ?? selected.label) : " "}</span>
          <ChevronDownIcon size={16} className="alk-select__caret" />
        </button>

        {mounted
          ? createPortal(
              <div
                ref={setPanel}
                id={listId}
                className="alk-floating-surface alk-select__panel"
                style={float.floatingStyles}
                data-side={float.side}
                data-state={state}
                role="listbox"
                aria-label={typeof label === "string" ? label : (ariaLabel ?? "Options")}
                aria-activedescendant={activeIndex >= 0 ? `${listId}-opt-${activeIndex}` : undefined}
              >
                {options.map((opt, i) => {
                  const isSelected = opt.value === currentValue;
                  return (
                    <div
                      key={`${opt.value}-${i}`}
                      id={`${listId}-opt-${i}`}
                      role="option"
                      aria-selected={isSelected}
                      aria-disabled={opt.disabled || undefined}
                      className="alk-select__option"
                      data-tick={tickRight ? "right" : undefined}
                      data-on={isSelected || undefined}
                      data-active={i === activeIndex || undefined}
                      data-disabled={opt.disabled || undefined}
                      onMouseEnter={() => !opt.disabled && setActiveIndex(i)}
                      onMouseDown={(e) => {
                        // Keep focus on the trigger so an outside-press dismiss never misfires.
                        e.preventDefault();
                        pick(i);
                      }}
                    >
                      {leadSlot ? (
                        <span className="alk-option-mark alk-select__option-mark" aria-hidden="true">
                          {isSelected && !tickRight ? <CheckIcon size={14} /> : (optionIcons?.[opt.value] ?? null)}
                        </span>
                      ) : null}
                      <span className="alk-select__option-label">{opt.label}</span>
                      {tickRight && isSelected ? (
                        <span className="alk-option-mark alk-select__option-mark" aria-hidden="true">
                          <CheckIcon size={14} />
                        </span>
                      ) : null}
                    </div>
                  );
                })}
              </div>,
              portalTarget,
            )
          : null}
      </div>
    </FieldShell>
  );
});
