// The chart frame: Vega arrives lazily, draws, reports what it cannot show
// without echoing the spec, and tears its view down.

import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { AlkeraChart } from "./AlkeraChart";
import { corpus, type InvalidCase } from "./corpus";
import { DEFAULT_CHART_TOKENS } from "./theme";

const tokens = DEFAULT_CHART_TOKENS.dark;

// jsdom has no canvas; Vega then estimates text widths, which is all a test needs.
beforeAll(() => {
  HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
});

const ROWS = [
  { day: "2026-01-01", orders: 3 },
  { day: "2026-01-02", orders: 7 },
  { day: "2026-01-03", orders: 5 },
];
const BOUND = {
  $schema: "https://vega.github.io/schema/vega-lite/v5.json",
  mark: "line",
  encoding: { x: { field: "day", type: "temporal" }, y: { field: "orders", type: "quantitative" } },
};

describe("AlkeraChart", () => {
  it("shows a loading state, then draws the result's rows into a bound spec", async () => {
    const { container } = render(<AlkeraChart spec={BOUND} data={ROWS} tokens={tokens} ariaLabel="Orders by day" />);
    expect(screen.getByText("Loading chart…")).toBeInTheDocument();
    await waitFor(() => expect(container.querySelector("svg")).not.toBeNull());
    expect(screen.queryByText("Loading chart…")).toBeNull();
    const figure = screen.getByRole("figure", { name: "Orders by day" });
    expect(figure).toHaveAttribute("aria-busy", "false");
    // One line path through three points: the rows reached the mark.
    const line = container.querySelector(".mark-line path");
    expect(line?.getAttribute("d")?.match(/[ML]/g)).toHaveLength(3);
  });

  it("draws nothing for a bound spec without rows, rather than failing", async () => {
    const { container } = render(<AlkeraChart spec={BOUND} tokens={tokens} />);
    await waitFor(() => expect(container.querySelector("svg")).not.toBeNull());
    expect(container.querySelector(".mark-line path")?.getAttribute("d") ?? "").toBe("");
  });

  it("binds a host table to a named reference", async () => {
    const spec = { data: { name: "result" }, mark: "bar", encoding: BOUND.encoding };
    const { container } = render(<AlkeraChart spec={spec} datasets={{ result: ROWS }} tokens={tokens} />);
    await waitFor(() => expect(container.querySelectorAll(".mark-rect path")).toHaveLength(3));
  });

  it("explains a missing host table instead of drawing an empty chart", async () => {
    const onError = vi.fn();
    const spec = { data: { name: "result" }, mark: "bar", encoding: BOUND.encoding };
    render(<AlkeraChart spec={spec} tokens={tokens} onError={onError} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("This chart could not be drawn.");
    expect(onError).toHaveBeenCalledOnce();
  });

  it.each(
    corpus<InvalidCase>("profile/invalid")
      .filter((c) => c.value.unsafe)
      .map((c) => [c.name, c.value.spec] as const),
  )("blocks the unsafe spec %s without echoing it", async (_name, spec) => {
    const onError = vi.fn();
    const { container } = render(<AlkeraChart spec={spec} tokens={tokens} onError={onError} />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This chart is blocked because it tries to load or run something.");
    expect(container.textContent).not.toMatch(/evil\.example|datum|now\(\)/);
    expect(container.querySelector("svg")).toBeNull();
    expect(onError).toHaveBeenCalledOnce();
  });

  it("finalizes its view and tooltip on unmount", async () => {
    const before = document.querySelectorAll(".alk-chart-tooltip").length;
    const { container, unmount } = render(<AlkeraChart spec={BOUND} data={ROWS} tokens={tokens} />);
    await waitFor(() => expect(container.querySelector("svg")).not.toBeNull());
    expect(document.querySelectorAll(".alk-chart-tooltip")).toHaveLength(before + 1);
    unmount();
    expect(document.querySelectorAll(".alk-chart-tooltip")).toHaveLength(before);
  });

  it("redraws when the spec changes, leaving one view behind", async () => {
    const { container, rerender } = render(<AlkeraChart spec={BOUND} data={ROWS} tokens={tokens} />);
    await waitFor(() => expect(container.querySelector(".mark-line")).not.toBeNull());
    rerender(<AlkeraChart spec={{ ...BOUND, mark: "bar" }} data={ROWS} tokens={tokens} />);
    await waitFor(() => expect(container.querySelector(".mark-rect")).not.toBeNull());
    expect(container.querySelectorAll("svg")).toHaveLength(1);
    expect(container.querySelector(".mark-line")).toBeNull();
  });

  it("draws again in the new scheme when the page's theme changes", async () => {
    const { container } = render(<AlkeraChart spec={BOUND} data={ROWS} />);
    await waitFor(() => expect(container.querySelector("svg")).not.toBeNull());
    const labelFill = (): string | null | undefined =>
      container.querySelector(".role-axis-label text")?.getAttribute("fill");
    expect(labelFill()).toBe(DEFAULT_CHART_TOKENS.light.muted);

    const frame = container.querySelector<HTMLElement>(".alk-chart");
    if (frame === null) throw new Error("no chart frame");
    frame.style.colorScheme = "dark";
    act(() => document.documentElement.setAttribute("data-alkera-color-scheme", "dark"));
    try {
      await waitFor(() => expect(labelFill()).toBe(DEFAULT_CHART_TOKENS.dark.muted));
    } finally {
      document.documentElement.removeAttribute("data-alkera-color-scheme");
    }
  });
});

describe("AlkeraChart across re-renders", () => {
  it("keeps its drawing when a host re-renders with the same spec and rows in new objects", async () => {
    const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
    const view = render(<AlkeraChart spec={copy(BOUND)} data={copy(ROWS)} tokens={copy(tokens)} />);
    await waitFor(() => expect(view.container.querySelector(".mark-line path")).not.toBeNull());
    const drawn = view.container.querySelector("svg");
    for (let i = 0; i < 5; i += 1) {
      view.rerender(<AlkeraChart spec={copy(BOUND)} data={copy(ROWS)} tokens={copy(tokens)} />);
      expect(screen.getByRole("figure")).toHaveAttribute("aria-busy", "false");
      expect(view.container.querySelector("svg")).toBe(drawn);
    }
  });

  it("draws again when the rows change", async () => {
    const view = render(<AlkeraChart spec={BOUND} data={ROWS} tokens={tokens} />);
    await waitFor(() => expect(view.container.querySelector(".mark-line path")).not.toBeNull());
    const drawn = view.container.querySelector("svg");
    view.rerender(<AlkeraChart spec={BOUND} data={ROWS.slice(0, 2)} tokens={tokens} />);
    await waitFor(() => expect(view.container.querySelector("svg")).not.toBe(drawn));
    await waitFor(() =>
      expect(view.container.querySelector(".mark-line path")?.getAttribute("d")?.match(/[ML]/g)).toHaveLength(2),
    );
  });
});
