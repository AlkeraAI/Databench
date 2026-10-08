import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Text } from "./Text";

afterEach(cleanup);

function el(): HTMLElement {
  const node = document.querySelector<HTMLElement>(".alk-text");
  if (!node) throw new Error("no text");
  return node;
}

describe("Text", () => {
  it("renders a span by default and applies the variant's role class", () => {
    render(<Text variant="num">42</Text>);
    const node = el();
    expect(node.tagName).toBe("SPAN");
    expect(node.className).toContain("alk-num");
    expect(node.textContent).toBe("42");
  });

  it("renders as any element via `as`, forwarding DOM props", () => {
    render(
      <Text as="td" variant="meta" title="when">
        Jul 1
      </Text>,
    );
    const node = el();
    expect(node.tagName).toBe("TD");
    expect(node).toHaveAttribute("title", "when");
    expect(node.className).toContain("alk-meta");
  });

  it("layers tone orthogonally over the variant (a muted number)", () => {
    render(
      <Text variant="num" tone="muted">
        7
      </Text>,
    );
    const node = el();
    expect(node.className).toContain("alk-num");
    expect(node).toHaveAttribute("data-tone", "muted");
  });

  it("clamps to N lines, and truncating sets no clamp", () => {
    const { rerender } = render(<Text clamp={3}>long synopsis</Text>);
    expect(el().className).toContain("alk-clamp");
    expect(el().style.getPropertyValue("--alk-clamp")).toBe("3");

    rerender(
      <Text variant="name" truncate>
        Ada Lovelace
      </Text>,
    );
    expect(el().className).toContain("alk-truncate");
    expect(el().className).not.toContain("alk-clamp");
    expect(el().style.getPropertyValue("--alk-clamp")).toBe("");
  });

  it("merges a caller className and preserves a caller style", () => {
    render(
      <Text variant="body" className="mine" style={{ marginTop: 4 }}>
        x
      </Text>,
    );
    const node = el();
    expect(node.className).toContain("alk-body");
    expect(node.className).toContain("mine");
    expect(node.style.marginTop).toBe("4px");
  });

  it("omits the variant class and data-tone when neither is set (a plain wrapper)", () => {
    render(<Text>plain</Text>);
    const node = el();
    expect(node.className.trim()).toBe("alk-text");
    expect(node).not.toHaveAttribute("data-tone");
  });
});
