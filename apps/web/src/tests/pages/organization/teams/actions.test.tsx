import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActionsMenu, type MenuItem } from "@/pages/organization/teams/detail/actions";

// The roster/header overflow menu now drives the base DropdownItem's danger/disabled props straight
// off its MenuItem[] registry (the page-local danger/disabled row components were deleted). These
// pin the observable contract of that mapping through the real menu: a destructive item still fires
// and closes; a disabled item renders but is inert (no onClick, no dismiss) and is aria-disabled; a
// `separated` item draws a divider before it. No class-name assertions — only what a user / AT sees.

afterEach(cleanup);

// Test-only fixtures — these strings are owned by the test, not copied from the component.
const MENU_LABEL = "Actions for Marcus";
const MOVE = "Move to team";
const REMOVE = "Remove from team";
const VIEW = "View usage (unavailable)";

/** Open the overflow menu and return its live panel. */
async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: MENU_LABEL }));
  return screen.getByRole("menu", { name: MENU_LABEL });
}

function panel(): HTMLElement | null {
  return screen.queryByRole("menu", { name: MENU_LABEL });
}

describe("ActionsMenu — registry → DropdownItem mapping", () => {
  it("renders every registry item's label, and a `separated` item draws a divider before it", async () => {
    const user = userEvent.setup();
    const items: MenuItem[] = [
      { key: "move", label: MOVE, icon: "move", onClick: vi.fn() },
      { key: "remove", label: REMOVE, icon: "trash", danger: true, separated: true, onClick: vi.fn() },
    ];
    render(<ActionsMenu items={items} label={MENU_LABEL} />);
    const menu = await openMenu(user);
    expect(screen.getByRole("menuitem", { name: MOVE })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: REMOVE })).toBeInTheDocument();
    // The separator precedes the destructive row it was declared on.
    const separator = menu.querySelector('[role="separator"]');
    expect(separator).not.toBeNull();
    const removeRow = screen.getByRole("menuitem", { name: REMOVE });
    expect(separator!.compareDocumentPosition(removeRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("a destructive (danger) item still fires its action and closes the menu", async () => {
    const user = userEvent.setup();
    const onRemove = vi.fn();
    const items: MenuItem[] = [{ key: "remove", label: REMOVE, icon: "trash", danger: true, onClick: onRemove }];
    render(<ActionsMenu items={items} label={MENU_LABEL} />);
    await openMenu(user);
    const row = screen.getByRole("menuitem", { name: REMOVE });
    // danger tints the row but leaves it a live command.
    expect(row).not.toBeDisabled();
    await user.click(row);
    expect(onRemove).toHaveBeenCalledOnce();
    expect(panel()).toBeNull();
  });

  it("a disabled item renders, is aria-disabled, and is INERT — no onClick, no dismiss", async () => {
    // `pointerEventsCheck: 0` forces the click through the native disabled guard, so the assertion
    // proves the component's own inert-on-disabled behavior, not merely the browser refusing a
    // disabled <button>. A wrong impl that forgot to map `item.disabled` onto DropdownItem fails
    // here on both onView (fires) and panel (dismisses).
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const onView = vi.fn();
    const onMove = vi.fn();
    const items: MenuItem[] = [
      { key: "plan", label: VIEW, icon: "usage", disabled: true, onClick: onView },
      { key: "move", label: MOVE, icon: "move", onClick: onMove },
    ];
    render(<ActionsMenu items={items} label={MENU_LABEL} />);
    await openMenu(user);
    const disabledRow = screen.getByRole("menuitem", { name: VIEW });
    expect(disabledRow).toHaveAttribute("aria-disabled", "true");
    expect(disabledRow).toBeDisabled();

    await user.click(disabledRow);
    expect(onView).not.toHaveBeenCalled();
    expect(panel()).not.toBeNull(); // a disabled click must not dismiss the menu

    // The enabled sibling in the same menu still works — the disabled item didn't gate the set.
    await user.click(screen.getByRole("menuitem", { name: MOVE }));
    expect(onMove).toHaveBeenCalledOnce();
    expect(panel()).toBeNull();
  });
});
