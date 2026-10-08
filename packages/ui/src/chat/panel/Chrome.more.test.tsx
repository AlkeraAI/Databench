// The header's three-dot menu, and what a host may put in it.
//
// The menu began as the width-of-last-resort home for the quick links: the rail
// below carries them, and only when the rail folds out entirely does the menu
// appear to hold them. That rule cannot hold for an ACTION — saving the chat as
// a template has no rail of its own to fold into, so a menu that stays hidden
// at every readable width is an action nobody can reach.
//
// So the seam is pinned from both sides: rows the host wires lead the menu and
// the menu is drawn whether or not a link exists, while a menu carrying links
// alone keeps the class that hides it until the rail is gone.

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Chrome } from "./Chrome";

const TITLE = "Yesterday's orders";
const LINKS = [{ id: "knowledge" as const, label: "Knowledge", count: 3 }];
const SAVE = "Save as template…";

/** The three-dot menu's own key. */
const moreKey = (): HTMLElement => screen.getByRole("button", { name: "More" });
const rows = (): string[] =>
  Array.from(document.querySelectorAll(".chat-menu__panel [role='menuitem']")).map(
    (row) => row.textContent ?? "",
  );

describe("a chrome the host wired no overflow actions into", () => {
  it("draws no menu at all when there are no links either", () => {
    render(<Chrome title={TITLE} onDeleteChat={() => {}} />);

    expect(screen.queryByRole("button", { name: "More" })).toBeNull();
  });

  it("keeps the fold-of-last-resort class when it carries links alone", () => {
    render(<Chrome title={TITLE} links={LINKS} onDeleteChat={() => {}} />);

    // The class the stylesheet hides until the rail has folded out. Without it
    // the same links would be on screen twice at every width.
    expect(moreKey().closest(".chat-menu")?.className).toContain("chat-chrome__more");
  });
});

describe("a chrome the host wired overflow actions into", () => {
  it("drops the hiding class, so the action has a home at every width", () => {
    render(
      <Chrome
        title={TITLE}
        links={LINKS}
        moreActions={[{ id: "save-template", label: SAVE, onSelect: () => {} }]}
        onDeleteChat={() => {}}
      />,
    );

    expect(moreKey().closest(".chat-menu")?.className).not.toContain("chat-chrome__more");
  });

  it("draws the menu even where the host wired no links", () => {
    render(
      <Chrome
        title={TITLE}
        moreActions={[{ id: "save-template", label: SAVE, onSelect: () => {} }]}
        onDeleteChat={() => {}}
      />,
    );

    fireEvent.click(moreKey());
    expect(rows()).toEqual([SAVE]);
  });

  it("puts the host's rows ahead of the links, after the chrome's own rename", () => {
    render(
      <Chrome
        title={TITLE}
        links={LINKS}
        moreActions={[{ id: "save-template", label: SAVE, onSelect: () => {} }]}
        onRenameTitle={async () => {}}
        onDeleteChat={() => {}}
      />,
    );

    fireEvent.click(moreKey());
    expect(rows()).toEqual(["Rename", SAVE, "Knowledge3"]);
  });

  it("hands the press to the host and closes the menu", () => {
    const onSelect = vi.fn();
    render(
      <Chrome
        title={TITLE}
        moreActions={[{ id: "save-template", label: SAVE, onSelect }]}
        onDeleteChat={() => {}}
      />,
    );

    fireEvent.click(moreKey());
    fireEvent.click(screen.getByRole("menuitem", { name: SAVE }));

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(document.querySelector(".chat-menu__panel")).toBeNull();
  });

  it("reads a destructive row in danger ink", () => {
    render(
      <Chrome
        title={TITLE}
        moreActions={[{ id: "wipe", label: "Wipe", danger: true, onSelect: () => {} }]}
        onDeleteChat={() => {}}
      />,
    );

    fireEvent.click(moreKey());
    const row = screen.getByRole("menuitem", { name: "Wipe" });
    expect(row.getAttribute("data-danger")).not.toBeNull();
  });
});

describe("the chrome's own rename row", () => {
  it("is absent where the host wired no rename, and the menu still carries the links", () => {
    render(<Chrome title={TITLE} links={LINKS} onDeleteChat={() => {}} />);

    fireEvent.click(moreKey());
    expect(rows()).toEqual(["Knowledge3"]);
  });

  it("swaps the heading for the field holding the whole name", async () => {
    render(<Chrome title={TITLE} onRenameTitle={async () => {}} onDeleteChat={() => {}} />);

    fireEvent.click(moreKey());
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));

    const field = (await screen.findByLabelText("Chat title")) as HTMLInputElement;
    expect(field.value).toBe(TITLE);
  });

  it("writes the retyped name through the host's own seam", async () => {
    const onRenameTitle = vi.fn(async () => {});
    render(<Chrome title={TITLE} onRenameTitle={onRenameTitle} onDeleteChat={() => {}} />);

    fireEvent.click(moreKey());
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    const field = await screen.findByLabelText("Chat title");
    fireEvent.change(field, { target: { value: "Last week's orders" } });
    fireEvent.keyDown(field, { key: "Enter" });

    expect(onRenameTitle).toHaveBeenCalledWith("Last week's orders");
  });
});

describe("the links in the menu", () => {
  it("still lead where the host says, carrying their count", () => {
    const onOpenLink = vi.fn();
    render(
      <Chrome
        title={TITLE}
        links={LINKS}
        moreActions={[{ id: "save-template", label: SAVE, onSelect: () => {} }]}
        onOpenLink={onOpenLink}
        onDeleteChat={() => {}}
      />,
    );

    fireEvent.click(moreKey());
    const panel = document.querySelector(".chat-menu__panel") as HTMLElement;
    fireEvent.click(within(panel).getByText("Knowledge"));

    expect(onOpenLink).toHaveBeenCalledWith("knowledge");
  });
});
