import {
  forwardRef,
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type FocusEvent,
  type InputHTMLAttributes,
  type ReactNode,
} from "react";

import { cx } from "../../cx";
import { FieldShell, useFieldIds } from "../Field";
import { useEscLayer } from "../../../hooks";
import { CloseIcon, EyeIcon, EyeOffIcon, SearchIcon } from "../../icons";
import type { ControlSize } from "../../sizes";

export interface TextInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "size"> {
  label?: ReactNode;
  /** A trailing slot on the label row — e.g. a verification status mark beside the label. */
  labelAccessory?: ReactNode;
  description?: ReactNode;
  /** Render the description above the input instead of below it. */
  descriptionAbove?: boolean;
  error?: ReactNode;
  /** Show the required mark beside the label. Default `true`; see `FieldShellProps.requiredMark`. */
  requiredMark?: boolean;
  /** Control height — `sm` 28 / `md` 32 / `lg` 38 (default). (Replaces the native `size` attr.) */
  size?: ControlSize;
  /** A leading glyph inside the field. Defaults to a magnifier for `type="search"`; pass `null` to
   *  drop it, or any node to override. */
  leftSection?: ReactNode;
  /** A control that rides inside the field's trailing edge — a filter menu on a search bar, a unit
   *  picker on a measurement. It takes the outermost slot and the field's own clear ✕ (or password
   *  reveal) sits to its left, so the field's built-in affordance never moves when a caller adds
   *  one. The field reserves a gutter for each visible slot, so the text never runs under either. */
  rightSection?: ReactNode;
  /** Show a clear ✕ when there's a value. Default `true` for `type="search"`, `false` otherwise. The
   *  ✕ is rendered ONLY when there's a value and floats over the reserved right padding — it never
   *  overlays the placeholder or blocks the text cursor. */
  clearable?: boolean;
  /** `type="search"` only: collapse to a magnifier tap-target while `collapseQuery` matches, expanding
   *  the field on open. The caller owns the breakpoint, so a non-collapsible field never inherits one. */
  collapsible?: boolean;
  /** The media query that drives the collapse. Default `(max-width: 560px)`. */
  collapseQuery?: string;
  /** Class for the field wrapper; the input keeps `className`. */
  rootClassName?: string;
  /** Inline style for the same wrapper `rootClassName` targets — e.g. a one-off `--alk-search-w`. */
  rootStyle?: CSSProperties;
}

