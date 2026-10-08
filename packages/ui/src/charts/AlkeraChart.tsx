import { useEffect, useRef, useState, type CSSProperties, type ReactElement } from "react";

import { useColorSchemeChanges } from "../hooks/useColorScheme";
import { cx } from "../primitives/cx";
import type { ChartRows } from "./prepare";
import { readChartTokens, type ChartTokens } from "./theme";

// A chart in the Alkera chart profile, drawn by Vega.
//
// Pure presentation: the spec and its rows arrive as props, and the host owns
// where they came from (a saved result, a notebook output, an agent's reply).
// Vega itself loads on first use through a dynamic import of `vegaRuntime`, so
// a page that never shows a chart never downloads it.
//
// The theme is read from the live tokens of the surface the chart sits on, so
// the portal's light and dark schemes and a VS Code theme flow through without
// the host passing anything, and read again whenever the scheme changes;
// `tokens` overrides that for a host that knows better (a content frame handed
// its tokens by message).

export interface AlkeraChartProps {
  /** A Vega-Lite spec in the Alkera chart profile. */
  spec: unknown;
  /** The rows of the table a bound spec draws (a saved result's rows). */
  data?: ChartRows;
  /** Named tables for `data: {name}` references. */
  datasets?: Readonly<Record<string, ChartRows>>;
  tokens?: ChartTokens;
  renderer?: "svg" | "canvas";
  /** Height of a single view that names none. */
  height?: number;
  className?: string;
  /** The accessible name; defaults to the spec's description or title. */
  ariaLabel?: string;
  /** Told when the chart cannot be shown (refused, or failed to draw). */
  onError?: (error: Error) => void;
}

type Status = { kind: "loading" } | { kind: "ready" } | { kind: "error"; message: string };

/** What a reader is told when a chart cannot be shown. Never the cause's own
 *  text: that may carry the spec's values. */
const REFUSED = "This chart is blocked because it tries to load or run something.";
const FAILED = "This chart could not be drawn.";

let runtime: Promise<typeof import("./vegaRuntime")> | null = null;
/** The renderer, fetched once per page. */
export function loadChartRuntime(): Promise<typeof import("./vegaRuntime")> {
  runtime ??= import("./vegaRuntime");
  return runtime;
}

function accessibleName(spec: unknown, explicit: string | undefined): string {
  if (explicit) return explicit;
  if (typeof spec === "object" && spec !== null) {
    const { description, title } = spec as { description?: unknown; title?: unknown };
    if (typeof description === "string" && description) return description;
    if (typeof title === "string" && title) return title;
    if (typeof title === "object" && title !== null) {
      const text = (title as { text?: unknown }).text;
      if (typeof text === "string" && text) return text;
    }
  }
  return "Chart";
}

/** What a value holds, as text: two values with the same key draw the same
 *  chart. A value that cannot be written out keys on nothing stable, so it is
 *  never taken for another. */
function contentKey(value: unknown): string | null {
  try {
    return JSON.stringify(value) ?? "undefined";
  } catch {
    return null;
  }
}

/** `value`, or the earlier value when this one holds the same content.
 *
 *  A host re-renders with a chart's spec and rows rebuilt from its own state:
 *  a transcript parses a tool result again on every update it streams. Equal
 *  content in a new object must not tear the drawn chart down and draw it
 *  again, which empties its view for a frame and reads as a flicker. */
function useSameContent<T>(value: T): T {
  const held = useRef<{ key: string | null; value: T } | null>(null);
  const key = contentKey(value);
  if (held.current === null || key === null || held.current.key !== key) {
    held.current = { key, value };
  }
  return held.current.value;
}

export function AlkeraChart({
  spec: specProp,
  data: dataProp,
  datasets: datasetsProp,
  tokens: tokensProp,
  renderer = "svg",
  height = 240,
  className,
  ariaLabel,
  onError,
}: AlkeraChartProps): ReactElement {
  const spec = useSameContent(specProp);
  const data = useSameContent(dataProp);
  const datasets = useSameContent(datasetsProp);
  const tokens = useSameContent(tokensProp);
  const frame = useRef<HTMLDivElement>(null);
  const viewRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<Status>({ kind: "loading" });
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;
  const schemeChanges = useColorSchemeChanges();

  useEffect(() => {
    const host = viewRef.current;
    const outer = frame.current;
    if (!host || !outer) return;
    let cancelled = false;
    let teardown: (() => void) | undefined;
    setStatus({ kind: "loading" });
    void (async () => {
      try {
        const vega = await loadChartRuntime();
        if (cancelled) return;
        const chart = await vega.mountChart(host, spec, {
          tokens: tokens ?? readChartTokens(outer),
          tables: { data, datasets },
          renderer,
          width: outer.clientWidth,
          height,
        });
        if (cancelled) {
          chart.finalize();
          return;
        }
        let observer: ResizeObserver | undefined;
        if (chart.fills && typeof ResizeObserver !== "undefined") {
          let last = outer.clientWidth;
          observer = new ResizeObserver(() => {
            const width = outer.clientWidth;
            if (width === last || width <= 0) return;
            last = width;
            void chart.resize(width);
          });
          observer.observe(outer);
        }
        teardown = () => {
          observer?.disconnect();
          chart.finalize();
          host.replaceChildren();
        };
        setStatus({ kind: "ready" });
      } catch (error) {
        if (cancelled) return;
        const vega = await loadChartRuntime().catch(() => null);
        const refused = vega !== null && error instanceof vega.ChartRefusedError;
        setStatus({ kind: "error", message: refused ? REFUSED : FAILED });
        onErrorRef.current?.(error instanceof Error ? error : new Error(String(error)));
      }
    })();
    return () => {
      cancelled = true;
      teardown?.();
    };
  }, [spec, data, datasets, tokens, renderer, height, schemeChanges]);

  return (
    <div
      ref={frame}
      className={cx("alk-chart", className)}
      role="figure"
      aria-label={accessibleName(spec, ariaLabel)}
      aria-busy={status.kind === "loading"}
      style={{ "--alk-chart-height": `${height}px` } as CSSProperties}
    >
      <div ref={viewRef} className="alk-chart__view" hidden={status.kind === "error"} />
      {status.kind === "loading" ? (
        <div className="alk-chart__status" data-kind="loading">
          Loading chart…
        </div>
      ) : null}
      {status.kind === "error" ? (
        <div className="alk-chart__status" data-kind="error" role="alert">
          {status.message}
        </div>
      ) : null}
    </div>
  );
}
