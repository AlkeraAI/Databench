import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CodeBlock } from "./CodeBlock";

const JSON_SRC = '{\n  "name": "alkera",\n  "count": 42,\n  "ok": true\n}';

afterEach(cleanup);

describe("CodeBlock", () => {
  it("gives every JSON token kind its own class", () => {
    const { container } = render(<CodeBlock language="json" code={JSON_SRC} />);
    expect(container.querySelector(".alk-tok--property")).not.toBeNull(); // "name" (a key)
    expect(container.querySelector(".alk-tok--string")).not.toBeNull(); // "alkera" (a value)
    expect(container.querySelector(".alk-tok--number")).not.toBeNull(); // 42
    expect(container.querySelector(".alk-tok--boolean")).not.toBeNull(); // true
    // The full source survives tokenisation verbatim (no dropped characters).
    expect(container.textContent).toContain("alkera");
    expect(container.textContent).toContain("42");
  });

  it("renders a line-number gutter by default and drops it when lineNumbers is false", () => {
    const { container, rerender } = render(<CodeBlock language="json" code={JSON_SRC} />);
    const gutter = container.querySelectorAll(".alk-codeblock__n");
    expect(gutter.length).toBe(5); // one per line
    rerender(<CodeBlock language="json" code={JSON_SRC} lineNumbers={false} />);
    expect(container.querySelector(".alk-codeblock__n")).toBeNull();
    expect(container.querySelector(".alk-codeblock--flush")).not.toBeNull();
  });

  it("renders unknown / absent grammars as plain ink with no token spans", () => {
    const { container } = render(<CodeBlock code={"just some plain text"} />);
    expect(container.textContent).toContain("just some plain text");
    expect(container.querySelector('[class^="alk-tok--"]')).toBeNull();
  });

  it("infers the grammar from a file path when no explicit language is given", () => {
    const { container } = render(<CodeBlock path="query.sql" code={"SELECT * FROM t"} lineNumbers={false} />);
    expect(container.querySelector(".alk-tok--keyword")).not.toBeNull(); // SELECT / FROM
  });

  it("has no copy button by default, and copies the code when one is shown", () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", { value: { writeText }, configurable: true });

    const { rerender } = render(<CodeBlock language="yaml" code={"on: push"} />);
    expect(screen.queryByRole("button", { name: /copy code/i })).toBeNull();

    rerender(<CodeBlock language="yaml" code={"on: push"} copyButton="always" />);
    fireEvent.click(screen.getByRole("button", { name: /copy code/i }));
    expect(writeText).toHaveBeenCalledWith("on: push");
  });
});
