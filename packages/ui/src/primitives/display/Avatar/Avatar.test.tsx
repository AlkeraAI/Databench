import { createRef } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Avatar } from "./Avatar";

// Tests for the @alkera/ui Avatar (moved here from the portal). The contract: it renders the
// initials; it is decorative (aria-hidden, no role) UNLESS a label is given, in which case it is a
// labelled img that ALSO names the person on hover; a picture replaces the initials and a hue the
// brand tint; and a caller className, style and span attributes survive the merge.

afterEach(cleanup);

describe("Avatar", () => {
  it("renders the initials as decoration by default", () => {
    const { container } = render(<Avatar initials="TL" />);
    expect(screen.getByText("TL")).toBeInTheDocument();
    expect(container.querySelector(".alk-avatar")).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("becomes a labelled img when a label is given", () => {
    render(<Avatar initials="TL" label="Tara Lee" />);
    const img = screen.getByRole("img", { name: "Tara Lee" });
    expect(img).not.toHaveAttribute("aria-hidden");
  });

  it("says the name on hover, from the same string a screen reader is given", () => {
    // Two letters are not a name to a mouse user either: a stand-alone disc has
    // to answer "whose is this?" to a pointer, not only to assistive tech.
    const { container } = render(<Avatar initials="TL" label="Tara Lee" />);
    expect(container.querySelector(".alk-avatar")).toHaveAttribute("title", "Tara Lee");
  });

  it.each([
    ["no label at all", undefined],
    ["an empty label", ""],
    ["a label of only spaces", "   "],
  ])("offers no tooltip and stays decoration with %s", (_case, label) => {
    // An empty title is a tooltip that says nothing, which is worse than none:
    // the disc names nobody, so it goes back to being decoration.
    const { container } = render(<Avatar initials="TL" label={label} />);
    const disc = container.querySelector(".alk-avatar");
    expect(disc).not.toHaveAttribute("title");
    expect(disc).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("merges a caller className (the style-override contract)", () => {
    const { container } = render(<Avatar initials="TL" className="pt-shell-avatar" />);
    expect(container.querySelector(".alk-avatar")).toHaveClass("alk-avatar", "pt-shell-avatar");
  });

  it("draws the picture in place of the initials, as decoration inside the named disc", () => {
    const { container } = render(<Avatar initials="TL" label="Tara Lee" picture="https://pics.test/tara.png" />);
    const picture = container.querySelector<HTMLImageElement>(".alk-avatar img");
    expect(picture?.getAttribute("src")).toBe("https://pics.test/tara.png");
    expect(picture?.alt).toBe("");
    expect(container.querySelector(".alk-avatar")?.textContent).toBe("");
    // One name: the disc's, not a second one from the picture.
    expect(screen.getAllByRole("img", { name: "Tara Lee" })).toHaveLength(1);
  });

  it.each([
    ["no picture", undefined],
    ["a null picture", null],
    ["an empty picture", ""],
  ])("falls back to the initials with %s", (_case, picture) => {
    const { container } = render(<Avatar initials="TL" picture={picture} />);
    expect(container.querySelector(".alk-avatar img")).toBeNull();
    expect(container.querySelector(".alk-avatar")?.textContent).toBe("TL");
  });

  it.each([
    ["a hue", 137, "137"],
    ["a hue of zero", 0, "0"],
  ])("paints the disc in the person's own colour given %s", (_case, hue, expected) => {
    const { container } = render(<Avatar initials="TL" hue={hue} style={{ marginTop: 2 }} />);
    const disc = container.querySelector<HTMLElement>(".alk-avatar")!;
    expect(disc.style.getPropertyValue("--alk-avatar-hue")).toBe(expected);
    expect(disc).toHaveAttribute("data-hued");
    // A caller style is kept beside the hue, not replaced by it.
    expect(disc.style.marginTop).toBe("2px");
  });

  it("keeps the brand tint where no hue is given", () => {
    const { container } = render(<Avatar initials="TL" />);
    const disc = container.querySelector<HTMLElement>(".alk-avatar")!;
    expect(disc.style.getPropertyValue("--alk-avatar-hue")).toBe("");
    expect(disc).not.toHaveAttribute("data-hued");
  });

  it("puts span attributes and a ref on the disc, and leaves a Tooltip trigger to its tip", () => {
    const ref = createRef<HTMLSpanElement>();
    const { container } = render(
      <Avatar initials="TL" label="Tara Lee" ref={ref} tabIndex={0} data-user-id="u1" data-tip="" />,
    );
    const disc = container.querySelector<HTMLElement>(".alk-avatar")!;
    expect(ref.current).toBe(disc);
    expect(disc.tabIndex).toBe(0);
    expect(disc.dataset.userId).toBe("u1");
    // Marked as a Tooltip's trigger: no native title doubling the shared tip,
    // and still named to a reader who cannot hover.
    expect(disc).not.toHaveAttribute("title");
    expect(screen.getByRole("img", { name: "Tara Lee" })).toBe(disc);
  });
});
