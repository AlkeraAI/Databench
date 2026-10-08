import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StackedPage } from "./StackedPage";

// The in-document detail-page scaffold: chrome row (optional back, title,
// optional subtitle, optional actions) over the scrolling body.

afterEach(cleanup);

describe("StackedPage", () => {
  it("renders the title, subtitle and body, and nothing else", () => {
    const { rerender } = render(<StackedPage title="Cost ledger">body-content</StackedPage>);
    expect(screen.getByText("Cost ledger")).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveTextContent("body-content");
    // No back button and no subtitle unless the host asks for them.
    expect(screen.queryByRole("button")).not.toBeInTheDocument();

    rerender(
      <StackedPage title="Blob" subtitle="urn:blob:42">
        x
      </StackedPage>,
    );
    expect(screen.getByText("urn:blob:42")).toBeInTheDocument();
  });

  it("the back button needs onBack and takes backLabel", async () => {
    const user = userEvent.setup();
    const onBack = vi.fn();
    const { rerender } = render(
      <StackedPage title="Plan" onBack={onBack}>
        x
      </StackedPage>,
    );
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(onBack).toHaveBeenCalledTimes(1);

    rerender(
      <StackedPage title="Plan" onBack={onBack} backLabel="Back to chat">
        x
      </StackedPage>,
    );
    expect(screen.getByRole("button", { name: "Back to chat" })).toBeInTheDocument();
  });

  it("renders the actions slot in the chrome row, not the body", () => {
    render(
      <StackedPage title="Results" actions={<button type="button">Export</button>}>
        x
      </StackedPage>,
    );
    const action = screen.getByRole("button", { name: "Export" });
    expect(action).toBeInTheDocument();
    expect(screen.getByRole("main")).not.toContainElement(action);
  });

  // A trail is not a way out, so the back button still waits on onBack and
  // stays beside the trail.
  describe("nav slot", () => {
    it("the nav slot names the page in place of the title and subtitle", () => {
      render(
        <StackedPage title="Plan" subtitle="urn:plan:1" nav={<nav aria-label="Trail">trail</nav>}>
          body
        </StackedPage>,
      );
      expect(screen.getByRole("navigation", { name: "Trail" })).toBeInTheDocument();
      expect(screen.queryByText("Plan")).not.toBeInTheDocument();
      expect(screen.queryByText("urn:plan:1")).not.toBeInTheDocument();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
    });

    it("the trail rides beside the back button, still the one press up a level", async () => {
      const user = userEvent.setup();
      const onBack = vi.fn();
      render(
        <StackedPage title="Plan" onBack={onBack} nav={<nav aria-label="Trail">trail</nav>}>
          body
        </StackedPage>,
      );
      expect(screen.getByRole("navigation", { name: "Trail" })).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Back" }));
      expect(onBack).toHaveBeenCalledTimes(1);
    });
  });
});
