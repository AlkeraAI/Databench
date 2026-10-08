import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Popover } from "../Popover";
import { Submenu } from "./Submenu";

// The Submenu contract, driven as a person would: the row opens its flyout on hover, click and the
// keyboard; the keyboard moves into the flyout and back out of it; Escape closes the flyout alone;
// and inside a Popover a press on a flyout row is a press inside the menu, not outside it.

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const LABEL = "Move to";
const flyout = () => screen.queryByRole("menu", { name: LABEL });
const row = () => screen.getByRole("menuitem", { name: LABEL });

function Rows({ onPick = vi.fn() }: { onPick?: (name: string) => void }) {
  return (
    <>
      <button type="button" role="menuitem" disabled aria-current="true">
        Here
      </button>
      <button type="button" role="menuitem" onClick={() => onPick("Inbox")}>
        Inbox
      </button>
      <button type="button" role="menuitem" onClick={() => onPick("Archive")}>
        Archive
      </button>
    </>
  );
}

function InMenu({ onPick }: { onPick?: (name: string) => void }) {
  return (
    <div role="menu" aria-label="Actions">
      <button type="button" role="menuitem">
        Rename
      </button>
      <Submenu label={LABEL}>
        <Rows onPick={onPick} />
      </Submenu>
    </div>
  );
}

describe("Submenu: opening", () => {
  it("is closed until asked and says it has a menu", () => {
    render(<InMenu />);
    expect(flyout()).toBeNull();
    expect(row()).toHaveAttribute("aria-haspopup", "menu");
    expect(row()).toHaveAttribute("aria-expanded", "false");
  });

  it("opens on hover after a short dwell, without taking focus", async () => {
    const user = userEvent.setup();
    render(<InMenu />);
    await user.hover(row());
    await waitFor(() => expect(flyout()).toBeInTheDocument());
    expect(row()).toHaveAttribute("aria-expanded", "true");
    expect(document.activeElement).not.toBe(screen.getByRole("menuitem", { name: "Inbox" }));
  });

  it("does not open on a hover that only sweeps past", () => {
    vi.useFakeTimers();
    render(<InMenu />);
    fireEvent.pointerEnter(row(), { pointerType: "mouse" });
    fireEvent.pointerLeave(row(), { pointerType: "mouse" });
    act(() => vi.advanceTimersByTime(1000));
    expect(flyout()).toBeNull();
  });

  it("opens on a click, for touch", async () => {
    const user = userEvent.setup();
    render(<InMenu />);
    await user.click(row());
    expect(flyout()).toBeInTheDocument();
  });

  it.each(["{ArrowRight}", "{Enter}", " "])("opens on %s and moves focus to the first enabled row", async (key) => {
    const user = userEvent.setup();
    render(<InMenu />);
    row().focus();
    await user.keyboard(key);
    expect(flyout()).toBeInTheDocument();
    // "Here" is disabled, so focus skips it.
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Inbox" })));
  });
});

describe("Submenu: hover intent", () => {
  it("stays open while the pointer crosses from the row to the flyout", () => {
    vi.useFakeTimers();
    render(<InMenu />);
    fireEvent.pointerEnter(row(), { pointerType: "mouse" });
    act(() => vi.advanceTimersByTime(150));
    expect(flyout()).toBeInTheDocument();
    // Diagonal travel: off the row, across a sibling, then into the flyout inside the grace.
    fireEvent.pointerLeave(row(), { pointerType: "mouse" });
    fireEvent.pointerEnter(screen.getByRole("menuitem", { name: "Rename" }), { pointerType: "mouse" });
    act(() => vi.advanceTimersByTime(200));
    fireEvent.pointerEnter(flyout()!, { pointerType: "mouse" });
    act(() => vi.advanceTimersByTime(1000));
    expect(row()).toHaveAttribute("aria-expanded", "true");
  });

  it("closes once the pointer has left both the row and the flyout", () => {
    vi.useFakeTimers();
    render(<InMenu />);
    fireEvent.pointerEnter(row(), { pointerType: "mouse" });
    act(() => vi.advanceTimersByTime(150));
    fireEvent.pointerLeave(row(), { pointerType: "mouse" });
    act(() => vi.advanceTimersByTime(1000));
    expect(row()).toHaveAttribute("aria-expanded", "false");
  });
});

