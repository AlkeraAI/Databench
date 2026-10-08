// A menu row that destroys something says so before it is pressed. jsdom
// neither computes :hover states nor resolves var() chains, so the danger ink
// is proven by the pixel pass in a real window. What is reachable here is that
// the destructive row still selects and dismisses like any other.

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MenuRow, OverflowGlyph, PopMenu } from "@alkera/ui";

async function openMenu(): Promise<void> {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Fixture actions" }));
  await screen.findByRole("menu");
}

function renderMenu(onDelete: () => void = () => {}): void {
  render(
    <div className="chat-root">
      <PopMenu label="Fixture actions" glyph={<OverflowGlyph />}>
        {(close) => (
          <>
            <MenuRow icon={<svg data-testid="open-glyph" />} label="Open the fixture chat" id="open" onSelect={close} />
            <MenuRow
              icon={<svg data-testid="delete-glyph" />}
              label="Delete the fixture chat"
              id="delete"
              danger
              onSelect={() => {
                onDelete();
                close();
              }}
            />
          </>
        )}
      </PopMenu>
    </div>,
  );
}

describe("menu danger row", () => {
  it("selects and dismisses on a press like any other row", async () => {
    const onDelete = vi.fn();
    renderMenu(onDelete);
    const user = userEvent.setup();
    await openMenu();
    await user.click(screen.getByRole("menuitem", { name: "Delete the fixture chat" }));
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
  });
});
