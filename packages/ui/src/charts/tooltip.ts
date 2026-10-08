// The chart tooltip: built from DOM nodes and `textContent` only.
//
// Tooltip values are the rows a chart draws, often an agent's stored output,
// so nothing here ever parses a value as markup: no `innerHTML`, no template
// strings into the DOM. Styled inline from the chart's tokens, so it reads the
// same in the portal, the VS Code webview and a content frame with no
// stylesheet of ours.

import type { ChartTokens } from "./theme";

const MAX_ROWS = 24;
const MAX_CHARS = 200;

export interface TooltipHandle {
  /** Vega's tooltip handler signature: `(handler, event, item, value)`. */
  show: (handler: unknown, event: MouseEvent | undefined, item: unknown, value: unknown) => void;
  destroy: () => void;
}

const clip = (text: string): string => (text.length > MAX_CHARS ? `${text.slice(0, MAX_CHARS - 1)}…` : text);

function display(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return Number.isFinite(value) ? value.toLocaleString() : String(value);
  if (value instanceof Date) return value.toISOString();
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** Fill `root` with one tooltip value: a label/value table for an object, a
 *  single line for anything else. */
export function fillTooltip(root: HTMLElement, value: unknown, tokens: ChartTokens): void {
  root.replaceChildren();
  const doc = root.ownerDocument;
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const table = doc.createElement("table");
    table.style.borderCollapse = "collapse";
    for (const [key, cell] of Object.entries(value as Record<string, unknown>).slice(0, MAX_ROWS)) {
      const row = doc.createElement("tr");
      const label = doc.createElement("td");
      label.textContent = clip(key);
      label.style.color = tokens.muted;
      label.style.paddingRight = "12px";
      label.style.verticalAlign = "top";
      const data = doc.createElement("td");
      data.textContent = clip(display(cell));
      data.style.color = tokens.text;
      data.style.fontVariantNumeric = "tabular-nums";
      row.append(label, data);
      table.append(row);
    }
    root.append(table);
    return;
  }
  root.textContent = clip(display(value));
}

/** A tooltip living in `host` (the runtime passes the document body: a frame
 *  or a webview is its own document, so the tooltip stays inside its box),
 *  fixed at the pointer. */
export function createTooltip(host: HTMLElement, tokens: ChartTokens): TooltipHandle {
  const doc = host.ownerDocument;
  const el = doc.createElement("div");
  el.setAttribute("role", "tooltip");
  el.className = "alk-chart-tooltip";
  Object.assign(el.style, {
    position: "fixed",
    zIndex: "1000",
    pointerEvents: "none",
    display: "none",
    maxWidth: "320px",
    padding: "6px 8px",
    borderRadius: "6px",
    font: `12px ${tokens.font}`,
    lineHeight: "1.4",
    color: tokens.text,
    background: tokens.background === "transparent" ? "Canvas" : tokens.background,
    border: `1px solid ${tokens.domain}`,
    boxShadow: "0 4px 16px rgba(0, 0, 0, 0.18)",
  } satisfies Partial<CSSStyleDeclaration>);
  host.append(el);
  return {
    show: (_handler, event, _item, value) => {
      if (value === null || value === undefined || value === "" || !event) {
        el.style.display = "none";
        return;
      }
      fillTooltip(el, value, tokens);
      el.style.display = "block";
      const view = doc.defaultView;
      const right = view ? view.innerWidth : Infinity;
      const left = event.clientX + 12 + el.offsetWidth > right ? event.clientX - 12 - el.offsetWidth : event.clientX + 12;
      el.style.left = `${Math.max(0, left)}px`;
      el.style.top = `${event.clientY + 12}px`;
    },
    destroy: () => el.remove(),
  };
}