describe("Submenu: keyboard inside the flyout", () => {
  async function openByKeyboard() {
    const user = userEvent.setup();
    render(<InMenu />);
    row().focus();
    await user.keyboard("{ArrowRight}");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Inbox" })));
    return user;
  }

  it("walks the enabled rows with the arrows, wrapping", async () => {
    const user = await openByKeyboard();
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Archive" }));
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Inbox" }));
    await user.keyboard("{ArrowUp}");
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Archive" }));
  });

  it.each(["{ArrowLeft}", "{Escape}"])("%s closes the flyout and puts focus back on the row", async (key) => {
    const user = await openByKeyboard();
    await user.keyboard(key);
    expect(row()).toHaveAttribute("aria-expanded", "false");
    expect(document.activeElement).toBe(row());
  });
});

describe("Submenu: inside a Popover", () => {
  function PopoverMenu({ onPick, tick = 0 }: { onPick: (name: string) => void; tick?: number }) {
    return (
      <Popover label="Account" trigger={(p) => <button {...p} data-tick={tick}>account</button>}>
        {({ close }) => (
          <div role="menu" aria-label="Actions">
            <Submenu label={LABEL}>
              {/* A pick closes the whole menu, leaving the flyout to go with it. */}
              <Rows
                onPick={(name) => {
                  close();
                  onPick(name);
                }}
              />
            </Submenu>
          </div>
        )}
      </Popover>
    );
  }

  it("a press on a flyout row reaches the row instead of closing the popover first", async () => {
    const user = userEvent.setup();
    const onPick = vi.fn();
    render(<PopoverMenu onPick={onPick} />);
    await user.click(screen.getByRole("button", { name: "account" }));
    await user.click(row());
    await user.click(screen.getByRole("menuitem", { name: "Archive" }));
    expect(onPick).toHaveBeenCalledWith("Archive");
  });

  it("a press inside the flyout is not a press outside the popover", async () => {
    const user = userEvent.setup();
    render(<PopoverMenu onPick={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "account" }));
    await user.click(row());
    await user.click(flyout()!);
    expect(screen.getByRole("button", { name: "account" })).toHaveAttribute("aria-expanded", "true");
  });

  it("picking a flyout row by keyboard puts focus back on the popover's trigger", async () => {
    const user = userEvent.setup();
    render(<PopoverMenu onPick={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "account" }));
    row().focus();
    await user.keyboard("{ArrowRight}");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Inbox" })));
    await user.keyboard("{Enter}");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "account" }));
  });

  it("Escape in the flyout closes the flyout and leaves the popover open", async () => {
    const user = userEvent.setup();
    render(<PopoverMenu onPick={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "account" }));
    row().focus();
    await user.keyboard("{ArrowRight}");
    await user.keyboard("{Escape}");
    expect(row()).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("dialog", { name: "Account" })).toBeInTheDocument();
  });

  it("Escape still closes the flyout first after the popover re-renders", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<PopoverMenu onPick={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "account" }));
    row().focus();
    await user.keyboard("{ArrowRight}");
    rerender(<PopoverMenu onPick={vi.fn()} tick={1} />);
    await user.keyboard("{Escape}");
    expect(row()).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("dialog", { name: "Account" })).toBeInTheDocument();
  });

  it("a press outside both still closes the popover", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <PopoverMenu onPick={vi.fn()} />
        <button type="button">elsewhere</button>
      </div>,
    );
    await user.click(screen.getByRole("button", { name: "account" }));
    await user.click(row());
    await user.click(screen.getByRole("button", { name: "elsewhere" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Account" })).toBeNull());
  });
});
