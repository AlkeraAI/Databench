import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { StatCard } from "@/pages/workspace/dashboard/components";
import type { Stat } from "@/pages/workspace/dashboard/model";

// The trend chip is now a ui Pill (variant="plain") whose TONE carries the sentiment, and the viz
// decode is the shared Tooltip. The tests assert the user-visible contract — the delta text + the
// tone reading, the card navigating, and the viz decode surfacing without navigating — not a page
// class name (the styling moved into the library primitives).

// The stat card's interactive contracts, verified through the rendered DOM:
//  - the WHOLE card is one link to the metric's page, and clicking it navigates there;
//  - clicking the VIZ does NOT navigate (its click is swallowed so the hover/focus-read stays
//    decoupled from the nav) — while the card around it still does;
//  - focusing the viz surfaces its decode tooltip.

afterEach(cleanup);

const STAT: Stat = {
  key: "requests",
  label: "Requests",
  value: "1,234",
  note: "last 30d",
  to: "/analytics/usage",
  viz: { kind: "spark", points: [1, 4, 2, 8], tip: "peak 8/day · last 30d" },
};

// Renders the current path so a test can assert navigation happened — or did not.
function LocationProbe() {
  return <span data-testid="loc">{useLocation().pathname}</span>;
}

function renderCard(s: Stat = STAT) {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <StatCard s={s} />
      <LocationProbe />
    </MemoryRouter>,
  );
}

// The spark viz's accessible name (from the ui Sparkline) — used to grab the viz element.
const VIZ_NAME = "Trend, last period";

describe("StatCard — clickable card", () => {
  it("renders the whole card as one link to the metric's page", () => {
    renderCard();
    expect(screen.getByRole("link", { name: "Requests: 1,234" })).toHaveAttribute("href", "/analytics/usage");
  });

  it("navigates to the metric's page when the card is clicked", () => {
    renderCard();
    expect(screen.getByTestId("loc")).toHaveTextContent("/");
    fireEvent.click(screen.getByRole("link", { name: "Requests: 1,234" }));
    expect(screen.getByTestId("loc")).toHaveTextContent("/analytics/usage");
  });

  it("points each card at a distinct page when given distinct targets", () => {
    const knowledge: Stat = { ...STAT, label: "Catalog", to: "/knowledge", viz: { kind: "bars", values: [2, 1], tip: "2 models · 1 doc" } };
    renderCard(knowledge);
    expect(screen.getByRole("link", { name: /^Catalog:/ })).toHaveAttribute("href", "/knowledge");
  });
});

describe("StatCard — trend chip", () => {
  // The chip surfaces two reads: the delta text and the sentiment tone. The Pill's `data-tone` carries
  // the sentiment, and tone is independent of direction by design (a falling backlog can be a
  // green/positive event). Drive all three tones so a hardcoded tone (e.g. always success) fails, and
  // assert the tone tracks `tone`, NOT `dir`.
  const toneVal = { pos: "success", neg: "danger", flat: "neutral" } as const;

  it.each([
    { dir: "up", delta: "+9%", tone: "pos" },
    { dir: "down", delta: "-6%", tone: "neg" },
    { dir: "flat", delta: "0%", tone: "flat" },
  ] as const)("renders the $delta delta with the $tone sentiment tone", ({ dir, delta, tone }) => {
    renderCard({ ...STAT, trend: { dir, delta, tone } });
    const chip = screen.getByText(delta).closest(".alk-pill");
    if (!chip) throw new Error("no trend chip rendered");
    expect(chip).toHaveAttribute("data-tone", toneVal[tone]);
  });

  it("colors a falling-but-positive trend by its tone, not its direction", () => {
    // dir down + tone pos (a shrinking backlog is good): the chip must read positive, not negative.
    renderCard({ ...STAT, trend: { dir: "down", delta: "-40%", tone: "pos" } });
    const chip = screen.getByText("-40%").closest(".alk-pill");
    expect(chip).toHaveAttribute("data-tone", "success");
    expect(chip).not.toHaveAttribute("data-tone", "danger");
  });

  it("renders NO trend chip when the card has no real prior period", () => {
    renderCard({ ...STAT, trend: undefined });
    // The card renders no note/delta text that only a trend carries; the plain-value chip is absent.
    expect(screen.queryByText("+9%")).toBeNull();
    expect(screen.queryByText(/%$/)).toBeNull();
  });
});

describe("StatCard — viz decode decoupled from nav", () => {
  it("does NOT navigate when the viz itself is clicked", () => {
    renderCard();
    // The card IS a link to /analytics/usage; clicking the viz inside it must stay on "/".
    fireEvent.click(screen.getByRole("img", { name: VIZ_NAME }));
    expect(screen.getByTestId("loc")).toHaveTextContent("/");
  });

  it("surfaces the decode tooltip when the viz is focused (the keyboard-read path)", () => {
    renderCard();
    // At rest the Tooltip is unmounted (presence) — nothing to read yet.
    expect(screen.queryByRole("tooltip")).toBeNull();
    // Focus (the a11y read) opens it immediately; the decode text is its label.
    fireEvent.focus(screen.getByRole("img", { name: VIZ_NAME }).parentElement!);
    expect(screen.getByRole("tooltip")).toHaveTextContent("peak 8/day · last 30d");
  });
});
