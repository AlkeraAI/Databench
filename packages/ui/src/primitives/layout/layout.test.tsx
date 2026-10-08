import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Inline } from "./Inline";
import { Stack } from "./Stack";
import { resolveGap } from "./gap";

afterEach(cleanup);

describe("resolveGap", () => {
  it("maps a scale step to its space token and passes a string through", () => {
    expect(resolveGap(3)).toBe("var(--alkSpace3)");
    expect(resolveGap(0)).toBe("var(--alkSpace0)");
    expect(resolveGap("1rem")).toBe("1rem");
    expect(resolveGap(undefined)).toBeUndefined();
  });
});

describe("Stack", () => {
  it("renders a column and ALWAYS pins its own gap var (defaulted when omitted)", () => {
    const { rerender } = render(<Stack>x</Stack>);
    let node = document.querySelector<HTMLElement>(".alk-stack")!;
    expect(node.tagName).toBe("DIV");
    // No gap prop → the tight default is still pinned on the element, never left to inheritance.
    expect(node.style.getPropertyValue("--alk-stack-gap")).toBe("var(--alkSpace0)");
    rerender(
      <Stack gap={5} align="center" justify="space-between">
        x
      </Stack>,
    );
    node = document.querySelector<HTMLElement>(".alk-stack")!;
    expect(node.style.getPropertyValue("--alk-stack-gap")).toBe("var(--alkSpace5)");
    expect(node.style.alignItems).toBe("center");
    expect(node.style.justifyContent).toBe("space-between");
  });

  it("a nested gap-less Stack cannot inherit an ancestor's gap", () => {
    // --alk-stack-gap is a custom property and custom properties inherit, so a text stack nested in
    // a `<Stack gap={7}>` page column would read 16px instead of 2px unless every Stack pins its var.
    render(
      <Stack gap={7} data-testid="outer">
        <Stack data-testid="inner">x</Stack>
      </Stack>,
    );
    const outer = document.querySelector<HTMLElement>("[data-testid='outer']")!;
    const inner = document.querySelector<HTMLElement>("[data-testid='inner']")!;
    expect(outer.style.getPropertyValue("--alk-stack-gap")).toBe("var(--alkSpace7)");
    expect(inner.style.getPropertyValue("--alk-stack-gap")).toBe("var(--alkSpace0)");
  });

  it("renders as any element and forwards DOM props + className", () => {
    render(
      <Stack as="section" className="mine" aria-label="group">
        x
      </Stack>,
    );
    const node = document.querySelector<HTMLElement>(".alk-stack")!;
    expect(node.tagName).toBe("SECTION");
    expect(node.className).toContain("mine");
    expect(node).toHaveAttribute("aria-label", "group");
  });

  it("sets data-grow only when grow is asked (the flex:1 fill dial)", () => {
    const { rerender } = render(<Stack grow>x</Stack>);
    expect(document.querySelector(".alk-stack")).toHaveAttribute("data-grow", "");
    rerender(<Stack>x</Stack>);
    expect(document.querySelector(".alk-stack")).not.toHaveAttribute("data-grow");
  });
});

describe("Inline", () => {
  it("sets the gap var and pins nowrap only when asked", () => {
    const { rerender } = render(<Inline>x</Inline>);
    const node = () => document.querySelector<HTMLElement>(".alk-inline")!;
    // Left unset, the CSS default wraps.
    expect(node().style.flexWrap).toBe("");
    rerender(
      <Inline gap="1rem" wrap={false}>
        x
      </Inline>,
    );
    expect(node().style.getPropertyValue("--alk-inline-gap")).toBe("1rem");
    expect(node().style.flexWrap).toBe("nowrap");
  });

  it.each(["grow", "block"] as const)("sets data-%s only when asked", (flag) => {
    const { rerender } = render(<Inline {...{ [flag]: true }}>x</Inline>);
    expect(document.querySelector(".alk-inline")).toHaveAttribute(`data-${flag}`, "");
    rerender(<Inline>x</Inline>);
    expect(document.querySelector(".alk-inline")).not.toHaveAttribute(`data-${flag}`);
  });
});
