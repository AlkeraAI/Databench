// The connector brand mark, shared by the lineage survey and the webview's Plugins &
// Connections page. The artwork is vendor SVG from assets/icons/connectors, inlined at build
// time and never recolored. When the vendor supplies a distinct dark drawing, scheme tokens
// select between variants. One drawing otherwise serves both schemes at any nesting depth.
// An id with no registered mark falls back to a generic plug drawn in the surface's
// --con-<id> pigment when defined, so an unbranded connector still reads as a connectable
// source.
//
// Deliberately NOT merged with brand/ProviderLogo. That registry is LLM vendors and
// sign-in providers, a separate concern with separate ids.

import { CONNECTOR_BRANDS, type ConnectorBrand } from "../../assets/icons/connectors";
import { cx } from "../../primitives/cx";

import "./connectormark.css";

/** The registered brand for a connector/plugin id, or undefined when it takes the plug
 *  fallback. */
export function connectorBrand(id: string): ConnectorBrand | undefined {
  return CONNECTOR_BRANDS[id];
}

export interface ConnectorMarkProps {
  /** The connector/plugin id (the CLI plugin catalog vocabulary: `snowflake`, `dbt`, ...). */
  id: string;
  /** Box edge in px; the artwork fills the box. */
  size?: number;
  className?: string;
}

/**
 * A connector's brand mark. Decorative (aria-hidden), so pair it with the connector's name
 * wherever the identity matters. `data-connector` names the id and `data-fallback` marks the
 * plug, so a test can tell a brand hit from the fallback without pinning path data.
 */
export function ConnectorMark({ id, size = 16, className }: ConnectorMarkProps) {
  const brand = CONNECTOR_BRANDS[id];
  if (!brand) {
    // The plug marks a connectable source with no registered brand, drawn in the flask's
    // single-weight line.
    return (
      <svg
        data-connector={id}
        data-fallback=""
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke={`var(--con-${id}, currentColor)`}
        strokeWidth={1.7}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        focusable="false"
        // The shared class on the element itself: the fallback can't be wrapped (its data hooks
        // are what a caller queries), and without it the mark is the one flex child that shrinks
        // when a row runs out of room.
        className={cx("alk-connmark", className)}
      >
        <path d="M9 3.5V9M15 3.5V9" />
        <path d="M6.5 9h11v3.5a5.5 5.5 0 01-11 0z" />
        <path d="M12 18v2.5" />
      </svg>
    );
  }
  return (
    <span
      data-connector={id}
      className={cx("alk-connmark", className)}
      style={{ width: size, height: size }}
      aria-hidden="true"
    >
      {brand.Dark === undefined ? (
        <brand.Light focusable="false" />
      ) : (
        <>
          <brand.Light className="alk-connmark__light" focusable="false" />
          <brand.Dark className="alk-connmark__dark" focusable="false" />
        </>
      )}
    </span>
  );
}
