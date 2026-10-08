// The chart, across its three owners: derived here, admitted and stored by the
// server, read back by the object page.
//
// The two artifacts under `packages/api-core/tests/fixtures/objects/seam/` are
// the only spelling: the builder's output is pinned byte-for-byte against
// `chart_spec_from_browser.json` (which the server's
// `test_chart_spec_browser_seam.py` feeds to the real allowlist), and the
// reader is driven from `chart_spec_persisted.json` (which that same server
// test pins as the real `.persisted()` output). Change the shape and one of
// the two suites goes red before a saved chart quietly stops drawing.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { render, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AlkeraChart, DEFAULT_CHART_TOKENS, guardSpec } from "@alkera/ui";

import { timeSeriesSpec } from "@/pages/workspace/chat/chartDerive";
import { chartCaption, chartSpecOf, rowsForChart } from "@/pages/workspace/objects/chartSpec";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../../..");
const SEAM = "packages/api-core/tests/fixtures/objects/seam";
const read = (name: string): unknown =>
  JSON.parse(readFileSync(resolve(REPO_ROOT, SEAM, name), "utf-8"));

describe("the chart spec across the browser/server seam", () => {
  it("what the save dialog builds is byte-for-byte the artifact the server validates", () => {
    expect(timeSeriesSpec({ x: "day", y: "orders" })).toEqual(read("chart_spec_from_browser.json"));
  });

  it("what the server persists passes the renderer's guard and earns the page's caption", () => {
    const chart = chartSpecOf(read("chart_spec_persisted.json"));
    expect(chart).not.toBeNull();
    expect(guardSpec(chart)).toEqual({ ok: true });
    expect(chartCaption(chart as Record<string, unknown>)).toBe("orders over day");
  });

  it("is drawn from the rows route's page, bound by column key", async () => {
    const chart = chartSpecOf(read("chart_spec_persisted.json"));
    // The rows route's shape: labels in `columns`, stable names in `keys`.
    const page = {
      columns: ["Day", "Orders"],
      keys: ["day", "orders"],
      rows: [
        ["2026-09-05", 12],
        ["2026-09-06", 17],
      ],
    };
    expect(rowsForChart(page)).toEqual([
      { day: "2026-09-05", orders: 12 },
      { day: "2026-09-06", orders: 17 },
    ]);
    const { container } = render(
      <AlkeraChart spec={chart} data={rowsForChart(page)} tokens={DEFAULT_CHART_TOKENS.light} />,
    );
    await waitFor(() =>
      expect(container.querySelector(".mark-line path")?.getAttribute("d")?.match(/[ML]/g)).toHaveLength(2),
    );
  });
});
