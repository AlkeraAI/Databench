import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Button } from "./Button";

// The single primitive behind both a labelled action and an icon-only control. An icon-only button
// carries no text, so its variant/size attribute is the only signal a test or a stylesheet can see:
// the documented exception to "don't pin a class".

afterEach(cleanup);

const glyph = <svg data-testid="glyph" aria-hidden="true" />;

describe("Button", () => {
  it("labels a text button by its own text", () => {
    render(<Button>Save</Button>);
    const btn = screen.getByRole("button", { name: "Save" });
    expect(btn).toHaveClass("alk-btn");
    expect(btn).toHaveAttribute("data-variant", "primary");
    expect(btn).toHaveAttribute("data-fill", "filled");
    // Not icon-only, so the label wrapper applies the icon/label gap rules.
    expect(btn).not.toHaveAttribute("data-icon");
    expect(btn.querySelector(".alk-btn__label")).not.toBeNull();
  });

  it("names an icon-only button by its aria-label", () => {
    render(
      <Button iconOnly aria-label="Delete row">
        {glyph}
      </Button>,
    );
    const btn = screen.getByRole("button", { name: "Delete row" });
    expect(btn).toHaveAttribute("data-icon", "");
    expect(screen.getByTestId("glyph")).toBeInTheDocument();
    // The bare glyph renders directly, with no label wrapper to add padding.
    expect(btn.querySelector(".alk-btn__label")).toBeNull();
  });

  // `variant` (colour) and `fill` (weight) are orthogonal axes on their own attributes, so asserting
  // the exact value is the leak guard.
  it.each(["primary", "secondary", "destructive"] as const)("applies the %s variant exactly", (variant) => {
    render(
      <Button iconOnly aria-label="Action" variant={variant}>
        {glyph}
      </Button>,
    );
    expect(screen.getByRole("button", { name: "Action" })).toHaveAttribute("data-variant", variant);
  });

  it.each(["filled", "outline", "ghost"] as const)("applies the %s fill exactly", (fill) => {
    render(
      <Button iconOnly aria-label="Action" fill={fill}>
        {glyph}
      </Button>,
    );
    expect(screen.getByRole("button", { name: "Action" })).toHaveAttribute("data-fill", fill);
  });

  // lg is the default, carried by the base rule's token fallback, so it emits NO data-size. An
  // implementation that always set the attribute would emit a dangling `lg` with no matching rule.
  it.each([
    { size: "sm" as const, attr: "sm" },
    { size: "md" as const, attr: "md" },
    { size: "lg" as const, attr: null },
  ])("carries data-size=$attr for the $size size", ({ size, attr }) => {
    render(
      <Button iconOnly aria-label="Action" size={size}>
        {glyph}
      </Button>,
    );
    const btn = screen.getByRole("button", { name: "Action" });
    if (attr) expect(btn).toHaveAttribute("data-size", attr);
    else expect(btn).not.toHaveAttribute("data-size");
  });

  it("merges a caller className onto the root", () => {
    render(
      <Button iconOnly aria-label="Close" className="alk-modal__x">
        {glyph}
      </Button>,
    );
    expect(screen.getByRole("button", { name: "Close" })).toHaveClass("alk-btn", "alk-modal__x");
  });

  it("is type=button unless a type is given", () => {
    const onSubmit = vi.fn((e: React.FormEvent) => e.preventDefault());
    const { rerender } = render(
      <form onSubmit={onSubmit}>
        <Button aria-label="Clear">Clear</Button>
      </form>,
    );
    expect((screen.getByRole("button", { name: "Clear" }) as HTMLButtonElement).type).toBe("button");
    rerender(<Button type="submit">Submit</Button>);
    expect((screen.getByRole("button", { name: "Submit" }) as HTMLButtonElement).type).toBe("submit");
  });

  it.each([
    { disabled: false, calls: 1 },
    { disabled: true, calls: 0 },
  ])("fires onClick $calls time(s) when disabled=$disabled", async ({ disabled, calls }) => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <Button iconOnly aria-label="Expand" disabled={disabled} onClick={onClick}>
        {glyph}
      </Button>,
    );
    await user.click(screen.getByRole("button", { name: "Expand" }));
    expect(onClick).toHaveBeenCalledTimes(calls);
  });

  it("leaks no aria-pressed onto a plain button", () => {
    render(
      <Button iconOnly aria-label="Expand">
        {glyph}
      </Button>,
    );
    const btn = screen.getByRole("button", { name: "Expand" });
    expect(btn).not.toHaveAttribute("aria-pressed");
    expect(screen.queryByRole("button", { name: "Expand", pressed: false })).toBeNull();
    expect(screen.queryByRole("button", { name: "Expand", pressed: true })).toBeNull();
  });

  it.each([{ pressed: true as const }, { pressed: false as const }])(
    "reflects aria-pressed=$pressed for a toggle",
    ({ pressed }) => {
      render(
        <Button iconOnly aria-label="Mute" variant="secondary" fill="ghost" aria-pressed={pressed}>
          {glyph}
        </Button>,
      );
      expect(screen.getByRole("button", { name: "Mute", pressed })).toBeInTheDocument();
      // The opposite state must not also match, which guards a pinned attribute.
      expect(screen.queryByRole("button", { name: "Mute", pressed: !pressed })).toBeNull();
    },
  );

  it("goes busy while loading and swallows the click", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        Save
      </Button>,
    );
    const btn = screen.getByRole("button", { name: /save/i });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("aria-busy", "true");
    // A state that only disables but shows nothing reads as a dead button, not a busy one.
    expect(btn.querySelector(".alk-btn__icon .alk-spinner")).not.toBeNull();
    expect(btn.querySelector(".alk-btn__label")).not.toBeNull();
    await user.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("loading replaces leftSection and drops rightSection", () => {
    render(
      <Button
        loading
        leftSection={<span data-testid="left">L</span>}
        rightSection={<span data-testid="right">R</span>}
      >
        Save
      </Button>,
    );
    // The spinner stands in for the leading content, so the caller's section is gone, not doubled.
    expect(screen.queryByTestId("left")).toBeNull();
    // No trailing affordance mid-flight.
    expect(screen.queryByTestId("right")).toBeNull();
    expect(screen.getByRole("button", { name: /save/i }).querySelectorAll(".alk-spinner")).toHaveLength(1);
  });

  it("wraps each section in an icon slot around the label", () => {
    render(
      <Button leftSection={<span data-testid="left">L</span>} rightSection={<span data-testid="right">R</span>}>
        Save
      </Button>,
    );
    const btn = screen.getByRole("button", { name: /save/i });
    expect(screen.getByTestId("left").closest(".alk-btn__icon")).not.toBeNull();
    expect(screen.getByTestId("right").closest(".alk-btn__icon")).not.toBeNull();
    expect(btn.querySelector(".alk-btn__label")).not.toBeNull();
    expect(btn.querySelector(".alk-spinner")).toBeNull();
  });

  it("a loading icon-only button shows a bare spinner", () => {
    render(
      <Button iconOnly aria-label="Refresh" loading>
        {glyph}
      </Button>,
    );
    const btn = screen.getByRole("button", { name: "Refresh" });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByTestId("glyph")).toBeNull();
    expect(btn.querySelectorAll(".alk-spinner")).toHaveLength(1);
    // Its own branch: routing it through the text branch would wrap the spinner and emit a label.
    expect(btn.querySelector(".alk-btn__label")).toBeNull();
    expect(btn.querySelector(".alk-btn__icon")).toBeNull();
  });
});

