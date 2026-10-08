// A URL primitive renders text someone else wrote. Its contract is that a
// string that may not become a navigation does not become one here either —
// whether it would have been an href or a call into the host.

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { UrlLink } from "./UrlLink";

describe("UrlLink", () => {
  it("links an http(s) URL out of the page with no opener and no referrer", () => {
    render(<UrlLink url="https://app.example.com/x">the page</UrlLink>);
    const anchor = screen.getByRole("link", { name: "the page" });
    expect(anchor).toHaveAttribute("href", "https://app.example.com/x");
    expect(anchor).toHaveAttribute("target", "_blank");
    expect(anchor.getAttribute("rel")).toContain("noopener");
    expect(anchor.getAttribute("rel")).toContain("noreferrer");
  });

  it.each([
    ["javascript:alert(1)"],
    ["JavaScript:alert(1)"],
    ["data:text/html,<script>alert(1)</script>"],
    ["vbscript:msgbox(1)"],
    ["file:///etc/passwd"],
    ["https://user:secret@evil.example/"],
  ])("renders %s as text rather than an href", (url) => {
    const { container } = render(<UrlLink url={url}>{url}</UrlLink>);
    expect(container.querySelector("a")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(url)).toBeInTheDocument();
  });

  it("does not dress a refused URL as something that can be opened", () => {
    render(<UrlLink url="javascript:alert(1)">javascript:alert(1)</UrlLink>);
    const inert = screen.getByText("javascript:alert(1)");
    expect(inert).toHaveAttribute("data-inert");
    expect(inert).not.toHaveAttribute("role");
    expect(inert).not.toHaveAttribute("tabindex");
  });

  it("stops a click on a refused URL reaching the card around it", async () => {
    const onCardClick = vi.fn();
    render(
      <div onClick={onCardClick}>
        <UrlLink url="javascript:alert(1)">the target</UrlLink>
      </div>,
    );
    await userEvent.click(screen.getByText("the target"));
    expect(onCardClick).not.toHaveBeenCalled();
  });

  it("does not hand a refused URL to the host either", async () => {
    const onOpen = vi.fn();
    render(
      <UrlLink url="javascript:alert(1)" onOpen={onOpen}>
        click me
      </UrlLink>,
    );
    expect(screen.queryByRole("link")).toBeNull();
    await userEvent.click(screen.getByText("click me"));
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("hands an admitted URL to the host instead of navigating", async () => {
    const onOpen = vi.fn();
    render(
      <UrlLink url="https://app.example.com/x" onOpen={onOpen}>
        click me
      </UrlLink>,
    );
    await userEvent.click(screen.getByRole("link", { name: "click me" }));
    expect(onOpen).toHaveBeenCalledWith("https://app.example.com/x");
  });
});
