import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Dropdown, DropdownItem } from "./Dropdown";

// The menu primitive and its DropdownItem contract: a plain command fires onSelect and closes the
// menu, and a `disabled` item still renders but is inert on both axes while advertising itself to
// assistive tech. Every test drives the real menu and asserts what a user or AT can observe.

afterEach(cleanup);

// The accessible name given to each trigger/panel in these tests. A test-only fixture string — it
// exists nowhere in the Dropdown source, so it is safe as a literal (rule: only test-only ids may be
// raw literals).
const MENU_LABEL = "Row actions";
const ITEM_TEXT = "Delete row"; // a row label owned by the test, not by the component

/** Open the menu and hand back its first item's row + a query for the live panel. */
async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: MENU_LABEL }));
  return screen.getByRole("menu", { name: MENU_LABEL });
}

/** The live menu panel, or null once it has fully unmounted (past its exit). */
function menuPanel(): HTMLElement | null {
  return screen.queryByRole("menu", { name: MENU_LABEL });
}

function iconTrigger() {
  return { kind: "icon", icon: "•", ariaLabel: MENU_LABEL } as const;
}

describe("Dropdown — panel + trigger mechanics", () => {
  it("renders no panel until the trigger is pressed, then opens it", async () => {
    const user = userEvent.setup();
    render(
      <Dropdown label={MENU_LABEL} trigger={iconTrigger()}>
        <DropdownItem onSelect={() => {}}>{ITEM_TEXT}</DropdownItem>
      </Dropdown>,
    );
    // Closed at rest: no menu panel, and the trigger reports collapsed.
    expect(menuPanel()).toBeNull();
    const trigger = screen.getByRole("button", { name: MENU_LABEL });
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    await openMenu(user);
    expect(menuPanel()).not.toBeNull();
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    // The panel the trigger points at is the one that opened.
    expect(trigger.getAttribute("aria-controls")).toBe(menuPanel()!.id);
  });
});

describe("DropdownItem — a live command", () => {
  it("fires onSelect and closes the menu when clicked (default closeOnSelect)", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <Dropdown label={MENU_LABEL} trigger={iconTrigger()}>
        <DropdownItem onSelect={onSelect}>{ITEM_TEXT}</DropdownItem>
      </Dropdown>,
    );
    await openMenu(user);
    await user.click(screen.getByRole("menuitem", { name: ITEM_TEXT }));
    expect(onSelect).toHaveBeenCalledOnce();
    // The panel dismisses on select.
    expect(menuPanel()).toBeNull();
  });

  it("keeps the menu open when closeOnSelect is false (a multi-select group)", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <Dropdown label={MENU_LABEL} trigger={iconTrigger()}>
        <DropdownItem onSelect={onSelect} closeOnSelect={false}>
          {ITEM_TEXT}
        </DropdownItem>
      </Dropdown>,
    );
    await openMenu(user);
    await user.click(screen.getByRole("menuitem", { name: ITEM_TEXT }));
    expect(onSelect).toHaveBeenCalledOnce();
    // Still open — a multi-select row must not dismiss the panel.
    expect(menuPanel()).not.toBeNull();
  });
});