describe("an icon-only button annotates itself", () => {
  // A glyph says nothing to a sighted reader who does not know the icon, so the
  // name it already carries for a screen reader becomes the tip as well — and
  // the two can never disagree, because there is only one string.
  it("becomes its own tooltip from the name it already has", () => {
    render(
      <Button iconOnly aria-label="Copy link">
        {glyph}
      </Button>,
    );
    expect(screen.getByRole("button", { name: "Copy link" })).toHaveAttribute("title", "Copy link");
  });

  it("leaves a caller's own title alone", () => {
    render(
      <Button iconOnly aria-label="Copy link" title="Copy a link only people with access can open">
        {glyph}
      </Button>,
    );
    expect(screen.getByRole("button", { name: "Copy link" })).toHaveAttribute(
      "title",
      "Copy a link only people with access can open",
    );
  });

  it("stands aside for a Tooltip, so a reader is not annotated twice", () => {
    // `data-tip` is what the Tooltip primitive marks its trigger with.
    render(
      <Button iconOnly aria-label="Copy link" data-tip="">
        {glyph}
      </Button>,
    );
    expect(screen.getByRole("button", { name: "Copy link" })).not.toHaveAttribute("title");
  });

  it("puts no title on a button that already reads as words", () => {
    render(<Button>Save changes</Button>);
    expect(screen.getByRole("button", { name: "Save changes" })).not.toHaveAttribute("title");
  });
});
