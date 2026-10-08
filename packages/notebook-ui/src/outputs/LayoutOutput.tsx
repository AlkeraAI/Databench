// `application/vnd.alkera.layout+json`: stacks and callouts whose children
// are bundles, drawn through the same registry (`context.renderBundle`).

import type { CSSProperties } from "react";

import type { MimeBundle } from "../model/types";
import type { OutputRenderer, OutputRendererProps } from "./types";

export type LayoutAlign = "start" | "center" | "end" | "stretch";
export type CalloutKind = "info" | "warn" | "danger" | "success" | "neutral";

export type LayoutPayload =
  | { type: "hstack" | "vstack"; items: MimeBundle[]; gap?: number; align?: LayoutAlign }
  | { type: "callout"; kind: CalloutKind; body: MimeBundle };

const ALIGNS: readonly LayoutAlign[] = ["start", "center", "end", "stretch"];
const KINDS: readonly CalloutKind[] = ["info", "warn", "danger", "success", "neutral"];
/** Gaps above this many pixels are clamped. */
const MAX_GAP = 128;

function isBundle(value: unknown): value is MimeBundle {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The payload when it has a known shape, otherwise `null`. */
export function parseLayout(value: unknown): LayoutPayload | null {
  if (!isBundle(value)) return null;
  if (value.type === "hstack" || value.type === "vstack") {
    if (!Array.isArray(value.items) || !value.items.every(isBundle)) return null;
    const gap = typeof value.gap === "number" && Number.isFinite(value.gap) ? Math.min(Math.max(value.gap, 0), MAX_GAP) : undefined;
    const align = ALIGNS.includes(value.align as LayoutAlign) ? (value.align as LayoutAlign) : undefined;
    return { type: value.type, items: value.items, gap, align };
  }
  if (value.type === "callout") {
    if (!isBundle(value.body)) return null;
    const kind = KINDS.includes(value.kind as CalloutKind) ? (value.kind as CalloutKind) : "neutral";
    return { type: "callout", kind, body: value.body };
  }
  return null;
}

const CALLOUT_LABELS: Record<CalloutKind, string> = {
  info: "Note",
  warn: "Warning",
  danger: "Danger",
  success: "Success",
  neutral: "Note",
};

function LayoutOutput({ data, context }: OutputRendererProps) {
  const layout = parseLayout(data);
  if (!layout) return <p className="nb-output-note">This layout could not be read.</p>;
  if (layout.type === "callout") {
    return (
      <aside className={`nb-output-callout nb-output-callout--${layout.kind}`} aria-label={CALLOUT_LABELS[layout.kind]}>
        {context.renderBundle(layout.body, "body")}
      </aside>
    );
  }
  const style: CSSProperties = {};
  if (layout.gap !== undefined) style.gap = `${layout.gap}px`;
  if (layout.align) style.alignItems = layout.align === "start" || layout.align === "end" ? `flex-${layout.align}` : layout.align;
  return (
    <div className={`nb-output-stack nb-output-stack--${layout.type}`} style={style}>
      {layout.items.map((item, index) => (
        <div className="nb-output-stack-item" key={index}>
          {context.renderBundle(item, String(index))}
        </div>
      ))}
    </div>
  );
}

export const layoutRenderer: OutputRenderer = {
  id: "alkera.layout",
  mimes: ["application/vnd.alkera.layout+json"],
  rank: 0,
  place: "app",
  Component: LayoutOutput,
};
