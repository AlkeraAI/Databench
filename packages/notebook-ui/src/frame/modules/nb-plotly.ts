// Plotly outputs, drawn with Plotly's strict bundle (no code generation, so it
// runs under the frame's policy). The configuration is ours, not the
// figure's: no link out, no cloud, no logo, and no image download, which
// builds a `blob:` URL the frame refuses.

import Plotly from "plotly.js-strict-dist-min";

import { CHART_TOKENS } from "../../outputs/chartTheme";
import type { OutputTheme } from "../../outputs/types";
import type { FrameModule } from "../protocol";
import { outputJson, registerFrameModule } from "./register";

export const PLOTLY_CONFIG = {
  responsive: true,
  displaylogo: false,
  showLink: false,
  showSendToCloud: false,
  showEditInChartStudio: false,
  modeBarButtonsToRemove: ["toImage", "sendDataToCloud"],
  // Geo traces fetch their maps; nothing may be fetched from here.
  topojsonURL: "",
} as const;

const CLEAR = "rgba(0,0,0,0)";

function at(layout: Record<string, unknown>, path: string): unknown {
  let value: unknown = layout[path];
  if (value !== undefined) return value;
  value = layout;
  for (const key of path.split(".")) {
    if (typeof value !== "object" || value === null) return undefined;
    value = (value as Record<string, unknown>)[key];
  }
  return value;
}

/** The layout changes that draw a figure in `theme`: the Alkera chart
 *  theme's ink, grid and series colours on the frame's own ground. A key the
 *  figure's own layout sets is left out, so a figure that names its colours
 *  keeps them in either theme. (A template's defaults are not the author
 *  naming a colour, and are themed.) */
export function themeLayout(theme: OutputTheme, layout: Record<string, unknown> = {}): Record<string, unknown> {
  const tokens = CHART_TOKENS[theme];
  const themed: Record<string, unknown> = {
    paper_bgcolor: CLEAR,
    plot_bgcolor: CLEAR,
    "font.color": tokens.text,
    colorway: [...tokens.category],
    "xaxis.gridcolor": tokens.grid,
    "xaxis.linecolor": tokens.domain,
    "xaxis.zerolinecolor": tokens.domain,
    "yaxis.gridcolor": tokens.grid,
    "yaxis.linecolor": tokens.domain,
    "yaxis.zerolinecolor": tokens.domain,
  };
  return Object.fromEntries(Object.entries(themed).filter(([path]) => at(layout, path) === undefined));
}

export const plotlyModule: FrameModule = {
  mimes: ["application/vnd.plotly.v1+json"],
  async render(init, api) {
    const figure = outputJson(init.data);
    const data = Array.isArray(figure.data) ? figure.data : [];
    const layout = (typeof figure.layout === "object" && figure.layout !== null ? figure.layout : {}) as Record<string, unknown>;
    const chart = document.createElement("div");
    api.root.replaceChildren(chart);
    await Plotly.newPlot(chart, data, { ...layout, autosize: true }, PLOTLY_CONFIG);
    await Plotly.relayout(chart, themeLayout(api.theme(), layout));
    api.onMessage((message) => {
      if (message.type === "theme") void Plotly.relayout(chart, themeLayout(message.theme, layout));
    });
  },
};

registerFrameModule("nb-plotly", plotlyModule);
