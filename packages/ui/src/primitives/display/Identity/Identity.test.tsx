import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Identity } from "./Identity";

// An avatar (from `initials`) OR a custom `leading` node, beside a `name` line over an optional
// `secondary`, with an inline `nameTrailing` and a far-edge `trailing`. Identity is pure passthrough,
// so slot contents bind to named fixtures and the assertions read the rendered slots.

afterEach(cleanup);

const NAME = "Marcus North";
const EMAIL = "marcus@x.io";
const INITIALS = "MN";

function root(): HTMLElement {
  return document.querySelector(".alk-identity") as HTMLElement;
}

describe("Identity leading", () => {
  it("leads with an Avatar built from initials, at the row's size", () => {
    render(<Identity initials={INITIALS} name={NAME} size="sm" />);
    const avatar = document.querySelector(".alk-avatar");
    expect(avatar).toHaveTextContent(INITIALS);
    expect(avatar).toHaveAttribute("data-size", "sm");
    expect(root().firstElementChild).toBe(avatar);
  });

  it("a leading node replaces the Avatar rather than joining it", () => {
    render(<Identity initials={INITIALS} leading={<span data-testid="chip" />} name={NAME} />);
    expect(screen.getByTestId("chip")).toBeInTheDocument();
    expect(document.querySelector(".alk-avatar")).toBeNull();
    // The monogram does not leak in as bare text either.
    expect(screen.queryByText(INITIALS)).toBeNull();
  });

  it("renders no leading element when given neither", () => {
    render(<Identity name={NAME} />);
    expect(document.querySelector(".alk-avatar")).toBeNull();
    expect(root().firstElementChild).toHaveClass("alk-identity__text");
  });
});

describe("Identity text + slots", () => {
  it("renders the secondary line only when there is one", () => {
    // An always-rendered secondary span would ship an empty demoted line.
    const { rerender } = render(<Identity initials={INITIALS} name={NAME} />);
    expect(screen.getByText(NAME)).toBeInTheDocument();
    expect(document.querySelector(".alk-identity__secondary")).toBeNull();

    rerender(<Identity initials={INITIALS} name={NAME} secondary={EMAIL} />);
    expect(screen.getByText(EMAIL)).toBeInTheDocument();
  });

  it("keeps nameTrailing inline and trailing at the far edge", () => {
    render(
      <Identity
        initials={INITIALS}
        name={NAME}
        nameTrailing={<span data-testid="inline" />}
        trailing={<span data-testid="edge" />}
      />,
    );
    expect(document.querySelector(".alk-identity__name")).toContainElement(screen.getByTestId("inline"));
    expect(screen.getByTestId("inline").parentElement).not.toBe(root());
    expect(screen.getByTestId("edge").parentElement).toBe(root());
    expect(root().lastElementChild).toBe(screen.getByTestId("edge"));
  });

  it("merges a caller className", () => {
    render(<Identity initials={INITIALS} name={NAME} className="pt-roster-id" />);
    expect(root()).toHaveClass("alk-identity", "pt-roster-id");
  });
});
