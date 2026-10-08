import { createRef } from "react";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Card } from "./Card";

// The titled surface. The load-bearing invariant is that `size` is a single dial driving the COMPOSED
// IconChip too, so a md card always carries a md chip. Optical centring needs a real browser.

afterEach(cleanup);

const glyph = <svg data-testid="glyph" aria-hidden="true" />;

describe("Card", () => {
  it("renders a bare surface when no header props are given", () => {
    const { container } = render(<Card>Body copy.</Card>);
    expect(container.querySelector(".alk-card")).toBeInTheDocument();
    expect(container.querySelector(".alk-card__head")).toBeNull();
    expect(container.querySelector(".alk-card__body")).toHaveTextContent("Body copy.");
  });

  it.each([undefined, 2, 4, 5] as const)("titles at heading level %s (default 3)", (level) => {
    render(
      <Card title="Section" headingLevel={level}>
        x
      </Card>,
    );
    expect(screen.getByRole("heading", { name: "Section", level: level ?? 3 })).toBeInTheDocument();
  });

  // A size that styled the card but left the chip at the default md would read as a mismatched pair.
  it.each([
    { size: "sm" as const, chipSize: "sm" },
    { size: "md" as const, chipSize: undefined },
    { size: "lg" as const, chipSize: "lg" },
  ])("size $size drives the surface and the composed chip", ({ size, chipSize }) => {
    const { container } = render(
      <Card size={size} icon={glyph} title="Titled">
        x
      </Card>,
    );
    expect(container.querySelector(".alk-card")).toHaveClass(`alk-card--${size}`);
    const chip = container.querySelector(".alk-iconchip")!;
    // md is the default, so it emits no data-size.
    if (chipSize) expect(chip).toHaveAttribute("data-size", chipSize);
    else expect(chip).not.toHaveAttribute("data-size");
    expect(chip).toContainElement(screen.getByTestId("glyph"));
  });

  it.each([
    { tone: undefined, expectedTone: undefined, label: "default brand" },
    { tone: "surface" as const, expectedTone: "surface", label: "surface" },
    { tone: "neutral" as const, expectedTone: "neutral", label: "neutral" },
  ])("tones the composed chip ($label)", ({ tone, expectedTone }) => {
    const { container } = render(
      <Card icon={glyph} tone={tone} title="Logo">
        x
      </Card>,
    );
    const chip = container.querySelector(".alk-iconchip")!;
    if (expectedTone) expect(chip).toHaveAttribute("data-tone", expectedTone);
    else expect(chip).not.toHaveAttribute("data-tone");
  });

  // The header renders when ANY of icon / title / actions / eyebrow is present, the asymmetric cases a
  // `hasHeader = title != null` shortcut would silently drop.
  it.each([
    { label: "icon", props: { icon: glyph }, present: ".alk-iconchip" },
    { label: "actions", props: { actions: <button type="button">Manage</button> }, present: ".alk-card__actions" },
    { label: "eyebrow", props: { eyebrow: "Registers" }, present: ".alk-card__eyebrow" },
  ])("renders a header for a $label-only card", ({ props, present }) => {
    const { container } = render(<Card {...props}>x</Card>);
    expect(container.querySelector(".alk-card__head")).toBeInTheDocument();
    expect(container.querySelector(present)).toBeInTheDocument();
    expect(container.querySelector(".alk-card__title")).toBeNull();
  });

  it("mints no chip for a title-only header", () => {
    const { container } = render(<Card title="No glyph">x</Card>);
    expect(container.querySelector(".alk-card__head")).toBeInTheDocument();
    expect(container.querySelector(".alk-iconchip")).toBeNull();
  });

  it("rules the body only when divided is set", () => {
    const { container, rerender } = render(<Card>plain</Card>);
    expect(container.querySelector(".alk-card__body")).not.toHaveClass("alk-card__body--divided");
    rerender(<Card divided>ruled</Card>);
    expect(container.querySelector(".alk-card__body")).toHaveClass("alk-card__body--divided");
  });

  it("renders no body when there are no children", () => {
    const { container } = render(<Card title="Header only" />);
    expect(container.querySelector(".alk-card__head")).toBeInTheDocument();
    expect(container.querySelector(".alk-card__body")).toBeNull();
  });

  it("merges a caller className and forwards root props", () => {
    const { container } = render(
      <Card className="dsh-card" data-testid="card">
        x
      </Card>,
    );
    const card = container.querySelector(".alk-card")!;
    expect(card).toHaveClass("alk-card", "dsh-card");
    expect(card).toHaveAttribute("data-testid", "card");
  });
});

