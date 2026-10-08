// The trail a stacked page carries instead of a back button, and the chat
// header that carries the same trail. The contract that matters: the last crumb
// is where you are, so it reads as text and never as a control, while every
// ancestor the host can navigate to is one press away.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Breadcrumbs, Chrome, type Crumb } from "@alkera/ui";

afterEach(cleanup);

// A truncation tip is measured off the live element (`isOverflowing`), and jsdom reports
// every layout box as zero, so a crumb there is never cut off. Stamping the two widths is
// the only way to reach either branch.
function measure(el: HTMLElement, box: { scrollWidth: number; clientWidth: number }): void {
  for (const [prop, value] of Object.entries(box)) {
    Object.defineProperty(el, prop, { configurable: true, value });
  }
}

function trail(): { crumbs: Crumb[]; goes: [() => void, () => void] } {
  const goHome = vi.fn();
  const goChat = vi.fn();
  return {
    crumbs: [
      { label: "Chats", onGo: goHome },
      { label: "Data audit", onGo: goChat },
      { label: "Plan" },
    ],
    goes: [goHome, goChat],
  };
}

describe("Breadcrumbs", () => {
  it("renders nothing for an empty trail", () => {
    const { container } = render(<Breadcrumbs crumbs={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("keeps the current level text, even handed an onGo", async () => {
    const user = userEvent.setup();
    const onGo = vi.fn();
    render(<Breadcrumbs crumbs={[{ label: "Chats", onGo }, { label: "Plan", onGo }]} />);
    const here = screen.getByText("Plan");
    expect(here).toHaveAttribute("aria-current", "page");
    expect(here.tagName).not.toBe("BUTTON");
    expect(screen.queryByRole("button", { name: "Plan" })).not.toBeInTheDocument();
    await user.click(here);
    expect(onGo).not.toHaveBeenCalled();
  });

  it("fires exactly the clicked ancestor's onGo", async () => {
    const user = userEvent.setup();
    const { crumbs, goes } = trail();
    const [goHome, goChat] = goes;
    render(<Breadcrumbs crumbs={crumbs} />);
    await user.click(screen.getByRole("button", { name: "Data audit" }));
    expect(goChat).toHaveBeenCalledTimes(1);
    expect(goHome).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Chats" }));
    expect(goHome).toHaveBeenCalledTimes(1);
    expect(goChat).toHaveBeenCalledTimes(1);
  });

  it("leaves an ancestor without onGo as plain text", () => {
    render(<Breadcrumbs crumbs={[{ label: "Chats" }, { label: "Plan" }]} />);
    const unreachable = screen.getByText("Chats");
    expect(unreachable.tagName).not.toBe("BUTTON");
    expect(unreachable).not.toHaveAttribute("aria-current");
    expect(screen.getByText("Plan")).toHaveAttribute("aria-current", "page");
  });

  it("hands back the full label of a crumb that is cut off", async () => {
    const { crumbs } = trail();
    render(<Breadcrumbs crumbs={crumbs} />);
    const cut = screen.getByRole("button", { name: "Data audit" });
    const fits = screen.getByRole("button", { name: "Chats" });
    measure(cut, { scrollWidth: 420, clientWidth: 60 });
    measure(fits, { scrollWidth: 60, clientWidth: 60 });

    // Both crumbs are hovered, the fitting one first, so its open delay expires no
    // later than the cut one's. The tip that does arrive is therefore the clock the
    // absent one is judged against — no sleep, no pinned delay.
    fireEvent.pointerEnter(fits);
    fireEvent.pointerEnter(cut);

    await waitFor(() => expect(cut).toHaveAttribute("aria-describedby"));
    const tip = document.getElementById(cut.getAttribute("aria-describedby") ?? "");
    expect(tip).toHaveAttribute("role", "tooltip");
    expect(tip).toHaveTextContent("Data audit");

    // The valuable half: a crumb the reader can already read whole offers nothing.
    expect(fits).not.toHaveAttribute("aria-describedby");
    expect(screen.getAllByRole("tooltip")).toHaveLength(1);
  });
});

describe("Chrome carrying a trail", () => {
  it("reaches the parent chat in one press, and carries no back arrow", async () => {
    const user = userEvent.setup();
    const goParent = vi.fn();
    render(
      <Chrome
        title="Chat"
        trail={[{ label: "Data audit", onGo: goParent }, { label: "code-reviewer" }]}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Data audit" }));
    expect(goParent).toHaveBeenCalledTimes(1);
    // The trail is the whole way up: it names every level and reaches any of
    // them in one press, so an arrow that only climbs one rung had nothing to
    // add beside it.
    expect(screen.queryByRole("button", { name: "Back" })).not.toBeInTheDocument();

    // The trail names this level, so the placeholder title the chat list cannot
    // supply for a subagent session never reaches the header.
    expect(screen.getByRole("heading", { name: "code-reviewer" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByText("Chat")).not.toBeInTheDocument();
  });

  it("reads as a plain title when nothing is above it", () => {
    render(<Chrome title="unused" trail={[{ label: "Data audit" }]} />);
    expect(screen.getByRole("heading", { name: "Data audit" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Back" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Data audit" })).not.toBeInTheDocument();
  });
});
