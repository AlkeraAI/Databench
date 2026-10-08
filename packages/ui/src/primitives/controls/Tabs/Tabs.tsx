import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

import { cx } from "../../cx";

// Tabs — the underline section nav: a row of views over one content region, with a sliding
// marker under the active tab. This is page-section navigation (Running / Scheduled / Recent),
// not the boxed SegmentedControl toolbar control — tabs have natural widths, an icon slot, and
// a count reading, and they sit on a hairline rule instead of inside a bordered box.
//
// Tablist semantics with automatic activation: Left/Right/Home/End move focus AND select
// (WAI-ARIA tabs pattern), roving tabindex keeps one tab stop. The marker is measured from the
// active tab's real box and moves on transform only (translateX + scaleX over a 1px base), so
// the slide rides the compositor.

export interface TabItem {
  key: string;
  label: string;
  /** A leading glyph — tertiary ink at rest, brand ink on the active tab. */
  icon?: ReactNode;
  /** A trailing mono count reading beside the label. */
  count?: number;
}

export interface TabsProps {
  items: readonly TabItem[];
  value: string;
  onChange: (key: string) => void;
  /** Accessible name for the tablist (e.g. "Job lifecycle"). */
  label: string;
  className?: string;
  style?: CSSProperties;
}

export function Tabs({ items, value, onChange, label, className, style }: TabsProps) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [marker, setMarker] = useState({ left: 0, width: 0 });
  const active = items.findIndex((i) => i.key === value);

  useLayoutEffect(() => {
    const measure = (): void => {
      const el = refs.current[active];
      setMarker(el ? { left: el.offsetLeft, width: el.offsetWidth } : { left: 0, width: 0 });
    };
    measure();
    // Natural-width tabs move when their labels reflow (font load, container resize).
    const ro = new ResizeObserver(measure);
    if (rootRef.current) ro.observe(rootRef.current);
    return () => ro.disconnect();
  }, [active, items]);

  const onKeyDown = (event: React.KeyboardEvent): void => {
    const last = items.length - 1;
    const next =
      event.key === "ArrowRight" ? (active >= last ? 0 : active + 1)
      : event.key === "ArrowLeft" ? (active <= 0 ? last : active - 1)
      : event.key === "Home" ? 0
      : event.key === "End" ? last
      : null;
    if (next === null || items[next] === undefined) return;
    event.preventDefault();
    onChange(items[next].key);
    refs.current[next]?.focus();
  };

  return (
    <div ref={rootRef} className={cx("alk-tabs", className)} role="tablist" aria-label={label} style={style} onKeyDown={onKeyDown}>
      {items.map((item, i) => (
        <button
          key={item.key}
          ref={(el) => {
            refs.current[i] = el;
          }}
          type="button"
          role="tab"
          aria-selected={item.key === value}
          tabIndex={item.key === value || (active < 0 && i === 0) ? 0 : -1}
          data-on={item.key === value || undefined}
          onClick={() => onChange(item.key)}
        >
          {item.icon != null ? (
            <span className="alk-tabs__icon" aria-hidden>
              {item.icon}
            </span>
          ) : null}
          {item.label}
          {item.count != null ? <span className="alk-tabs__count">{item.count}</span> : null}
        </button>
      ))}
      {marker.width > 0 ? (
        <span
          className="alk-tabs__marker"
          style={{ transform: `translateX(${marker.left}px) scaleX(${marker.width})` }}
          aria-hidden
        />
      ) : null}
    </div>
  );
}
