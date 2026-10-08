// A crumb the header had to cut is still a name the reader has to be able to
// read. The trail's controls hand it back through the styled tip a keyboard can
// summon; the level you are on is not a control, so it hands it back the way
// the chat rail's own cut title does — as a native `title` the browser shows on
// hover whatever the crumb is rendered as.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Breadcrumbs } from "./Breadcrumbs";

afterEach(cleanup);

/** Longer than any header is wide, so the trail is certain to cut it. */
const LONG = "Reply with the single word OK and nothing else, then stop and wait";

describe("a crumb the trail cuts", () => {
  it("carries the full name of the level you are on", () => {
    render(<Breadcrumbs crumbs={[{ label: "Chats", onGo: vi.fn() }, { label: LONG }]} />);
    const here = screen.getByText(LONG);
    expect(here).toHaveAttribute("title", LONG);
    expect(here).toHaveAttribute("aria-current", "page");
  });

  it("carries it on the heading form too, where the trail stands in for the title", () => {
    render(<Breadcrumbs crumbs={[{ label: "Chats", onGo: vi.fn() }, { label: LONG }]} heading />);
    expect(screen.getByRole("heading", { name: LONG })).toHaveAttribute("title", LONG);
  });

  it("carries it on an ancestor the host cannot navigate to", () => {
    render(<Breadcrumbs crumbs={[{ label: LONG }, { label: "Plan" }]} />);
    const unreachable = screen.getByText(LONG);
    expect(unreachable.tagName).not.toBe("BUTTON");
    expect(unreachable).toHaveAttribute("title", LONG);
  });

  it("leaves the focusable steps on the styled tip, which carries no native title", () => {
    render(<Breadcrumbs crumbs={[{ label: LONG, onGo: vi.fn() }, { label: "Plan" }]} />);
    const step = screen.getByRole("button", { name: LONG });
    // Two tips on one hover is the bug the primitive drops `title` to avoid; a
    // control keeps the styled one because focus can reach it.
    expect(step).not.toHaveAttribute("title");
  });
});
