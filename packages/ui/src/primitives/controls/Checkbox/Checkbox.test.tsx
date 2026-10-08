import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Checkbox } from "./Checkbox";

// Tests for the @alkera/ui Checkbox — a styled native <input type="checkbox">. They assert the
// observable contract: it renders a real checkbox input; the size seam is a data-attribute (the box
// edge is CSS-driven, so the attribute is the only jsdom-observable signal of the chosen size); the
// default md emits no attribute so the base rule applies; a visible label wraps both; and a bare box
// (no label) renders the input alone. (The exact resolved pixel size is a rendered concern verified
// on the running app — the CSS keys --alk-check-size off these attributes.)

afterEach(cleanup);

describe("Checkbox", () => {
  it("renders a native checkbox input", () => {
    render(<Checkbox aria-label="Verified" />);
    const box = screen.getByRole("checkbox", { name: "Verified" });
    expect(box).toHaveClass("alk-checkbox");
    expect(box.tagName).toBe("INPUT");
    expect(box).toHaveAttribute("type", "checkbox");
  });

  it.each([
    { size: "sm" as const, emitted: true },
    { size: "md" as const, emitted: false },
    { size: "lg" as const, emitted: true },
  ])("emits data-size only for the non-default $size size (default md omits it)", ({ size, emitted }) => {
    render(<Checkbox aria-label="box" size={size} />);
    const box = screen.getByRole("checkbox", { name: "box" });
    if (emitted) expect(box).toHaveAttribute("data-size", size);
    else expect(box).not.toHaveAttribute("data-size");
  });

  it("wraps a visible label with the box, and renders bare without one", () => {
    const { container, rerender } = render(<Checkbox label="Share with the team" />);
    const field = container.querySelector(".alk-checkbox-field")!;
    expect(field.tagName).toBe("LABEL");
    expect(field.querySelector(".alk-checkbox")).toBeInTheDocument();
    expect(field.querySelector(".alk-checkbox-field__label")).toHaveTextContent("Share with the team");

    rerender(<Checkbox aria-label="bare" />);
    expect(container.querySelector(".alk-checkbox-field")).toBeNull();
    expect(container.querySelector(".alk-checkbox")).toBeInTheDocument();
  });

  it("forwards native props (checked) and a caller className onto the input", () => {
    render(<Checkbox aria-label="c" checked readOnly className="ktm-check" />);
    const box = screen.getByRole("checkbox", { name: "c" });
    expect(box).toBeChecked();
    expect(box).toHaveClass("alk-checkbox", "ktm-check");
  });
});
