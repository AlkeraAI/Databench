import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { Popover } from "./Popover";
import { useEscLayer } from "../../../hooks";

// The Popover dismissal contract, driven as a user would: a click-mode popover closes on Escape
// and on an outside press; a hover-mode popover keeps outside-press OFF (pointer-leave owns that
// close) but still dismisses on Escape like every transient overlay. Escape routes through the
// shared stack, so one press closes exactly one surface — the topmost — and a layer registered
// beneath an open popover never fires on the same press.

afterEach(cleanup);

const PANEL_LABEL = "Trust legend"; // test-only fixture strings
const BODY_TEXT = "The four grades";

function clickPopover() {
  return (
    <Popover label={PANEL_LABEL} trigger={(p) => <button {...p}>legend</button>}>
      <span>{BODY_TEXT}</span>
    </Popover>
  );
}

function hoverPopover() {
  return (
    <Popover label={PANEL_LABEL} openOn="hover" trigger={(p) => <button {...p}>legend</button>}>
      <span>{BODY_TEXT}</span>
    </Popover>
  );
}

const panel = () => screen.queryByRole("dialog", { name: PANEL_LABEL });

describe("Popover — click mode dismissal", () => {
  it("closes on an outside press", async () => {
    const user = userEvent.setup();
    render(
      <div>
        {clickPopover()}
        <button>elsewhere</button>
      </div>,
    );
    await user.click(screen.getByRole("button", { name: "legend" }));
    expect(panel()).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "elsewhere" }));
    await waitFor(() => expect(panel()).toBeNull());
  });
});

describe("Popover — hover mode dismissal", () => {
  it("dismisses on Escape even when hover owns the pointer close", async () => {
    const user = userEvent.setup();
    render(hoverPopover());
    await user.hover(screen.getByRole("button", { name: "legend" }));
    await waitFor(() => expect(panel()).toBeInTheDocument());

    await user.keyboard("{Escape}");
    await waitFor(() => expect(panel()).toBeNull());
  });

  it("ignores an outside press — pointer-leave owns that close channel", async () => {
    const user = userEvent.setup();
    render(hoverPopover());
    await user.hover(screen.getByRole("button", { name: "legend" }));
    await waitFor(() => expect(panel()).toBeInTheDocument());

    // A bare outside mousedown (no pointer travel, no focus steal): the hover popover must not
    // close on it — the outside-press channel would fight the hover close (press-close, then a
    // re-hover instantly re-opens).
    fireEvent.mouseDown(document.body);
    expect(panel()).toBeInTheDocument();
  });
});

/** A page-layer probe: registers on the shared stack beneath any popover opened after it. */
function PageLayer({ onEscape }: { onEscape: () => void }) {
  useEscLayer(true, onEscape);
  return null;
}

describe("Escape stack — one press closes exactly one surface", () => {
  it("takes the Escape alone, leaving the next press to the page", async () => {
    const user = userEvent.setup();
    let pageEscapes = 0;
    render(
      <div>
        <PageLayer onEscape={() => (pageEscapes += 1)} />
        {clickPopover()}
      </div>,
    );
    await user.click(screen.getByRole("button", { name: "legend" }));
    expect(panel()).toBeInTheDocument();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(panel()).toBeNull());
    expect(pageEscapes).toBe(0);

    await act(async () => {
      await user.keyboard("{Escape}");
    });
    expect(pageEscapes).toBe(1);
  });
});

describe("Popover — focus returns to the trigger", () => {
  function menuPopover() {
    return (
      <div>
        <Popover label="Account" trigger={(p) => <button {...p}>account</button>}>
          {({ close }) => (
            <div role="menu" aria-label="Account">
              <button type="button" role="menuitem">
                Profile
              </button>
              <button type="button" role="menuitem" onClick={close}>
                Preferences
              </button>
            </div>
          )}
        </Popover>
        <button>elsewhere</button>
      </div>
    );
  }

  it("on Escape from inside the panel", async () => {
    const user = userEvent.setup();
    render(menuPopover());
    const trigger = screen.getByRole("button", { name: "account" });
    await user.click(trigger);
    screen.getByRole("menuitem", { name: "Profile" }).focus();

    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Account" })).toBeNull());
    expect(document.activeElement).toBe(trigger);
  });

  it("when a row inside the panel closes it", async () => {
    const user = userEvent.setup();
    render(menuPopover());
    const trigger = screen.getByRole("button", { name: "account" });
    await user.click(trigger);
    screen.getByRole("menuitem", { name: "Preferences" }).focus();

    await user.keyboard("{Enter}");

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Account" })).toBeNull());
    expect(document.activeElement).toBe(trigger);
  });

  it("but not when a press elsewhere closed it", async () => {
    const user = userEvent.setup();
    render(menuPopover());
    await user.click(screen.getByRole("button", { name: "account" }));
    const elsewhere = screen.getByRole("button", { name: "elsewhere" });

    await user.click(elsewhere);

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Account" })).toBeNull());
    expect(document.activeElement).toBe(elsewhere);
  });
});
