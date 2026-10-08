import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Callout } from "./Callout";

// The toned call-out box. A colour-only box has no text to betray a wrong tint, so `data-tone` is the
// load-bearing seam. Pixel colours need a real browser.

afterEach(cleanup);

function calloutEl(): HTMLElement {
  return document.querySelector(".alk-callout") as HTMLElement;
}

describe("Callout tone", () => {
  it.each([
    { tone: "brand" },
    { tone: "neutral" },
    { tone: "info" },
    { tone: "success" },
    { tone: "warning" },
    { tone: "danger" },
  ] as const)("applies the $tone tone exactly", ({ tone }) => {
    render(<Callout tone={tone}>body</Callout>);
    const el = calloutEl();
    expect(el).toHaveClass("alk-callout");
    expect(el).toHaveAttribute("data-tone", tone);
  });

  it("defaults to the info tone when none is given", () => {
    render(<Callout>body</Callout>);
    expect(calloutEl()).toHaveAttribute("data-tone", "info");
  });
});

describe("Callout role", () => {
  // Every tone announces politely except danger, which must interrupt. A decorative plate opts out.
  it.each([
    { label: "a non-danger tone announces politely", props: { tone: "info" as const }, role: "status" },
    { label: "the danger tone interrupts", props: { tone: "danger" as const }, role: "alert" },
    { label: "role=note stays silent", props: { tone: "success" as const, role: "note" as const }, role: "note" },
  ])("$label", ({ props, role }) => {
    render(<Callout {...props}>body</Callout>);
    expect(screen.getByRole(role)).toBeInTheDocument();
    for (const other of ["status", "alert", "note"].filter((r) => r !== role)) {
      expect(screen.queryByRole(other)).toBeNull();
    }
  });
});

describe("Callout title", () => {
  it("renders a headline over the body only when a title is given", () => {
    const { rerender } = render(<Callout tone="brand">verified</Callout>);
    expect(calloutEl().querySelector(".alk-callout__title")).toBeNull();
    expect(calloutEl().querySelector(".alk-callout__text")).toHaveTextContent("verified");

    rerender(
      <Callout tone="danger" title="Invalid link">
        Try again.
      </Callout>,
    );
    expect(screen.getByText("Invalid link")).toHaveClass("alk-callout__title");
    expect(calloutEl().querySelector(".alk-callout__text")).toHaveTextContent("Try again.");
  });
});

describe("Callout leading mark", () => {
  it("renders a default glyph for the tone", () => {
    render(<Callout tone="warning">body</Callout>);
    const icon = document.querySelector(".alk-callout__icon");
    expect(icon).not.toBeNull();
    expect(icon?.querySelector("svg")).not.toBeNull();
  });

  it("uses a caller's icon over the tone default", () => {
    render(<Callout icon={<svg data-testid="custom" />}>body</Callout>);
    expect(document.querySelector(".alk-callout__icon")).toContainElement(screen.getByTestId("custom"));
  });

  // null drops the mark entirely, unlike `undefined`, which falls back to the tone default.
  it("renders no mark when icon is null", () => {
    render(<Callout icon={null}>body</Callout>);
    expect(document.querySelector(".alk-callout__icon")).toBeNull();
  });

  // neutral is a quiet inset rather than a status, so alone among the tones it defaults to no mark.
  it("renders no default mark for the neutral tone", () => {
    render(<Callout tone="neutral">quiet</Callout>);
    expect(document.querySelector(".alk-callout__icon")).toBeNull();
  });
});

describe("Callout override", () => {
  it("merges a caller className onto the root", () => {
    render(<Callout className="page-assay">body</Callout>);
    expect(calloutEl()).toHaveClass("alk-callout", "page-assay");
  });
});