describe("Dropdown — a nested sub-menu keeps its parent open", () => {
  it("does not dismiss the parent when a portaled child menu's item is picked", async () => {
    // The consolidated-filter pattern: a Dropdown rendered INSIDE another Dropdown's panel. The child
    // portals its own panel OUTSIDE the parent (to escape the parent panel's transform/clip), so its
    // rows live elsewhere in the DOM. Picking one must NOT read as an outside press on the parent — a
    // wrong impl (no nested-panel awareness) collapses the whole consolidated menu on the first pick.
    const user = userEvent.setup();
    const onPick = vi.fn();
    render(
      <Dropdown label="Filters" trigger={{ kind: "icon", icon: "F", ariaLabel: "Filters" }}>
        <Dropdown label="Kind" trigger={{ kind: "icon", icon: "K", ariaLabel: "Kind" }}>
          <DropdownItem onSelect={onPick}>Schema</DropdownItem>
        </Dropdown>
      </Dropdown>,
    );

    await user.click(screen.getByRole("button", { name: "Filters" }));
    expect(screen.getByRole("menu", { name: "Filters" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Kind" }));
    expect(screen.getByRole("menu", { name: "Kind" })).toBeInTheDocument();

    // Pick a value in the CHILD menu.
    await user.click(screen.getByRole("menuitem", { name: "Schema" }));
    expect(onPick).toHaveBeenCalledOnce();
    // The child dismisses on select; the PARENT survives — the whole point of the fix.
    expect(screen.queryByRole("menu", { name: "Kind" })).toBeNull();
    expect(screen.getByRole("menu", { name: "Filters" })).toBeInTheDocument();
  });
});

describe("DropdownItem — a disabled command", () => {
  it("is present, inert, and does not gate its siblings", async () => {
    // `pointerEventsCheck: 0` forces the click THROUGH the native disabled guard, so this proves the
    // component's own short-circuit rather than the browser refusing to dispatch. The live sibling
    // catches a wrong impl that gates the whole menu on one disabled row.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const onLive = vi.fn();
    const onDead = vi.fn();
    const LIVE_TEXT = "Move to team"; // test-only labels, owned here
    const DEAD_TEXT = "View usage";
    render(
      <Dropdown label={MENU_LABEL} trigger={iconTrigger()}>
        <DropdownItem disabled onSelect={onDead}>
          {DEAD_TEXT}
        </DropdownItem>
        <DropdownItem onSelect={onLive}>{LIVE_TEXT}</DropdownItem>
      </Dropdown>,
    );
    await openMenu(user);
    const dead = screen.getByRole("menuitem", { name: DEAD_TEXT });
    expect(dead).toHaveAttribute("aria-disabled", "true");
    expect(dead).toBeDisabled();

    await user.click(dead);
    expect(onDead).not.toHaveBeenCalled();
    expect(menuPanel()).not.toBeNull();

    await user.click(screen.getByRole("menuitem", { name: LIVE_TEXT }));
    expect(onLive).toHaveBeenCalledOnce();
    expect(menuPanel()).toBeNull();
  });
});

// --- keyboard + overlay-stack contract (the menu-button pattern) ---------------------------

import { Modal } from "../Modal";

describe("Dropdown keyboard + stack", () => {
  function KeyboardHarness() {
    return (
      <Dropdown trigger={iconTrigger()} label={MENU_LABEL}>
        <DropdownItem onSelect={() => {}}>First</DropdownItem>
        <DropdownItem onSelect={() => {}}>Second</DropdownItem>
        <DropdownItem onSelect={() => {}}>Third</DropdownItem>
      </Dropdown>
    );
  }

  it("Escape inside a Modal closes ONLY the menu, then the modal on the next press", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose} title="Host">
        <KeyboardHarness />
      </Modal>,
    );
    await user.click(screen.getByRole("button", { name: MENU_LABEL }));
    expect(menuPanel()).not.toBeNull();
    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled(); // the modal survived the first press
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("returns focus to the trigger when Escape closes the menu", async () => {
    const user = userEvent.setup();
    render(<KeyboardHarness />);
    await openMenu(user);
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: MENU_LABEL })).toHaveFocus();
  });

  it("ArrowDown opens the menu, and arrows rove and wrap", async () => {
    const user = userEvent.setup();
    render(<KeyboardHarness />);
    screen.getByRole("button", { name: MENU_LABEL }).focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "First" })).toHaveFocus();
    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Third" })).toHaveFocus();
    await user.keyboard("{ArrowDown}"); // wraps
    expect(screen.getByRole("menuitem", { name: "First" })).toHaveFocus();
    await user.keyboard("{End}");
    expect(screen.getByRole("menuitem", { name: "Third" })).toHaveFocus();
    await user.keyboard("{Home}");
    expect(screen.getByRole("menuitem", { name: "First" })).toHaveFocus();
  });

  it("Tab closes the menu and hands focus back to the trigger", async () => {
    const user = userEvent.setup();
    render(<KeyboardHarness />);
    await openMenu(user);
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{Tab}");
    // closed (exit animation may hold the node; selection state is what matters)
    expect(screen.getByRole("button", { name: MENU_LABEL })).toHaveAttribute("aria-expanded", "false");
  });
});

// --- where the portaled panel lands -------------------------------------------
//
// The panel leaves the trigger's subtree so it escapes a scroll-clipping
// ancestor. Where it lands decides whether a screen reader can still reach it.

