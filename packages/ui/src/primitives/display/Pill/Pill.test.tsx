import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Pill } from "./Pill";

// The class is the legitimate observable seam for a text-styling chip; radius and colour pixel values
// need a real browser.

afterEach(cleanup);

function pillEl(): HTMLElement {
  return document.querySelector(".alk-pill") as HTMLElement;
}

describe("Pill shape", () => {
  it("owns radius through shape, independent of size", () => {
    // A bare lg pill stays round: radius is owned by shape, not size.
    const { rerender } = render(<Pill>label</Pill>);
    expect(pillEl()).not.toHaveClass("alk-pill--rect");

    rerender(<Pill size="lg">label</Pill>);
    expect(pillEl()).toHaveClass("alk-pill--lg");
    expect(pillEl()).not.toHaveClass("alk-pill--rect");

    rerender(
      <Pill size="lg" shape="rect">
        label
      </Pill>,
    );
    expect(pillEl()).toHaveClass("alk-pill--lg", "alk-pill--rect");
  });
});

describe("Pill leading mark", () => {
  it("puts the icon in its own slot before the label", () => {
    render(<Pill icon={<svg data-testid="glyph" />}>Admin</Pill>);
    const iconSpan = document.querySelector(".alk-pill__icon");
    expect(iconSpan).toContainElement(screen.getByTestId("glyph"));
    expect(pillEl().firstElementChild).toBe(iconSpan);
    expect(screen.getByText("Admin")).toBeInTheDocument();
  });

  // A chip shows one leading mark at most: an explicit icon wins over the boolean dot, and a bare
  // pill grows neither slot.
  it.each([
    { label: "dot alone", props: { dot: true }, icon: false, dot: true },
    { label: "icon and dot", props: { dot: true, icon: <svg /> }, icon: true, dot: false },
    { label: "neither", props: {}, icon: false, dot: false },
  ])("$label", ({ props, icon, dot }) => {
    render(<Pill {...props}>Online</Pill>);
    expect(document.querySelector(".alk-pill__icon") !== null).toBe(icon);
    expect(document.querySelector(".alk-pill__dot") !== null).toBe(dot);
  });
});

describe("Pill tone and variant", () => {
  // Emphasis is tone x variant: every tone defaults to the soft wash, and `solid` is the
  // full-strength fill. A swap of the axes would render the wrong emphasis.
  it("layers the variant onto the tone, soft by default", () => {
    const { rerender } = render(<Pill tone="brand">Admin</Pill>);
    expect(pillEl()).toHaveAttribute("data-tone", "brand");
    expect(pillEl()).toHaveClass("alk-pill--soft");
    expect(pillEl()).not.toHaveClass("alk-pill--solid");

    rerender(
      <Pill tone="brand" variant="solid">
        3
      </Pill>,
    );
    expect(pillEl()).toHaveAttribute("data-tone", "brand");
    expect(pillEl()).toHaveClass("alk-pill--solid");
  });

  it("defaults to the neutral tone, so no brand emphasis appears unasked", () => {
    render(<Pill>label</Pill>);
    expect(pillEl()).toHaveAttribute("data-tone", "neutral");
  });
});

describe("Pill composition", () => {
  it("an interactive rect pill with an icon is a button carrying all three", () => {
    render(
      <Pill interactive shape="rect" tone="brand" icon={<svg data-testid="glyph" />}>
        Admin
      </Pill>,
    );
    const btn = screen.getByRole("button", { name: "Admin" });
    expect(btn).toHaveClass("alk-pill", "alk-pill--rect");
    expect(btn).toHaveAttribute("data-tone", "brand");
    expect(btn.querySelector(".alk-pill__icon")).toContainElement(screen.getByTestId("glyph"));
  });
});
