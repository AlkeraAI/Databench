import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { JSON_CHILD_LIMIT, JsonTree } from "./JsonTree";

describe("JsonTree", () => {
  const value = { name: "alkera", tags: ["a", "b"], nested: { deep: { flag: true } }, nothing: null, n: 3 };

  it("opens the top level and leaves deeper containers closed", () => {
    render(<JsonTree value={value} />);
    expect(screen.getByRole("button", { name: /Object\(5 keys\)/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText('"alkera"')).toBeInTheDocument();
    expect(screen.getByText("null")).toBeInTheDocument();
    const tags = screen.getByRole("button", { name: /tags: Array\(2\)/ });
    expect(tags).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText('"a"')).toBeNull();
  });

  it("expands and collapses with a click", async () => {
    const user = userEvent.setup();
    render(<JsonTree value={value} />);
    const tags = screen.getByRole("button", { name: /tags: Array\(2\)/ });
    await user.click(tags);
    expect(tags).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText('"a"')).toBeInTheDocument();
    await user.click(tags);
    expect(tags).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText('"a"')).toBeNull();
  });

  it("toggles from the keyboard", async () => {
    const user = userEvent.setup();
    render(<JsonTree value={value} />);
    const nested = screen.getByRole("button", { name: /nested: Object\(1 key\)/ });
    nested.focus();
    await user.keyboard("{Enter}");
    expect(nested).toHaveAttribute("aria-expanded", "true");
    const deep = screen.getByRole("button", { name: /deep: Object\(1 key\)/ });
    deep.focus();
    await user.keyboard(" ");
    expect(screen.getByText("true")).toBeInTheDocument();
  });

  it("draws a scalar root", () => {
    render(<JsonTree value={42} />);
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("shows long arrays in steps", async () => {
    const user = userEvent.setup();
    const long = Array.from({ length: JSON_CHILD_LIMIT + 5 }, (_, i) => i);
    const { container } = render(<JsonTree value={long} />);
    const list = container.querySelector(".nb-json-children") as HTMLElement;
    expect(list.querySelectorAll(":scope > li .nb-json-number")).toHaveLength(JSON_CHILD_LIMIT);
    await user.click(within(list).getByRole("button", { name: "Show 5 more" }));
    expect(list.querySelectorAll(":scope > li .nb-json-number")).toHaveLength(JSON_CHILD_LIMIT + 5);
  });
});