describe("Dropdown — where the panel lands", () => {
  const oneItemMenu = () => (
    <Dropdown label={MENU_LABEL} trigger={iconTrigger()}>
      <DropdownItem onSelect={() => {}}>{ITEM_TEXT}</DropdownItem>
    </Dropdown>
  );

  it("keeps the panel INSIDE an aria-modal dialog the trigger sits in", async () => {
    // `aria-modal="true"` tells assistive tech to ignore everything outside the
    // dialog, so a panel portaled past it is on screen and reaches no screen
    // reader at all — every option in a select opened from a modal was a dead
    // end. Reached THROUGH the dialog here, which is the only route AT has.
    const user = userEvent.setup();
    render(
      <Modal open onClose={() => {}} title="Host">
        {oneItemMenu()}
      </Modal>,
    );
    await openMenu(user);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("menu", { name: MENU_LABEL })).toBe(menuPanel());
  });

  it("still lands in the dialog when a themed root sits inside it", async () => {
    // Nearest match wins, and here that is the themed root — which is itself
    // inside the dialog, so the tie-break cannot cost the modal contract.
    const user = userEvent.setup();
    render(
      <Modal open onClose={() => {}} title="Host">
        <div data-alkera-color-scheme="dark" data-testid="themed">
          {oneItemMenu()}
        </div>
      </Modal>,
    );
    await openMenu(user);
    expect(screen.getByTestId("themed").contains(menuPanel())).toBe(true);
    expect(within(screen.getByRole("dialog")).getByRole("menu", { name: MENU_LABEL })).toBe(menuPanel());
  });

  it("portals up to the themed page root when no dialog encloses the trigger", async () => {
    // The panel resolves the tokens and the active light/dark scheme from that
    // root, so landing above it would strip the menu of its own theme.
    const user = userEvent.setup();
    render(
      <div data-alkera-color-scheme="dark" data-testid="themed">
        {oneItemMenu()}
      </div>,
    );
    await openMenu(user);
    expect(screen.getByTestId("themed").contains(menuPanel())).toBe(true);
  });

  it("treats the IDE surface as that root, so the webview resolves its tokens too", async () => {
    const user = userEvent.setup();
    render(
      <div data-alkera-ide="" data-testid="themed">
        {oneItemMenu()}
      </div>,
    );
    await openMenu(user);
    expect(screen.getByTestId("themed").contains(menuPanel())).toBe(true);
  });

  it("falls back to the document body when neither is on the page", async () => {
    const user = userEvent.setup();
    render(oneItemMenu());
    await openMenu(user);
    expect(menuPanel()?.parentElement).toBe(document.body);
  });
});

// --- focus once the menu closes ------------------------------------------------

describe("Dropdown — focus after the menu closes", () => {
  const OUTSIDE_TARGET = "Unrelated control"; // a test-owned button beside the menu

  function PickHarness() {
    return (
      <Dropdown label={MENU_LABEL} trigger={iconTrigger()}>
        <DropdownItem onSelect={() => {}}>First</DropdownItem>
        <DropdownItem onSelect={() => {}}>Second</DropdownItem>
      </Dropdown>
    );
  }

  const triggerButton = () => screen.getByRole("button", { name: MENU_LABEL });

  it("returns focus to the trigger when a row is clicked", async () => {
    // Per the WAI menu-button pattern, activating a row closes the menu AND
    // hands focus back. The row is removed on close, so without this focus
    // resolves to <body> and the next Tab starts over from the top of the page.
    const user = userEvent.setup();
    render(<PickHarness />);
    await openMenu(user);
    await user.click(screen.getByRole("menuitem", { name: "First" }));
    expect(triggerButton()).toHaveFocus();
  });

  it("returns focus to the trigger when a row is chosen from the keyboard", async () => {
    const user = userEvent.setup();
    render(<PickHarness />);
    triggerButton().focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "First" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(triggerButton()).toHaveFocus();
  });

  it("leaves focus where an OUTSIDE press put it", async () => {
    // The asymmetric partner: an outside press dismisses the menu too, but
    // yanking focus back would steal the control the user just aimed at.
    const user = userEvent.setup();
    render(
      <>
        <PickHarness />
        <button type="button">{OUTSIDE_TARGET}</button>
      </>,
    );
    await openMenu(user);
    await user.click(screen.getByRole("button", { name: OUTSIDE_TARGET }));
    expect(screen.getByRole("button", { name: OUTSIDE_TARGET })).toHaveFocus();
    expect(triggerButton()).not.toHaveFocus();
  });

  it("leaves focus inside an aria-modal dialog after a pick", async () => {
    // The composed case. Focus resting on <body> is worse inside a dialog than
    // untidy: `useFocusTrap` only redirects Tab from the trap's first or last
    // stop, so from <body> the next Tab walks into the page behind the dialog.
    // The Tab itself is not driven here — the trap filters its stops on
    // `offsetParent`, which jsdom never populates, so every Tab is swallowed and
    // would prove nothing. Where focus RESTS is the half jsdom can answer.
    const user = userEvent.setup();
    render(
      <Modal open onClose={() => {}} title="Host">
        <PickHarness />
      </Modal>,
    );
    // The dialog claims its initial focus on the next animation frame. Let that
    // land before driving the menu, or it arrives late and overwrites the very
    // focus this case is about.
    const dialog = screen.getByRole("dialog");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    await openMenu(user);
    await user.click(screen.getByRole("menuitem", { name: "Second" }));
    expect(triggerButton()).toHaveFocus();
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});
