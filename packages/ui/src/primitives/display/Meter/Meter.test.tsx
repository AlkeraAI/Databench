import { readFileSync } from "node:fs";
import { join } from "node:path";

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Meter } from "./Meter";

afterEach(cleanup);

// jsdom neither cascades nor computes custom properties, so what an EMPTY meter looks like is
// read off the sheets: the track's declaration, and what the surface tokens are worth in each
// scheme. Vitest runs this package from its own root.
const UI_SRC = join(process.cwd(), "src");
const METER_CSS = readFileSync(join(UI_SRC, "primitives/display/Meter/meter.css"), "utf8");
const TOKENS_CSS = readFileSync(join(UI_SRC, "theme/tokens.css"), "utf8");

/** The `background` the bare `.alk-meter` track rule declares. */
function trackBackground(): string {
  const rule = METER_CSS.match(/\.alk-meter\s*\{([^}]*)\}/);
  const declaration = rule?.[1]?.match(/(?:^|;)\s*background(?:-color)?\s*:\s*([^;]+)/);
  if (!declaration?.[1]) throw new Error("the meter track declares no background");
  return declaration[1].trim();
}

/** Every literal a token is assigned across the schemes (`--alkCardBg: #ffffff;` → "#ffffff"). */
function assignments(token: string): string[] {
  return Array.from(TOKENS_CSS.matchAll(new RegExp(`${token}\\s*:\\s*([^;]+);`, "g")), (m) =>
    (m[1] ?? "").trim().toLowerCase(),
  );
}

/** The tokens a card, a control or a page is painted with — the grounds a meter sits on. */
const SURFACE_TOKENS = ["--alkCardBg", "--alkControlBg", "--alkRaisedBg", "--alkOverlayBg", "--alkPageBg"];

function meter(): HTMLElement {
  const el = document.querySelector<HTMLElement>(".alk-meter");
  if (!el) throw new Error("no meter");
  return el;
}

describe("Meter", () => {
  it("reads 0-100 as a progressbar and drives the fill var", () => {
    const { rerender } = render(<Meter value={0.42} label="Credits used" />);
    const el = meter();
    expect(el).toHaveAttribute("role", "progressbar");
    expect(el).toHaveAttribute("aria-label", "Credits used");
    expect(el).toHaveAttribute("aria-valuenow", "42");
    expect(el).toHaveAttribute("aria-valuemin", "0");
    expect(el).toHaveAttribute("aria-valuemax", "100");
    // The fill scale rides a custom property, so it animates via transform, not width.
    expect(el.style.getPropertyValue("--alk-meter-v")).toBe("0.42");
    // sm is the base rule, so only a larger size carries the attribute.
    expect(el).not.toHaveAttribute("data-size");
    rerender(<Meter value={0.42} size="lg" />);
    expect(meter()).toHaveAttribute("data-size", "lg");
  });

  it("clamps a value above 1 to a full bar and below 0 to empty", () => {
    render(<Meter value={1.8} />);
    expect(meter().getAttribute("aria-valuenow")).toBe("100");
    cleanup();
    render(<Meter value={-3} />);
    expect(meter().getAttribute("aria-valuenow")).toBe("0");
  });

  // Every tone in the union lands on its own fill class — and no other tone's (the class is the only
  // seam the stylesheet keys the bar ink off).
  it.each([
    { tone: "brand" },
    { tone: "neutral" },
    { tone: "info" },
    { tone: "success" },
    { tone: "warning" },
    { tone: "danger" },
  ] as const)("paints the $tone fill exactly", ({ tone }) => {
    render(<Meter value={0.5} tone={tone} />);
    const fill = document.querySelector(".alk-meter__fill");
    expect(fill).toHaveClass(`alk-meter__fill--${tone}`);
    expect(fill?.className.match(/alk-meter__fill--/g)).toHaveLength(1);
  });

  it("forwards className and data attributes to the track", () => {
    render(<Meter value={0.5} className="mine" data-testid="m" />);
    expect(meter().className).toContain("mine");
    expect(meter()).toHaveAttribute("data-testid", "m");
  });

  // An org that has used nothing renders a 0% meter. The track is then the whole gauge, and a
  // track painted in the card's own colour is invisible on it (in light, the control ground is
  // the card's white).
  it("paints the empty track in a colour that is not any surface's colour", () => {
    const track = trackBackground();
    for (const token of SURFACE_TOKENS) {
      expect(track, `the track must not be the ${token} surface`).not.toContain(token);
      // and not a literal equal to that surface's value in either scheme
      for (const literal of assignments(token)) expect(track.toLowerCase()).not.toBe(literal);
    }
    // The track is a tint of the ink over whatever ground it sits on — the one construction that
    // contrasts with every surface in both schemes — and dark enough to read, not a 3% whisper.
    const tint = track.match(/color-mix\(in srgb,\s*var\(--alkPrimaryText\)\s*(\d+)%,\s*transparent\)/);
    expect(tint, `the track is "${track}", not an ink tint`).not.toBeNull();
    expect(Number(tint?.[1])).toBeGreaterThanOrEqual(8);
  });

  it("engraves a tick layer only when ticks are requested, with segments = ticks + 1", () => {
    const { rerender } = render(<Meter value={0.5} ticks={3} />);
    expect(document.querySelector(".alk-meter__ticks")).not.toBeNull();
    // a ready percentage (100% / segments) — calc division by a var doesn't parse in CSS
    expect(meter().style.getPropertyValue("--alk-meter-seg")).toBe("25%");
    rerender(<Meter value={0.5} />);
    expect(document.querySelector(".alk-meter__ticks")).toBeNull();
    // zero ticks is a plain gauge, not a degenerate single-segment scale
    rerender(<Meter value={0.5} ticks={0} />);
    expect(document.querySelector(".alk-meter__ticks")).toBeNull();
  });
});
