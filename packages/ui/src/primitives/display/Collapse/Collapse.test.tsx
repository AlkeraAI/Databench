import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Collapse } from "./Collapse";

afterEach(cleanup);

function root(): HTMLElement {
  const el = document.querySelector<HTMLElement>(".alk-collapse");
  if (!el) throw new Error("no collapse");
  return el;
}

describe("Collapse", () => {
  it("keeps closed content mounted but inert", () => {
    // The children stay in the tree for the height transition, so `inert` is what keeps Tab and
    // clicks out of a collapsed region.
    const { rerender } = render(
      <Collapse open={false}>
        <button type="button">deep link</button>
      </Collapse>,
    );
    const inner = document.querySelector<HTMLElement>(".alk-collapse__inner")!;
    expect(root()).toHaveAttribute("data-state", "closed");
    expect(document.querySelector("button")).not.toBeNull();
    expect(inner.hasAttribute("inert")).toBe(true);

    rerender(
      <Collapse open>
        <button type="button">deep link</button>
      </Collapse>,
    );
    expect(root()).toHaveAttribute("data-state", "open");
    expect(inner.hasAttribute("inert")).toBe(false);
  });

  it("forwards className and data attributes to the root", () => {
    render(
      <Collapse open className="mine" data-testid="c">
        x
      </Collapse>,
    );
    expect(root().className).toContain("mine");
    expect(root()).toHaveAttribute("data-testid", "c");
  });
});
