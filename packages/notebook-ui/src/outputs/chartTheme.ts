// The Alkera chart theme as a Vega config, per scheme. The token values and
// the mapping mirror the chart profile's server-side theme, so a chart drawn
// here matches one exported there.

import type { OutputTheme } from "./types";

interface ChartTokens {
  background: string;
  text: string;
  muted: string;
  grid: string;
  domain: string;
  font: string;
  category: string[];
  ramp: string[];
  diverging: string[];
}

const FONT = '"Hanken Grotesk", ui-sans-serif, system-ui, sans-serif';

export const CHART_TOKENS: Record<OutputTheme, ChartTokens> = {
  light: {
    background: "#ffffff",
    text: "#1b1a17",
    muted: "#5f5b53",
    grid: "rgba(60, 52, 40, 0.14)",
    domain: "rgba(60, 52, 40, 0.26)",
    font: FONT,
    category: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
    ramp: ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"],
    diverging: ["#184f95", "#3987e5", "#9ec5f4", "#f0efec", "#f2aca9", "#e34948", "#a3272a"],
  },
  dark: {
    background: "#25231e",
    text: "#ece9e2",
    muted: "#a39d92",
    grid: "rgba(231, 226, 216, 0.11)",
    domain: "rgba(231, 226, 216, 0.2)",
    font: FONT,
    category: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
    ramp: ["#0d366b", "#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4", "#cde2fb"],
    diverging: ["#9ec5f4", "#3987e5", "#1c5cab", "#383835", "#a3272a", "#e66767", "#f2aca9"],
  },
};

export function chartConfig(theme: OutputTheme): Record<string, unknown> {
  const tokens = CHART_TOKENS[theme];
  const { font, text, muted } = tokens;
  const label = { labelColor: muted, labelFont: font, labelFontSize: 11 };
  const heading = { titleColor: text, titleFont: font, titleFontSize: 11, titleFontWeight: 600 };
  return {
    background: tokens.background,
    font,
    padding: 8,
    view: { stroke: null },
    title: {
      color: text,
      subtitleColor: muted,
      font,
      subtitleFont: font,
      fontSize: 14,
      fontWeight: 600,
      anchor: "start",
      offset: 8,
    },
    axis: {
      ...label,
      ...heading,
      domainColor: tokens.domain,
      tickColor: tokens.domain,
      gridColor: tokens.grid,
      titlePadding: 8,
      labelPadding: 4,
    },
    axisBand: { grid: false },
    legend: { ...label, ...heading, symbolType: "circle", symbolSize: 64 },
    header: { ...label, ...heading },
    range: {
      category: [...tokens.category],
      ordinal: tokens.ramp.slice(1),
      ramp: [...tokens.ramp],
      heatmap: [...tokens.ramp],
      diverging: [...tokens.diverging],
    },
    mark: { color: tokens.category[0] },
    line: { strokeWidth: 2 },
    trail: { size: 2 },
    point: { size: 64, filled: true },
    circle: { size: 64 },
    square: { size: 64 },
    bar: { binSpacing: 2, cornerRadiusEnd: 2 },
    rect: { binSpacing: 2 },
    arc: { stroke: tokens.background, strokeWidth: 2 },
    area: { opacity: 0.85 },
    rule: { color: muted },
    text: { color: text, font, fontSize: 11 },
    tick: { thickness: 2 },
  };
}
