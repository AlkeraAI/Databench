import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { IconChip } from "./IconChip";

// Tests for the @alkera/ui IconChip — the decorative icon badge. They assert the observable contract: it renders the glyph;
// the tone/size seam (a decorative chip carries no text, so its data-attribute is the only observable signal);
// it is hidden from assistive tech by default (it accompanies a real label); a caller className
// survives; and arbitrary span attributes (data-* hooks) forward.

afterEach(cleanup);

const glyph = <svg data-testid="glyph" aria-hidden="true" />;

describe("IconChip", () => {
  // The defaults (brand tone, md size) are the base rule, so they emit no data-attribute.
  it.each([
    { tone: "brand", emitted: false },
    { tone: "neutral", emitted: true },
    { tone: "surface", emitted: true },
  ] as const)("wraps the glyph and sets data-tone for $tone", ({ tone, emitted }) => {
    const { container } = render(<IconChip tone={tone}>{glyph}</IconChip>);
    const chip = container.querySelector(".alk-iconchip")!;
    expect(chip).toContainElement(screen.getByTestId("glyph"));
    if (emitted) expect(chip).toHaveAttribute("data-tone", tone);
    else expect(chip).not.toHaveAttribute("data-tone");
  });

  it.each([
    { size: "sm", emitted: true },
    { size: "md", emitted: false },
    { size: "lg", emitted: true },
  ] as const)("sets data-size for $size", ({ size, emitted }) => {
    const { container } = render(<IconChip size={size}>{glyph}</IconChip>);
    const chip = container.querySelector(".alk-iconchip")!;
    if (emitted) expect(chip).toHaveAttribute("data-size", size);
    else expect(chip).not.toHaveAttribute("data-size");
  });

  // A numeric size is the escape hatch for an off-scale media cell: it must NOT stamp a sm/md/lg
  // scale attribute (which would override the box via CSS), and must instead drive the box + glyph
  // through the custom properties the stylesheet reads — the box at the exact px the caller asked
  // for, the glyph scaled with it (so it isn't left unset → a zero-size icon).
  it("takes a numeric size through custom properties, not a scale attribute", () => {
    // An off-scale media cell needs the exact px it asked for. A stamped sm/md/lg would override the
    // box in CSS, and an unset glyph var would leave a zero-size icon.
    const { container } = render(<IconChip size={30}>{glyph}</IconChip>);
    const chip = container.querySelector(".alk-iconchip") as HTMLElement;
    expect(chip).not.toHaveAttribute("data-size");
    expect(chip.style.getPropertyValue("--alk-iconchip-box")).toBe("30px");
    // The glyph rides ~57% of the box, rounded, so 30 gives 17px and never 0.
    expect(chip.style.getPropertyValue("--alk-iconchip-glyph")).toBe("17px");
  });

  // The caller's style spreads LAST, so a colliding property is theirs. Flip the spread order and
  // the computed 30px would clobber the override.
  it("lets a caller's style override a colliding custom property", () => {
    const { container } = render(
      <IconChip size={30} style={{ ["--alk-iconchip-box" as string]: "99px" } as React.CSSProperties}>
        {glyph}
      </IconChip>,
    );
    const chip = container.querySelector(".alk-iconchip") as HTMLElement;
    expect(chip.style.getPropertyValue("--alk-iconchip-box")).toBe("99px");
    // the non-colliding computed var the caller did NOT override still rides through.
    expect(chip.style.getPropertyValue("--alk-iconchip-glyph")).toBe("17px");
  });

  // The string-size path takes a different style branch (no computed vars to merge): the caller's
  // style must still pass through untouched. A refactor that only wires `style` through inside the
  // numeric branch would silently drop it for every scale-class chip.
  it("passes a caller's style through on the string-size path", () => {
    const { container } = render(
      <IconChip size="md" style={{ marginLeft: 4 }}>
        {glyph}
      </IconChip>,
    );
    const chip = container.querySelector(".alk-iconchip") as HTMLElement;
    expect(chip.style.marginLeft).toBe("4px");
    // no numeric box var on the string path — the scale class owns the box.
    expect(chip.style.getPropertyValue("--alk-iconchip-box")).toBe("");
  });

  it("is hidden from assistive tech by default", () => {
    const { container } = render(<IconChip>{glyph}</IconChip>);
    expect(container.querySelector(".alk-iconchip")).toHaveAttribute("aria-hidden", "true");
  });

  it("lets a caller override the default aria-hidden (a chip that IS the only label)", () => {
    // The `aria-hidden` default must be a default, not a hardcode: a caller spreading their own
    // aria-* (or role) has to win. This pins the prop-merge order — move the {...rest} spread above
    // the literal default and a chip used as a standalone icon would stay silently hidden from AT.
    const { container } = render(
      <IconChip aria-hidden={false} aria-label="Verified">
        {glyph}
      </IconChip>,
    );
    const chip = container.querySelector(".alk-iconchip")!;
    expect(chip).toHaveAttribute("aria-hidden", "false");
    expect(chip).toHaveAttribute("aria-label", "Verified");
  });

  it("merges a caller className and forwards span attributes", () => {
    const { container } = render(
      <IconChip className="bl-plate__chip" data-logo>
        {glyph}
      </IconChip>,
    );
    const chip = container.querySelector(".alk-iconchip")!;
    expect(chip).toHaveClass("alk-iconchip", "bl-plate__chip");
    expect(chip).toHaveAttribute("data-logo");
  });
});
