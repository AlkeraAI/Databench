// Kernel text with its ANSI styling, drawn as React text spans.

import { useMemo, type CSSProperties } from "react";

import { parseAnsi, xtermColor, type AnsiColor, type AnsiSegment } from "./ansi";

function colorValue(color: AnsiColor): string {
  if (color.kind === "rgb") return `rgb(${color.r}, ${color.g}, ${color.b})`;
  if (color.index < 16) return `var(--nb-ansi-${color.index})`;
  return xtermColor(color.index);
}

function segmentProps(segment: AnsiSegment): { className?: string; style?: CSSProperties } {
  const { style } = segment;
  const classes: string[] = [];
  if (style.bold) classes.push("nb-ansi-bold");
  if (style.dim) classes.push("nb-ansi-dim");
  if (style.italic) classes.push("nb-ansi-italic");
  if (style.underline) classes.push("nb-ansi-underline");
  if (style.strike) classes.push("nb-ansi-strike");
  let fg = style.fg ? colorValue(style.fg) : undefined;
  let bg = style.bg ? colorValue(style.bg) : undefined;
  if (style.inverse) {
    [fg, bg] = [bg ?? "var(--nb-output-bg, var(--nb-bg))", fg ?? "var(--nb-text)"];
  }
  const css: CSSProperties = {};
  if (fg) css.color = fg;
  if (bg) css.backgroundColor = bg;
  return {
    className: classes.length ? classes.join(" ") : undefined,
    style: fg || bg ? css : undefined,
  };
}

export function AnsiText({ text }: { text: string }) {
  const segments = useMemo(() => parseAnsi(text), [text]);
  return (
    <>
      {segments.map((segment, i) => {
        const { className, style } = segmentProps(segment);
        if (!className && !style) return <span key={i}>{segment.text}</span>;
        return (
          <span key={i} className={className} style={style}>
            {segment.text}
          </span>
        );
      })}
    </>
  );
}

/** A MIME value as text: Jupyter writes long text as a list of lines. */
export function toText(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value) && value.every((line) => typeof line === "string")) return value.join("");
  if (value === null || value === undefined) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
