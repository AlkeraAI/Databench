// The Alkera chart theme: design tokens mapped onto a Vega config.
//
// One mapping, two implementations held together by a committed fixture:
// `chartConfig` here (the renderer, reading the live tokens of whatever surface
// it sits on) and `alkera_core.charts.theme.chart_config` (server-side export,
// where there is no stylesheet). `packages/api-core/tests/fixtures/charts/theme`
// is the Python output; `theme.test.ts` feeds the same tokens through this
// function and must produce the same object.
//
// Nothing here imports Vega: the config is plain data, so the theme costs the
// initial bundle nothing.

/** The token roles a chart reads. */
export interface ChartTokens {
  background: string;
  text: string;
  muted: string;
  grid: string;
  domain: string;
  font: string;
  /** Eight categorical slots, assigned in order, never cycled. */
  category: readonly string[];
  /** A one-hue sequential ramp, near-surface first. */
  ramp: readonly string[];
  /** Two arms around a neutral midpoint. */
  diverging: readonly string[];
}

export type ChartScheme = "light" | "dark";

const FONT = '"Hanken Grotesk", ui-sans-serif, system-ui, sans-serif';

/** The validated defaults per scheme, identical to `CHART_TOKENS` in
 *  `alkera_core/charts/theme.py` (the parity test pins it). */
export const DEFAULT_CHART_TOKENS: Readonly<Record<ChartScheme, ChartTokens>> = {
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

export type ChartConfig = Record<string, unknown>;

/** The Vega config for one scheme's tokens: 2px lines, 8px markers, a 2px
 *  surface gap between fills, rounded bar ends, recessive grid and axes, and
 *  text in text tokens (never a series color). */
export function chartConfig(tokens: ChartTokens): ChartConfig {
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

/** The CSS custom property behind each single-color role. */
const ROLE_VARS: ReadonlyArray<[keyof ChartTokens, string]> = [
  ["background", "--alkCardBg"],
  ["text", "--alkPrimaryText"],
  ["muted", "--alkSecondaryText"],
  ["grid", "--alkBorder"],
  ["domain", "--alkBorderStrong"],
];

const SERIES_VARS = Array.from({ length: 8 }, (_, i) => `--alkChartSeries${i + 1}`);

/** The scheme the element sits in: the `color-scheme` its tokens declare. A surface that
 *  declares only one is that one; one that declares both (`light dark`) follows the reader's
 *  OS; one that declares neither (`normal`) is a plain page, which browsers paint light. */
export function schemeOf(el: Element): ChartScheme {
  const declared = getComputedStyle(el).colorScheme ?? "";
  const light = /\blight\b/.test(declared);
  const dark = /\bdark\b/.test(declared);
  if (dark && !light) return "dark";
  if (light && !dark) return "light";
  if (light && dark) return prefersDark() ? "dark" : "light";
  return "light";
}

function prefersDark(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

/** A color no token sheet uses, standing in for "this token is not defined". */
const SENTINEL = "rgb(1, 2, 3)";

/** A value that is a resolved color, not an unresolved `var()` or nothing. */
const resolved = (value: string): boolean => value !== "" && !value.includes("var(");

/**
 * The tokens of the surface `el` sits on, resolved through a probe element so
 * a `color-mix()`, a relative color or a VS Code variable arrives as the
 * color the browser computed. A role the stylesheet does not resolve keeps the
 * scheme's default, so a host without the token sheet still gets a whole theme.
 * The background is transparent: a chart sits on whatever card holds it.
 */
export function readChartTokens(el: Element): ChartTokens {
  const scheme = schemeOf(el);
  const base = DEFAULT_CHART_TOKENS[scheme];
  const doc = el.ownerDocument;
  const probe = doc.createElement("span");
  probe.style.display = "none";
  el.appendChild(probe);
  // An undefined custom property would make the probe inherit its parent's
  // text color, which reads like a resolved token; the sentinel fallback tells
  // the two apart.
  const color = (variable: string): string | null => {
    probe.style.color = "";
    probe.style.color = `var(${variable}, ${SENTINEL})`;
    if (probe.style.color === "") return null;
    const value = getComputedStyle(probe).color.replace(/\s+/g, "");
    return resolved(value) && value !== SENTINEL.replace(/\s+/g, "") ? getComputedStyle(probe).color : null;
  };
  try {
    const tokens: ChartTokens = { ...base, background: "transparent" };
    for (const [role, variable] of ROLE_VARS) {
      if (role === "background") continue;
      const value = color(variable);
      if (value) (tokens as unknown as Record<string, string>)[role] = value;
    }
    const series = SERIES_VARS.map(color);
    if (series.every((value): value is string => value !== null)) tokens.category = series;
    const font = getComputedStyle(el).getPropertyValue("--alkFontUi").trim();
    if (resolved(font)) tokens.font = font;
    return tokens;
  } finally {
    probe.remove();
  }
}
