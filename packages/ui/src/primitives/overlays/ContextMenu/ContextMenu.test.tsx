import { render, screen, fireEvent, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";

import { ContextMenu, useContextMenu, type ContextMenuItem } from "./ContextMenu";

/** A row that owns a context menu, exactly as a Files treegrid row will use it. */
function Harness({ items }: { items: ContextMenuItem[] }) {
  const menu = useContextMenu();
  return (
    <div>
      <div data-testid="row" role="row" tabIndex={0} {...menu.triggerProps}>
        report.csv
      </div>
      <button type="button">elsewhere</button>
      <ContextMenu {...menu.menuProps} items={items} label="Actions for report.csv" />
    </div>
  );
}

function baseItems(onOpen = vi.fn(), onRename = vi.fn()): ContextMenuItem[] {
  return [
    { id: "open", label: "Open", shortcut: "Enter", onSelect: onOpen },
    { id: "rename", label: "Rename", shortcut: "F2", onSelect: onRename },
    { id: "trash", label: "Move to trash", disabled: "You have read-only access to this folder" },
  ];
}

const rightClick = (el: HTMLElement) => fireEvent.contextMenu(el, { clientX: 120, clientY: 80 });

describe("ContextMenu — opening", () => {
  it("opens at the pointer on right-click", () => {
    render(<Harness items={baseItems()} />);
    expect(screen.queryByRole("menu")).toBeNull();

    rightClick(screen.getByTestId("row"));

    const menu = screen.getByRole("menu", { name: "Actions for report.csv" });
    expect(menu).toHaveStyle({ left: "120px", top: "80px" });
    expect(screen.getAllByRole("menuitem")).toHaveLength(3);
  });

  it("opens on Shift+F10 anchored at the focused element, not at (0, 0)", () => {
    render(<Harness items={baseItems()} />);
    const row = screen.getByTestId("row");
    row.getBoundingClientRect = () => ({ left: 40, bottom: 200, right: 240, top: 180 }) as DOMRect;
    row.focus();

    fireEvent.keyDown(row, { key: "F10", shiftKey: true });

    expect(screen.getByRole("menu")).toHaveStyle({ left: "40px", top: "200px" });
  });

  it("ignores F10 without Shift", () => {
    render(<Harness items={baseItems()} />);
    fireEvent.keyDown(screen.getByTestId("row"), { key: "F10" });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("anchors a keyboard-synthesised contextmenu (0, 0) to the focused element", () => {
    render(<Harness items={baseItems()} />);
    const row = screen.getByTestId("row");
    row.getBoundingClientRect = () => ({ left: 12, bottom: 34, right: 200, top: 20 }) as DOMRect;
    row.focus();

    fireEvent.contextMenu(row, { clientX: 0, clientY: 0 });

    expect(screen.getByRole("menu")).toHaveStyle({ left: "12px", top: "34px" });
  });

  it("gives DOM focus to the first row and makes only it tabbable", () => {
    render(<Harness items={baseItems()} />);
    rightClick(screen.getByTestId("row"));

    const [open, rename, trash] = screen.getAllByRole("menuitem");
    expect(open).toHaveFocus();
    expect(open).toHaveAttribute("tabindex", "0");
    expect(rename).toHaveAttribute("tabindex", "-1");
    expect(trash).toHaveAttribute("tabindex", "-1");
  });
});

describe("ContextMenu — keyboard", () => {
  const openMenu = (items: ContextMenuItem[]) => {
    render(<Harness items={items} />);
    rightClick(screen.getByTestId("row"));
    return screen.getByRole("menu");
  };

  it("ArrowDown and ArrowUp move the roving focus and wrap at both ends", () => {
    const menu = openMenu(baseItems());
    const rows = screen.getAllByRole("menuitem");

    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(rows[1]).toHaveFocus();
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(rows[0]).toHaveFocus(); // wrapped past the last row
    fireEvent.keyDown(menu, { key: "ArrowUp" });
    expect(rows[2]).toHaveFocus(); // wrapped back past the first
  });

  it("Home and End jump to the ends", () => {
    const menu = openMenu(baseItems());
    const rows = screen.getAllByRole("menuitem");

    fireEvent.keyDown(menu, { key: "End" });
    expect(rows[2]).toHaveFocus();
    fireEvent.keyDown(menu, { key: "Home" });
    expect(rows[0]).toHaveFocus();
  });

  it("Enter selects the focused item and closes the menu", () => {
    const onRename = vi.fn();
    const menu = openMenu(baseItems(vi.fn(), onRename));

    fireEvent.keyDown(menu, { key: "ArrowDown" });
    fireEvent.keyDown(menu, { key: "Enter" });

    expect(onRename).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("Space selects too", () => {
    const onOpen = vi.fn();
    const menu = openMenu(baseItems(onOpen));

    fireEvent.keyDown(menu, { key: " " });

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("Escape closes without selecting", () => {
    const onOpen = vi.fn();
    const menu = openMenu(baseItems(onOpen));

    fireEvent.keyDown(menu, { key: "Escape" });

    expect(onOpen).not.toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("type-ahead jumps to the next row starting with the letter, and cycles on repeat", () => {
    const menu = openMenu([
      { id: "open", label: "Open" },
      { id: "rename", label: "Rename" },
      { id: "restore", label: "Restore" },
    ]);
    const rows = screen.getAllByRole("menuitem");

    fireEvent.keyDown(menu, { key: "r" });
    expect(rows[1]).toHaveFocus();
    fireEvent.keyDown(menu, { key: "r" });
    expect(rows[2]).toHaveFocus();
    fireEvent.keyDown(menu, { key: "r" });
    expect(rows[1]).toHaveFocus(); // wraps back round the matches
  });

  it("type-ahead ignores a letter no row starts with, and a modifier chord", () => {
    const menu = openMenu(baseItems());
    const rows = screen.getAllByRole("menuitem");

    fireEvent.keyDown(menu, { key: "z" });
    expect(rows[0]).toHaveFocus();
    fireEvent.keyDown(menu, { key: "r", metaKey: true });
    expect(rows[0]).toHaveFocus();
  });
});

const withSubmenu = (onCopy = vi.fn()): ContextMenuItem[] => [
  { id: "open", label: "Open" },
  {
    id: "moveto",
    label: "Move to…",
    submenu: [
      { id: "home", label: "Home", onSelect: onCopy },
      { id: "shared", label: "Shared" },
    ],
  },
  { id: "locked", label: "Share…", disabled: "Sharing is not enabled yet", submenu: [{ id: "x", label: "Anyone" }] },
];

describe("ContextMenu — submenus", () => {
  const openMenu = (items: ContextMenuItem[]) => {
    render(<Harness items={items} />);
    rightClick(screen.getByTestId("row"));
    return screen.getByRole("menu");
  };

  it("ArrowRight opens the submenu and moves focus into it", () => {
    const parent = openMenu(withSubmenu());

    fireEvent.keyDown(parent, { key: "ArrowDown" });
    expect(screen.queryByRole("menu", { name: "Move to…" })).toBeNull();
    fireEvent.keyDown(parent, { key: "ArrowRight" });

    const sub = screen.getByRole("menu", { name: "Move to…" });
    expect(screen.getAllByRole("menuitem", { name: "Move to…" })[0]).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("menuitem", { name: "Home" })).toHaveFocus();
    expect(sub).toBeInTheDocument();
  });

  it("ArrowLeft closes the submenu and returns focus to the parent row", () => {
    const parent = openMenu(withSubmenu());
    fireEvent.keyDown(parent, { key: "ArrowDown" });
    fireEvent.keyDown(parent, { key: "ArrowRight" });

    fireEvent.keyDown(screen.getByRole("menu", { name: "Move to…" }), { key: "ArrowLeft" });

    expect(screen.queryByRole("menu", { name: "Move to…" })).toBeNull();
    expect(screen.getByRole("menuitem", { name: /Move to…/ })).toHaveFocus();
  });

  it("Escape inside a submenu closes only the submenu", () => {
    const parent = openMenu(withSubmenu());
    fireEvent.keyDown(parent, { key: "ArrowDown" });
    fireEvent.keyDown(parent, { key: "ArrowRight" });

    fireEvent.keyDown(screen.getByRole("menu", { name: "Move to…" }), { key: "Escape" });

    expect(screen.queryByRole("menu", { name: "Move to…" })).toBeNull();
    expect(screen.getByRole("menu", { name: "Actions for report.csv" })).toBeInTheDocument();
  });

  it("selecting a submenu item runs it and closes the whole menu", () => {
    const onCopy = vi.fn();
    const parent = openMenu(withSubmenu(onCopy));
    fireEvent.keyDown(parent, { key: "ArrowDown" });
    fireEvent.keyDown(parent, { key: "ArrowRight" });

    fireEvent.keyDown(screen.getByRole("menu", { name: "Move to…" }), { key: "Enter" });

    expect(onCopy).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("ArrowLeft in the ROOT menu does nothing — there is no parent to return to", () => {
    const parent = openMenu(withSubmenu());

    fireEvent.keyDown(parent, { key: "ArrowLeft" });

    expect(screen.getByRole("menu", { name: "Actions for report.csv" })).toBeInTheDocument();
    expect(screen.getAllByRole("menuitem")[0]).toHaveFocus();
  });

  it("a disabled row with a submenu never opens it", () => {
    const parent = openMenu(withSubmenu());
    fireEvent.keyDown(parent, { key: "End" });

    fireEvent.keyDown(parent, { key: "ArrowRight" });

    expect(screen.queryByRole("menu", { name: "Share…" })).toBeNull();
  });
});

describe("ContextMenu — disabled items", () => {
  it("exposes the reason as the tooltip and the accessible description, and refuses selection", async () => {
    const user = userEvent.setup();
    render(<Harness items={baseItems()} />);
    rightClick(screen.getByTestId("row"));
    const trash = screen.getByRole("menuitem", { name: /Move to trash/ });

    expect(trash).toHaveAttribute("aria-disabled", "true");
    expect(trash).toHaveAttribute("title", "You have read-only access to this folder");
    expect(trash).toHaveAccessibleDescription("You have read-only access to this folder");

    await user.click(trash);

    // Still open: a refused row neither runs nor dismisses.
    expect(screen.getByRole("menu")).toBeInTheDocument();
  });

  it("Enter on a disabled row does not select or close", () => {
    render(<Harness items={baseItems()} />);
    rightClick(screen.getByTestId("row"));
    const menu = screen.getByRole("menu");

    fireEvent.keyDown(menu, { key: "End" });
    fireEvent.keyDown(menu, { key: "Enter" });

    expect(screen.getByRole("menu")).toBeInTheDocument();
  });

  it("an enabled row carries no reason, tooltip or aria-disabled — the negative twin", () => {
    render(<Harness items={baseItems()} />);
    rightClick(screen.getByTestId("row"));
    const open = screen.getByRole("menuitem", { name: /Open/ });

    expect(open).not.toHaveAttribute("aria-disabled");
    expect(open).not.toHaveAttribute("title");
    expect(open).toHaveAccessibleDescription("");
  });
});

describe("ContextMenu — destructive rows", () => {
  const withDelete = (disabled?: string): ContextMenuItem[] => [
    { id: "open", label: "Open" },
    { id: "delete", label: "Delete forever", tone: "destructive", ...(disabled ? { disabled } : {}) },
  ];

  it("marks the destructive row, and only it, for the danger ink", () => {
    render(<Harness items={withDelete()} />);
    rightClick(screen.getByTestId("row"));

    expect(screen.getByRole("menuitem", { name: "Delete forever" })).toHaveAttribute(
      "data-tone",
      "destructive",
    );
    expect(screen.getByRole("menuitem", { name: "Open" })).not.toHaveAttribute("data-tone");
  });

  it("a refused destructive row still reads as refused — the reason outranks the cost", () => {
    render(<Harness items={withDelete("You cannot delete this item.")} />);
    rightClick(screen.getByTestId("row"));
    const row = screen.getByRole("menuitem", { name: /Delete forever/ });

    // Both marks are on the row; the stylesheet gives the disabled ink the last
    // word, which is what `:not([data-disabled])` in contextmenu.css encodes.
    expect(row).toHaveAttribute("data-tone", "destructive");
    expect(row).toHaveAttribute("data-disabled");
    expect(row).toHaveAccessibleDescription("You cannot delete this item.");
  });
});

describe("ContextMenu — dismissal", () => {
  const open = (items = baseItems()) => {
    render(<Harness items={items} />);
    rightClick(screen.getByTestId("row"));
  };

  it("closes on a scroll anywhere in the page", () => {
    open();
    act(() => {
      fireEvent.scroll(document, {});
    });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("closes when the window loses focus", () => {
    open();
    act(() => {
      fireEvent.blur(window);
    });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("closes on an outside press but NOT on a press inside the menu", () => {
    open();
    fireEvent.mouseDown(screen.getByRole("menuitem", { name: /Open/ }));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByRole("button", { name: "elsewhere" }));
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("closes after a click-selected item runs", async () => {
    const onOpen = vi.fn();
    open(baseItems(onOpen));

    await userEvent.setup().click(screen.getByRole("menuitem", { name: /Open/ }));

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("returns focus to the element that opened it", () => {
    render(<Harness items={baseItems()} />);
    const row = screen.getByTestId("row");
    row.focus();
    rightClick(row);
    expect(screen.getAllByRole("menuitem")[0]).toHaveFocus();

    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });

    expect(row).toHaveFocus();
  });
});

describe("the panel's ground", () => {
  it("portals into the themed ancestor of whatever opened it", async () => {
    const user = userEvent.setup();
    function Themed() {
      const menu = useContextMenu();
      return (
        <div data-alkera-color-scheme="dark" data-testid="themed">
          <button type="button" {...menu.triggerProps}>
            row
          </button>
          <ContextMenu {...menu.menuProps} items={[{ id: "a", label: "Open" }]} label="Actions" />
        </div>
      );
    }
    render(<Themed />);
    await user.pointer({ keys: "[MouseRight]", target: screen.getByRole("button", { name: "row" }) });

    // The tokens a portaled panel resolves are the ones its ANCESTOR sets. Portaled to
    // <body> it took the document's scheme instead, which drew a light menu over a dark
    // page. Containment, not a class name, is what decides that.
    const panel = await screen.findByRole("menu", { name: "Actions" });
    expect(screen.getByTestId("themed")).toContainElement(panel);
    expect(panel.parentElement).not.toBe(document.body);
  });
});

describe("ContextMenu — staying on screen", () => {
  // jsdom lays nothing out, so the panel is given the size a real browser would: one 31px row per
  // item plus its padding, 240px wide. Both measuring APIs answer the same, so the placement is
  // judged on where it puts the panel, not on which API it happens to read.
  const ROW = 31;
  const WIDTH = 240;
  const panelHeight = (el: HTMLElement) => el.querySelectorAll('[role="menuitem"]').length * ROW + 8;
  const isPanel = (el: HTMLElement) => el.classList.contains("alk-ctxmenu");
  const restore: (() => void)[] = [];

  const stub = <K extends "offsetHeight" | "offsetWidth" | "getBoundingClientRect">(
    key: K,
    descriptor: PropertyDescriptor,
  ) => {
    const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, key);
    Object.defineProperty(HTMLElement.prototype, key, { configurable: true, ...descriptor });
    restore.push(() => {
      if (original) Object.defineProperty(HTMLElement.prototype, key, original);
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[key];
    });
  };

  beforeEach(() => {
    const viewport = { innerWidth: window.innerWidth, innerHeight: window.innerHeight };
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 900 });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1280 });
    restore.push(() => {
      Object.defineProperty(window, "innerHeight", { configurable: true, value: viewport.innerHeight });
      Object.defineProperty(window, "innerWidth", { configurable: true, value: viewport.innerWidth });
    });
    stub("offsetHeight", {
      get(this: HTMLElement) {
        return isPanel(this) ? panelHeight(this) : 0;
      },
    });
    stub("offsetWidth", {
      get(this: HTMLElement) {
        return isPanel(this) ? WIDTH : 0;
      },
    });
    const originalRect = HTMLElement.prototype.getBoundingClientRect;
    stub("getBoundingClientRect", {
      value(this: HTMLElement) {
        if (!isPanel(this)) return originalRect.call(this);
        const top = parseFloat(this.style.top) || 0;
        const left = parseFloat(this.style.left) || 0;
        const height = panelHeight(this);
        return { top, left, bottom: top + height, right: left + WIDTH, width: WIDTH, height, x: left, y: top } as DOMRect;
      },
    });
  });
  afterEach(() => {
    while (restore.length) restore.pop()!();
  });

  const rows = (n: number): ContextMenuItem[] =>
    Array.from({ length: n }, (_, i) => ({ id: `row-${i}`, label: `Action ${i}` }));

  /** Where the panel's bottom edge lands, from the placement the component chose. */
  const bottomOf = (menu: HTMLElement) => parseFloat(menu.style.top) + Math.min(panelHeight(menu), 900);

  it("flips above a row near the bottom of a 900px viewport, so the last row is reachable", () => {
    render(<Harness items={rows(18)} />);
    fireEvent.contextMenu(screen.getByTestId("row"), { clientX: 400, clientY: 847 });

    const menu = screen.getByRole("menu");
    expect(parseFloat(menu.style.top)).toBe(847 - panelHeight(menu));
    expect(bottomOf(menu)).toBeLessThanOrEqual(900);
  });

  it("re-places the panel when its rows arrive after it opened", () => {
    // The Files grid opens its menu from the right-click and fills it from the selection that same
    // click makes, so the panel is short for one frame and tall the next. Placed once, for the
    // short panel, the tall one ran 113px past a 900px viewport and its last rows could not be
    // clicked.
    const { rerender } = render(<Harness items={rows(3)} />);
    fireEvent.contextMenu(screen.getByTestId("row"), { clientX: 400, clientY: 447 });
    expect(screen.getByRole("menu")).toHaveStyle({ top: "447px" });

    rerender(<Harness items={rows(18)} />);

    const menu = screen.getByRole("menu");
    expect(screen.getAllByRole("menuitem")).toHaveLength(18);
    expect(bottomOf(menu)).toBeLessThanOrEqual(900);
    expect(parseFloat(menu.style.top)).toBeGreaterThanOrEqual(0);
  });

  it("a menu taller than the viewport is capped to it and scrolls, pinned at the top", () => {
    render(<Harness items={rows(40)} />);
    fireEvent.contextMenu(screen.getByTestId("row"), { clientX: 400, clientY: 600 });

    const menu = screen.getByRole("menu");
    expect(menu).toHaveStyle({ top: "0px", maxHeight: "900px" });
  });

  it("stays at the pointer when there is room below — the flip is not unconditional", () => {
    render(<Harness items={rows(18)} />);
    fireEvent.contextMenu(screen.getByTestId("row"), { clientX: 400, clientY: 100 });

    expect(screen.getByRole("menu")).toHaveStyle({ top: "100px", left: "400px" });
  });

  it("flips left of a pointer at the right edge", () => {
    render(<Harness items={rows(3)} />);
    fireEvent.contextMenu(screen.getByTestId("row"), { clientX: 1200, clientY: 100 });

    expect(screen.getByRole("menu")).toHaveStyle({ left: `${1200 - WIDTH}px` });
  });
});
