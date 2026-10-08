import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PageError } from "./PageError";

// The one plate a page shows when its data does not arrive. The whole point of hoisting it here is
// that every page says the same thing the same way, so the tests pin the SHAPE a reader sees:
// one headline naming what failed, at most one sentence, and one action that re-runs the read.

afterEach(cleanup);

describe("PageError", () => {
  it("names what failed in the headline", () => {
    render(<PageError of="your workspace" />);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load your workspace");
  });

  it("carries the caller's sentence, and a default when there is none", () => {
    const { unmount } = render(<PageError of="teams" message="The server is rate-limiting you." />);
    expect(screen.getByRole("alert")).toHaveTextContent("The server is rate-limiting you.");
    unmount();
    render(<PageError of="teams" />);
    expect(screen.getByRole("alert")).toHaveTextContent("The request did not reach the server.");
  });

  it("runs the retry on click, and offers no action without one", async () => {
    const retry = vi.fn();
    const { unmount } = render(<PageError of="teams" onRetry={retry} />);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledTimes(1);
    unmount();
    render(<PageError of="teams" />);
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("says when a retry is only worth making later", () => {
    render(<PageError of="teams" retryAfterSeconds={45} onRetry={() => {}} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Try again in 45 seconds.");
  });

  it("tucks the raw failure under a disclosure rather than the headline", () => {
    render(<PageError of="teams" details="could not load teams (500)" />);
    expect(screen.getByText("could not load teams (500)")).toBeInTheDocument();
    expect(screen.getByRole("alert").textContent).not.toMatch(/^could not load teams/);
  });

  it("weights down to an in-card plate without changing what it says", () => {
    render(<PageError of="sign-in methods" size="md" />);
    expect(document.querySelector(".alk-emptystate")).toHaveAttribute("data-size", "md");
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load sign-in methods");
  });
});