function useMediaQuery(query: string, enabled: boolean): boolean {
  // Lazy-init from the live query so the first paint is correct (a post-effect read would flash the
  // expanded field before collapsing it on a narrow screen).
  const [matches, setMatches] = useState(() =>
    enabled && typeof window !== "undefined" ? window.matchMedia(query).matches : false,
  );
  useEffect(() => {
    if (!enabled) {
      setMatches(false);
      return;
    }
    const mq = window.matchMedia(query);
    const apply = () => setMatches(mq.matches);
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, [query, enabled]);
  return matches;
}

/** Set a controlled input's value AND fire React's `onChange` — so the clear ✕ reaches a controlled
 *  parent through its normal `onChange(e)` with the empty value. (React overrides the value setter,
 *  so we call the native one, then dispatch `input`.) */
function driveValue(el: HTMLInputElement, next: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  if (setter) setter.call(el, next);
  else el.value = next;
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

/**
 * TextInput — the one single-line text field. `type` selects the flavour (`text` by default):
 *  - `password` → a reveal toggle (the input stays the accessible control).
 *  - `search`   → a leading magnifier + a clear ✕ that appears only when there's a value (floating
 *                 over reserved right padding — never a dead slot over the field), and optional
 *                 collapse-to-icon on narrow screens (`collapsible`). A search field is a FIXED-width
 *                 toolbar control by default (unlike a form field, it doesn't fill its column) —
 *                 override with `rootClassName` (set `width`/`flex`, or `--alk-search-w`) to widen or
 *                 flex-fill it.
 *  - anything else (`email`, `url`, …) → a plain field.
 * `label` / `description` / `error` wire the field scaffold; omit them for a bare input.
 */
export const TextInput = forwardRef<HTMLInputElement, TextInputProps>(function TextInput(
  {
    className,
    rootClassName,
    rootStyle,
    type = "text",
    size = "lg",
    leftSection,
    rightSection,
    clearable,
    collapsible = false,
    collapseQuery = "(max-width: 560px)",
    label,
    labelAccessory,
    description,
    descriptionAbove,
    error,
    required,
    requiredMark,
    id: idProp,
    value,
    onBlur,
    onKeyDown,
    "aria-describedby": ariaDescribedBy,
    "aria-label": ariaLabel,
    ...rest
  },
  ref,
) {
  const isPassword = type === "password";
  const isSearch = type === "search";
  const [revealed, setRevealed] = useState(false);
  const { id, descId, errId, describedBy } = useFieldIds(idProp, { description, error });

  const localRef = useRef<HTMLInputElement | null>(null);
  const setRefs = useCallback(
    (el: HTMLInputElement | null) => {
      localRef.current = el;
      if (typeof ref === "function") ref(el);
      else if (ref) ref.current = el;
    },
    [ref],
  );

  // Collapsible search: fold to a magnifier while narrow; open on tap, fold back on blur when empty
  // or on Escape (focus returns to the toggle so a keyboard user isn't stranded).
  const canCollapse = collapsible && isSearch;
  const collapsed = useMediaQuery(collapseQuery, canCollapse);
  const [expanded, setExpanded] = useState(false);
  const toggleRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (!collapsed) setExpanded(false);
  }, [collapsed]);

  const inputType = isPassword ? (revealed ? "text" : "password") : type;
  const lead = leftSection !== undefined ? leftSection : isSearch ? <SearchIcon size={16} /> : null;
  const hasValue = value != null && value !== "";
  const showClear = (clearable ?? isSearch) && hasValue;
  const trailSlots = (isPassword || showClear ? 1 : 0) + (rightSection != null ? 1 : 0);

  const clear = () => {
    const el = localRef.current;
    if (el) {
      driveValue(el, "");
      el.focus();
    }
  };

  const onFieldBlur = (e: FocusEvent<HTMLInputElement>) => {
    onBlur?.(e);
    if (collapsed && !hasValue) setExpanded(false);
  };

  // The empty expanded search is a transient surface, so its Escape fold joins the shared overlay
  // stack: one press folds only the topmost layer, and a page-level Escape handler beneath never
  // fires on the same press. A field-local keydown fold leaked that press to the layer below. With
  // a value the layer stands down (WebKit's native search-Escape clears the text first; the next
  // press, now empty, folds), and a plain or folded search never joins the stack at all.
  const fold = useCallback(() => {
    setExpanded(false);
    requestAnimationFrame(() => toggleRef.current?.focus());
  }, []);
  useEscLayer(collapsed && expanded && !hasValue, fold);

  const builtIn = isPassword ? (
    <button
      type="button"
      className="alk-input__affix-btn"
      onClick={() => setRevealed((v) => !v)}
      aria-label={revealed ? "Hide password" : "Show password"}
      aria-pressed={revealed}
    >
      {revealed ? <EyeOffIcon size={16} /> : <EyeIcon size={16} />}
    </button>
  ) : showClear ? (
    <button type="button" className="alk-input__affix-btn" onClick={clear} aria-label={isSearch ? "Clear search" : "Clear"}>
      <CloseIcon size={16} />
    </button>
  ) : null;

  const trailing =
    builtIn || rightSection != null ? (
      <span className="alk-input__trail">
        {builtIn}
        {rightSection}
      </span>
    ) : null;

  // rootClassName styles the field container. With a label/desc/error it's FieldShell's wrapper;
  // for a bare field (a toolbar search) it rides the outermost field box below, since FieldShell
  // then renders nothing of its own.
  const bare = !label && !description && !error;

  const affix = (
    <div
      className={cx("alk-input-affix", bare && !canCollapse && rootClassName)}
      style={bare && !canCollapse ? rootStyle : undefined}
      data-search={isSearch ? "" : undefined}
    >
      {lead != null ? (
        <span className="alk-input__lead" aria-hidden="true">
          {lead}
        </span>
      ) : null}
      <input
        ref={setRefs}
        id={id}
        type={inputType}
        value={value}
        required={required}
        onBlur={onFieldBlur}
        onKeyDown={onKeyDown}
        aria-label={ariaLabel}
        aria-invalid={error ? true : undefined}
        aria-describedby={cx(ariaDescribedBy, describedBy) || undefined}
        className={cx("alk-input", className)}
        data-size={size !== "lg" ? size : undefined}
        data-lead={lead != null ? "" : undefined}
        data-trail={trailSlots > 0 ? trailSlots : undefined}
        {...rest}
      />
      {trailing}
    </div>
  );

  // Collapsible search is a bare field (no label) that folds to a toggle while narrow.
  const field = canCollapse ? (
    <div
      className={cx("alk-input-collapse", bare && rootClassName)}
      style={bare ? rootStyle : undefined}
      data-narrow={collapsed || undefined}
      data-expanded={expanded || undefined}
    >
      {collapsed && !expanded ? (
        <button
          ref={toggleRef}
          type="button"
          className="alk-input-collapse__toggle"
          // The toggle's accessible name: the field's label, else the caller's aria-label (a bare
          // toolbar search names itself that way), else the generic fallback.
          aria-label={typeof label === "string" ? label : ariaLabel ?? "Search"}
          aria-expanded={false}
          onClick={() => {
            setExpanded(true);
            requestAnimationFrame(() => localRef.current?.focus());
          }}
        >
          <SearchIcon size={18} />
        </button>
      ) : null}
      {affix}
    </div>
  ) : (
    affix
  );

  return (
    <FieldShell
      htmlFor={id}
      label={label}
      labelAccessory={labelAccessory}
      description={description}
      descriptionAbove={descriptionAbove}
      descId={descId}
      error={error}
      errId={errId}
      required={required}
      requiredMark={requiredMark}
      className={bare ? undefined : rootClassName}
      style={bare ? undefined : rootStyle}
    >
      {field}
    </FieldShell>
  );
});
