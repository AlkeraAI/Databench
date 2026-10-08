import { useRef } from "react";
import type { CSSProperties, ElementType, FocusEvent, HTMLAttributes, PointerEvent, ReactNode } from "react";

import { isOverflowing } from "../../../hooks";
import { Tooltip } from "../../overlays/Tooltip";
import { cx } from "../../cx";

/** The typographic voice — a shared role from the type scale (typography.css). `variant` reuses the
 *  role class so the scale stays single-source; a page never re-spells family / size / weight. */
export type TextVariant =
  | "h1"
  | "h2"
  | "h3"
  | "body"
  | "eyebrow"
  | "meta"
  | "caption"
  | "strong"
  | "name"
  | "num"
  | "tnum"
  | "code";

/** Colour override, orthogonal to the variant — a demoted (muted / faint) or status reading. */
export type TextTone = "muted" | "faint" | "danger" | "warning" | "success";

/** When the reading annotates itself with the shared Tooltip. `truncate` is measured on the live
 *  element, never inferred from the props. */
export type TextTooltip = "none" | "truncate" | "always";

export interface TextProps extends HTMLAttributes<HTMLElement> {
  /** The typographic voice. Omit to inherit the ambient one (a tone- or clamp-only wrapper). */
  variant?: TextVariant;
  /** Colour override, orthogonal to `variant` — wins over the variant's own ink. Pair a status tone
   *  with an icon / word at the call site (never colour alone). */
  tone?: TextTone;
  /** Clamp to N lines with a trailing ellipsis (multi-line overflow). */
  clamp?: number;
  /** Truncate to a single line with a trailing ellipsis. */
  truncate?: boolean;
  /** Show the whole reading in a Tooltip: never (`none`, the default), only while the text is
   *  measurably cut off (`truncate`), or on every hover / focus (`always`). Anything but `none`
   *  makes the tip the one annotation — a `title` becomes its label instead of a second native tip. */
  tooltip?: TextTooltip;
  /** The tip's reading. Defaults to `title`, then to the text's own string content — pass it when
   *  the call site holds a fuller string than it rendered (a shortened path). */
  tooltipLabel?: ReactNode;
  /** The element to render — a heading, a table cell, a paragraph. Default `span`. */
  as?: ElementType;
  children?: ReactNode;
}

/** A long reading (a path, a sentence) wraps inside this cap rather than running off the viewport. */
const TIP_MAX_WIDTH = 360;

type ElementProps = Omit<TextProps, "tooltip" | "tooltipLabel">;
type Extra = HTMLAttributes<HTMLElement> & { ref?: (el: HTMLElement | null) => void };

/** The element every mode renders. A plain function, not a component, so the default path pays for
 *  no extra fiber. `extra` already carries the caller's own handlers, chained. */
function textNode({ variant, tone, clamp, truncate, as, className, style, children, ...rest }: ElementProps, extra?: Extra): ReactNode {
  const Component = (as ?? "span") as ElementType;
  return (
    <Component
      className={cx("alk-text", variant && `alk-${variant}`, truncate && "alk-truncate", clamp != null && "alk-clamp", className)}
      data-tone={tone}
      style={clamp != null ? ({ ...style, "--alk-clamp": clamp } as CSSProperties) : style}
      {...rest}
      {...extra}
    >
      {children}
    </Component>
  );
}

function chain<E>(...fns: (((e: E) => void) | undefined)[]): ((e: E) => void) | undefined {
  const live = fns.filter((fn): fn is (e: E) => void => fn != null);
  if (live.length === 0) return undefined;
  return (e: E) => {
    for (const fn of live) fn(e);
  };
}

/** `aria-describedby` is an id list, so a caller's description survives beside the tip's. */
function mergeIds(...ids: (string | undefined)[]): string | undefined {
  const live = ids.filter((id) => id != null && id !== "");
  return live.length > 0 ? live.join(" ") : undefined;
}

/** The rendered string, when the children are text. Mixed markup returns nothing rather than a
 *  half reading — that call site passes `tooltipLabel`. */
function textOf(node: ReactNode): string | null {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) {
    const parts = node.map(textOf);
    return parts.every((part) => part != null) ? parts.join("") : null;
  }
  return null;
}

function AnnotatedText({ mode, label, ...props }: ElementProps & { mode: "truncate" | "always"; label?: ReactNode }) {
  const nodeRef = useRef<HTMLElement | null>(null);
  const derived = props.title ?? textOf(props.children);
  const tip = label ?? (derived != null && derived.trim() !== "" ? derived : null);
  const has = tip != null && tip !== "";
  // Measured when the pointer or focus arrives, not on every commit: see isOverflowing.
  const shouldOpen = (): boolean => has && (mode === "always" || isOverflowing(nodeRef.current));

  if (!has) return textNode(props, { ref: (el) => void (nodeRef.current = el) });

  // The native title is dropped: it is the tip's label now, and two tips on one hover is a bug.
  const { title: _title, ...rest } = props;
  return (
    <Tooltip label={tip} wrap maxWidth={TIP_MAX_WIDTH}>
      {(t) =>
        textNode(rest, {
          ref: (el) => {
            nodeRef.current = el;
            t.ref(el);
          },
          "aria-describedby": mergeIds(rest["aria-describedby"], t["aria-describedby"]),
          onPointerEnter: chain<PointerEvent<HTMLElement>>((e) => shouldOpen() && t.onPointerEnter(e), rest.onPointerEnter),
          onPointerMove: chain<PointerEvent<HTMLElement>>(t.onPointerMove, rest.onPointerMove),
          onPointerLeave: chain<PointerEvent<HTMLElement>>(t.onPointerLeave, rest.onPointerLeave),
          onFocus: chain<FocusEvent<HTMLElement>>(() => shouldOpen() && t.onFocus(), rest.onFocus),
          onBlur: chain<FocusEvent<HTMLElement>>(t.onBlur, rest.onBlur),
        })
      }
    </Tooltip>
  );
}

/**
 * Text — the one general typographic component. `variant` selects a shared voice (an `alk-*` role from
 * the type scale); `tone`, `clamp`, and `truncate` are orthogonal props that layer on top, so the same
 * component covers a heading, a muted caption, a clamped synopsis, or a truncated mono reading without
 * a page ever hand-composing role classes. `as` renders any element (default `span`). Extra DOM props
 * (title, aria-*, onClick, id, …) forward to the element.
 *
 * `tone` is applied as a `data-tone` attribute whose CSS rule (text.css) has higher specificity than a
 * single role class, so a status/demoted colour reliably beats the variant's bundled ink regardless of
 * stylesheet order. `clamp` drives the shared `--alk-clamp` custom property.
 *
 * `tooltip` hands the cut-off reading back to the user through the shared Tooltip. `truncate` mode
 * measures the live element (a widened column drops the tip on its own) rather than trusting the
 * `truncate` / `clamp` props, which say a reading MAY be cut, never that it is. The default `none`
 * renders exactly the element above — no state, no observer, no wrapper — since most of the thousands
 * of readings on a page never need a tip.
 *
 * A span is not focusable, so the tip is pointer-only until the call site makes the element a tab stop
 * (`tabIndex={0}`, or an `as` that focuses itself); the tip's focus handlers are already wired for it.
 */
export function Text({ tooltip, tooltipLabel, ...props }: TextProps) {
  if (tooltip == null || tooltip === "none") return textNode(props);
  return <AnnotatedText mode={tooltip} label={tooltipLabel} {...props} />;
}
