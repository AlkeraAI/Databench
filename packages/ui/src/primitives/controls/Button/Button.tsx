import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";

import { cx } from "../../cx";
import { Spinner } from "../../icons";
import type { ControlSize } from "../../sizes";

/** WHAT the button means — the colour/intent. Orthogonal to `fill` (HOW it's drawn). */
export type ButtonVariant = "primary" | "secondary" | "destructive";
/** HOW the button is drawn — the weight of the treatment. Orthogonal to `variant` (the colour). */
export type ButtonFill = "filled" | "outline" | "ghost";
export type ButtonSize = ControlSize;

interface ButtonBase extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Intent / colour family — `primary` (brand, default), `secondary` (neutral), `destructive`
   *  (danger). Pair with `fill` for the weight: e.g. a quiet danger row action is
   *  `variant="destructive" fill="ghost"`; a brand-bordered button is `variant="primary"
   *  fill="outline"`. */
  variant?: ButtonVariant;
  /** Treatment weight — `filled` (solid, the default), `outline` (bordered on the surface), `ghost`
   *  (transparent until touched). Orthogonal to `variant`, so every colour gets every weight. */
  fill?: ButtonFill;
  /** Control height — `sm` 28 / `md` 32 / `lg` 38 (default). Every control shares these tokens, so a
   *  button lines up with an input / select / segmented control of the SAME size in a row, and a
   *  smaller button (e.g. `sm`) nests inside a full-size bordered field with even padding. */
  size?: ButtonSize;
  /** Content set before the label — typically an icon. Ignored when `iconOnly`. */
  leftSection?: ReactNode;
  /** Content set after the label — typically an icon. Ignored when `iconOnly`. */
  rightSection?: ReactNode;
  fullWidth?: boolean;
  /** Shows a leading spinner (replacing `leftSection`), hides `rightSection`, keeps
   *  the label, disables the button, and marks it `aria-busy`. */
  loading?: boolean;
}

/**
 * `iconOnly` makes the button a square glyph target (width = the size's control height, no label
 * padding) — the single primitive behind every icon button (close, kebab, copy, toolbar action).
 * An icon-only control carries no text, so a screen reader has nothing to announce: `aria-label`
 * is REQUIRED at the type level when `iconOnly` is set. A toggle passes `aria-pressed` (a ghost
 * gets the active tint). A text button needs no `aria-label`.
 *
 * A glyph is also unreadable to someone who CAN see it and does not know the icon, so an icon-only
 * button annotates itself: its `aria-label` becomes a native `title` unless the caller gave a
 * `title` of its own or wrapped it in a `Tooltip` (which marks the trigger `data-tip`). One
 * fallback here is what keeps every icon button in the product annotated, including ones written
 * after this line.
 */
export type ButtonProps = ButtonBase & ({ iconOnly: true; "aria-label": string } | { iconOnly?: false });

/** The base action. `variant` carries the colour, `fill` the weight, `size` the height, `iconOnly`
 *  the square glyph form. The label (or the single glyph, when `iconOnly`) is the one slot. */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "primary",
    fill = "filled",
    size = "lg",
    iconOnly = false,
    leftSection,
    rightSection,
    fullWidth,
    loading,
    disabled,
    className,
    type = "button",
    children,
    ...rest
  },
  ref,
) {
  const annotated = "title" in rest || "data-tip" in rest;
  const tip =
    iconOnly && !annotated && typeof rest["aria-label"] === "string" ? rest["aria-label"] : undefined;
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      title={tip}
      className={cx("alk-btn", className)}
      data-variant={variant}
      data-fill={fill}
      // lg is the default, carried by the base rule's token fallback — only sm/md emit an attribute.
      data-size={size !== "lg" ? size : undefined}
      data-icon={iconOnly ? "" : undefined}
      data-full={fullWidth ? "" : undefined}
      data-loading={loading ? "" : undefined}
      {...rest}
    >
      {iconOnly ? (
        // The single glyph is the whole content; no label/section wrapping, no padding.
        loading ? <Spinner size={15} /> : children
      ) : (
        <>
          {loading ? (
            <span className="alk-btn__icon">
              <Spinner size={15} />
            </span>
          ) : leftSection ? (
            <span className="alk-btn__icon">{leftSection}</span>
          ) : null}
          {children ? <span className="alk-btn__label">{children}</span> : null}
          {!loading && rightSection ? <span className="alk-btn__icon">{rightSection}</span> : null}
        </>
      )}
    </button>
  );
});
