// One mapping in two languages: the server's export theme and this renderer's
// must agree, and the defaults must be the token sheet's own colors.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { CORPUS } from "./corpus";
import { chartConfig, DEFAULT_CHART_TOKENS, readChartTokens, schemeOf, type ChartScheme } from "./theme";

const fixture = (scheme: ChartScheme) =>
  JSON.parse(readFileSync(resolve(CORPUS, "theme", `${scheme}.json`), "utf8")) as {
    tokens: unknown;
    config: unknown;
  };

const tokensCss = readFileSync(resolve(__dirname, "../theme/tokens.css"), "utf8");
/** The declarations in the block that opens with `selector`. */
function block(selector: string): string {
  const start = tokensCss.indexOf(selector);
  return tokensCss.slice(start, tokensCss.indexOf("\n}", start));
}
const declared = (css: string, name: string): string | undefined =>
  new RegExp(`${name}:\\s*([^;]+);`).exec(css)?.[1]?.trim();

describe.each(["light", "dark"] as const)("the %s theme", (scheme) => {
  it("holds the same tokens as the server's", () => {
    expect(DEFAULT_CHART_TOKENS[scheme]).toEqual(fixture(scheme).tokens);
  });

  it("maps them to the same Vega config as the server's", () => {
    expect(chartConfig(DEFAULT_CHART_TOKENS[scheme])).toEqual(fixture(scheme).config);
  });

  it("takes its surface and text colors from tokens.css", () => {
    const css = block(scheme === "dark" ? ":root,\n[data-alkera-color-scheme=\"dark\"]" : ':root[data-alkera-color-scheme="light"]');
    const tokens = DEFAULT_CHART_TOKENS[scheme];
    expect(declared(css, "--alkCardBg")).toBe(tokens.background);
    expect(declared(css, "--alkPrimaryText")).toBe(tokens.text);
    expect(declared(css, "--alkSecondaryText")).toBe(tokens.muted);
    expect(declared(css, "--alkBorder")).toBe(tokens.grid);
    expect(declared(css, "--alkBorderStrong")).toBe(tokens.domain);
    tokens.category.forEach((color, i) => expect(declared(css, `--alkChartSeries${i + 1}`)).toBe(color));
  });
});

describe("readChartTokens", () => {
  it("falls back to the scheme's defaults with a transparent ground when no token resolves", () => {
    const el = document.createElement("div");
    document.body.append(el);
    const tokens = readChartTokens(el);
    expect(tokens.background).toBe("transparent");
    expect(tokens.category).toEqual(DEFAULT_CHART_TOKENS[schemeOf(el)].category);
    el.remove();
  });

  it("reads the light scheme from the element's declared color-scheme", () => {
    const el = document.createElement("div");
    el.style.colorScheme = "light";
    document.body.append(el);
    expect(schemeOf(el)).toBe("light");
    expect(readChartTokens(el).text).toBe(DEFAULT_CHART_TOKENS.light.text);
    el.style.colorScheme = "dark";
    expect(schemeOf(el)).toBe("dark");
    el.remove();
  });

  it("reads a surface that declares no scheme as light, the way a browser paints it", () => {
    const el = document.createElement("div");
    document.body.append(el);
    expect(schemeOf(el)).toBe("light");
    el.style.colorScheme = "normal";
    expect(schemeOf(el)).toBe("light");
    expect(readChartTokens(el).text).toBe(DEFAULT_CHART_TOKENS.light.text);
    el.remove();
  });

  it("follows the reader's OS on a surface that declares both", () => {
    const el = document.createElement("div");
    el.style.colorScheme = "light dark";
    document.body.append(el);
    const real = window.matchMedia;
    const prefer = (dark: boolean) => (query: string) =>
      ({ matches: dark && query.includes("dark"), media: query }) as unknown as MediaQueryList;
    try {
      window.matchMedia = prefer(true);
      expect(schemeOf(el)).toBe("dark");
      window.matchMedia = prefer(false);
      expect(schemeOf(el)).toBe("light");
    } finally {
      window.matchMedia = real;
      el.remove();
    }
  });
});