describe("Card region", () => {
  // `region` makes the surface a navigable AT landmark whose accessible name IS the title, via a real
  // aria-labelledby link. No linkage unless `region && title`.
  it("names the section landmark by its title", () => {
    const { container } = render(
      <Card region title="General">
        body
      </Card>,
    );
    const region = screen.getByRole("region", { name: "General" });
    expect(region.tagName).toBe("SECTION");
    const title = container.querySelector(".alk-card__title")!;
    expect(title.id).toBeTruthy();
    expect(region).toHaveAttribute("aria-labelledby", title.id);
    expect(region).toHaveAccessibleName("General");
  });

  // An unconditional aria-labelledby would mint a broken, empty-named region here (jsdom does expose a
  // section whose labelledby points at nothing), which is what the queryByRole assertion closes.
  it("stays unnamed when region is set without a title", () => {
    const { container } = render(<Card region>body</Card>);
    const root = container.querySelector(".alk-card")!;
    expect(root.tagName).toBe("SECTION");
    expect(root).not.toHaveAttribute("aria-labelledby");
    expect(screen.queryByRole("region")).toBeNull();
  });

  it("is a plain div by default, even with a title", () => {
    const { container } = render(<Card title="General">body</Card>);
    const root = container.querySelector(".alk-card")!;
    expect(root.tagName).toBe("DIV");
    expect(root).not.toHaveAttribute("aria-labelledby");
    expect(screen.queryByRole("region")).toBeNull();
  });

  it("gives each region its own title linkage", () => {
    render(
      <>
        <Card region title="Alpha">
          a
        </Card>
        <Card region title="Beta">
          b
        </Card>
      </>,
    );
    const alphaLabel = screen.getByRole("region", { name: "Alpha" }).getAttribute("aria-labelledby");
    const betaLabel = screen.getByRole("region", { name: "Beta" }).getAttribute("aria-labelledby");
    expect(alphaLabel).toBeTruthy();
    expect(betaLabel).toBeTruthy();
    expect(alphaLabel).not.toBe(betaLabel);
  });
});

describe("Card variant", () => {
  // The panel variant trades the section look (a toned IconChip + serif title) for a plain brand glyph
  // under a small-caps eyebrow. Keeping the chip in panel would carry the filled disc into the eyebrow.
  it.each([
    { label: "panel", variant: "panel" as const, wrapper: ".alk-card__glyph", absent: ".alk-iconchip" },
    { label: "section", variant: undefined, wrapper: ".alk-iconchip", absent: ".alk-card__glyph" },
  ])("$label wraps the icon in $wrapper", ({ variant, wrapper, absent }) => {
    const { container } = render(
      <Card variant={variant} icon={glyph} title="Permissions">
        x
      </Card>,
    );
    expect(container.querySelector(wrapper)).toContainElement(screen.getByTestId("glyph"));
    expect(container.querySelector(absent)).toBeNull();
    // The title stays a real heading in both, so the outline is correct either way.
    expect(screen.getByRole("heading", { name: "Permissions" })).toBeInTheDocument();
  });

  it("mints no plain glyph for a title-only panel header", () => {
    const { container } = render(
      <Card variant="panel" title="Permissions">
        x
      </Card>,
    );
    expect(container.querySelector(".alk-card__head")).toBeInTheDocument();
    expect(container.querySelector(".alk-card__glyph")).toBeNull();
    expect(container.querySelector(".alk-iconchip")).toBeNull();
  });
});

describe("Card footer", () => {
  it("renders the footer after the body, and only when given", () => {
    const { container, rerender } = render(<Card>body</Card>);
    expect(container.querySelector(".alk-card__foot")).toBeNull();
    rerender(<Card footer={<button type="button">Save</button>}>body</Card>);
    const foot = container.querySelector(".alk-card__foot")!;
    expect(foot).toContainElement(screen.getByRole("button", { name: "Save" }));
    expect(foot.previousElementSibling).toBe(container.querySelector(".alk-card__body"));
  });
});

describe("Card headerDivider", () => {
  it("rules the header only when headerDivider is set", () => {
    const { container, rerender } = render(<Card title="T">x</Card>);
    expect(container.querySelector(".alk-card")).not.toHaveClass("alk-card--ruled");
    rerender(
      <Card title="T" headerDivider>
        x
      </Card>,
    );
    expect(container.querySelector(".alk-card")).toHaveClass("alk-card--ruled");
  });
});

describe("Card ref", () => {
  it("forwards its ref to the root card element", () => {
    // The knowledge catalogue observes its own width to collapse the toolbar; a non-forwarding Card
    // would leave the ResizeObserver with nothing to watch.
    const ref = createRef<HTMLDivElement>();
    const { container } = render(<Card ref={ref}>body</Card>);
    expect(ref.current).toBe(container.querySelector(".alk-card"));
  });
});

describe("Card eyebrow", () => {
  it("stacks the eyebrow above the title in one heading column", () => {
    const { container } = render(
      <Card eyebrow="Last 30 days" title="Daily spend">
        x
      </Card>,
    );
    const heading = container.querySelector(".alk-card__heading")!;
    const eyebrow = container.querySelector<HTMLElement>(".alk-card__eyebrow");
    expect(eyebrow).toHaveTextContent("Last 30 days");
    expect(heading).toContainElement(eyebrow);
    expect(heading).toContainElement(container.querySelector<HTMLElement>(".alk-card__title"));
  });

  it("does not wrap the title when no eyebrow is given", () => {
    // Existing consumers keep the title as a direct child of the id cluster, so no layout shift.
    const { container } = render(<Card title="Plain">x</Card>);
    expect(container.querySelector(".alk-card__heading")).toBeNull();
    expect(container.querySelector(".alk-card__title")).toHaveTextContent("Plain");
  });
});
