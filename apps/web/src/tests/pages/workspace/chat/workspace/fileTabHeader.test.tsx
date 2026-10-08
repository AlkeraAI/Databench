// The one line over an open file.
//
// The property under test is the fold: a pane the reader drags narrow must lose
// no action and cut no control. So the row is measured (the same ResizeObserver
// seam the dock's columns are measured through) and the assertions are about
// what a person can still reach at a given width — never about how the arithmetic
// was done.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  FACTS_FLOOR,
  FileTabHeader,
  visibleActionCount,
  type FileTabAction,
} from "@/pages/workspace/chat/workspace/FileTabHeader";

/** jsdom lays nothing out, so the width the header measures is stated here — the
 *  one thing a browser would have supplied. */
function atWidth(width: number): () => void {
  const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get: () => width,
  });
  return () => {
    if (original) Object.defineProperty(HTMLElement.prototype, "clientWidth", original);
    else delete (HTMLElement.prototype as unknown as { clientWidth?: unknown }).clientWidth;
  };
}

let restoreWidth: (() => void) | null = null;

afterEach(() => {
  restoreWidth?.();
  restoreWidth = null;
});

const LABELS = ["Soft wrap", "Download", "Open in new tab", "Share", "Reveal in Files", "Copy link"];

function actions(run: (id: string) => void = () => {}): FileTabAction[] {
  return LABELS.map((label, index) => ({
    id: `a${index}`,
    label,
    icon: <svg data-testid={`glyph-${index}`} />,
    run: () => run(`a${index}`),
    ...(label === "Soft wrap" ? { pressed: false } : {}),
  }));
}

function mount(width: number | null, over: Partial<Parameters<typeof FileTabHeader>[0]> = {}) {
  if (width !== null) restoreWidth = atWidth(width);
  return render(
    <FileTabHeader
      name="notes.txt"
      path="charts/notes.txt"
      facts={["2 KB", "Updated Sep 19, 2026"]}
      actions={actions()}
      {...over}
    />,
  );
}

describe("how many actions a width can hold", () => {
  it("shows every action when the row has not been measured yet", () => {
    // The first paint must never be the narrowest layout.
    expect(visibleActionCount(0, 6)).toBe(6);
  });

  it("shows every action when they all fit", () => {
    expect(visibleActionCount(900, 6)).toBe(6);
  });

  it("spends one slot on the menu as soon as one action does not fit", () => {
    const wide = visibleActionCount(900, 6);
    // Just under the width that held all six: five would fit, but one of those
    // slots has to carry the menu holding the sixth.
    let folding = 900;
    while (visibleActionCount(folding, 6) === wide && folding > 0) folding -= 5;
    expect(visibleActionCount(folding, 6)).toBeLessThanOrEqual(4);
  });

  it("never asks for a negative number of actions, however little room there is", () => {
    for (const width of [1, 20, 60, 108]) {
      expect(visibleActionCount(width, 6)).toBeGreaterThanOrEqual(0);
    }
  });
});

describe("the header at a comfortable width", () => {
  it("names the file, carries the whole path on it, and states the readings once", () => {
    mount(900);
    expect(screen.getByTitle("charts/notes.txt")).toHaveTextContent("notes.txt");
    expect(screen.getByText("2 KB")).toBeInTheDocument();
    expect(screen.getByText("Updated Sep 19, 2026")).toBeInTheDocument();
  });

  it("draws every action as a glyph that still announces what it does", () => {
    mount(900);
    const group = within(screen.getByRole("group", { name: "File actions" }));
    for (const label of LABELS) {
      const button = group.getByRole("button", { name: label });
      // A glyph and nothing else: the name is the label, not text in the button.
      expect(button.textContent).toBe("");
      expect(button.querySelector("svg")).not.toBeNull();
    }
    expect(screen.queryByRole("button", { name: "More actions" })).toBeNull();
  });

  it("reads a setting back as a switch and a door as a plain command", () => {
    mount(900);
    expect(screen.getByRole("button", { name: "Soft wrap" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByRole("button", { name: "Download" })).not.toHaveAttribute("aria-pressed");
  });
});

describe("the header in a pane dragged narrow", () => {
  it("folds the actions that no longer fit into a menu, losing none of them", async () => {
    const user = userEvent.setup();
    const ran: string[] = [];
    restoreWidth = atWidth(200);
    render(
      <FileTabHeader
        name="notes.txt"
        path="charts/notes.txt"
        facts={["2 KB", "Updated Sep 19, 2026"]}
        actions={actions((id) => ran.push(id))}
      />,
    );

    const row = within(screen.getByRole("group", { name: "File actions" }));
    const onRow = LABELS.filter((label) => row.queryAllByRole("button", { name: label }).length > 0);
    expect(onRow.length).toBeLessThan(LABELS.length);

    const more = screen.getByRole("button", { name: "More actions" });
    await user.click(more);
    const menu = within(await screen.findByRole("menu"));
    for (const label of LABELS.filter((label) => !onRow.includes(label))) {
      expect(menu.getByRole("menuitem", { name: label })).toBeInTheDocument();
    }

    // A folded action is a working action, not a listing of one.
    const folded = LABELS.filter((label) => !onRow.includes(label))[0]!;
    await user.click(menu.getByRole("menuitem", { name: folded }));
    expect(ran).toEqual([`a${LABELS.indexOf(folded)}`]);
  });

  it("moves the readings onto the name rather than dropping them or wrapping the row", () => {
    mount(FACTS_FLOOR - 1);
    expect(screen.queryByText("2 KB")).toBeNull();
    expect(screen.getByTitle(/charts\/notes\.txt · 2 KB · Updated Sep 19, 2026/)).toHaveTextContent(
      "notes.txt",
    );
  });

  it("keeps the readings on the line once there is room for them", () => {
    mount(FACTS_FLOOR);
    expect(screen.getByText("2 KB")).toBeInTheDocument();
    expect(screen.getByTitle("charts/notes.txt")).toBeInTheDocument();
  });
});

describe("what the header never does", () => {
  it("draws no menu when every action is on the row", () => {
    mount(900);
    expect(screen.queryByRole("button", { name: "More actions" })).toBeNull();
  });

  it("observes the row it is drawn in, so a drag re-decides the fold", () => {
    const observed: Element[] = [];
    class Recording {
      observe(element: Element) {
        observed.push(element);
      }
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", Recording);
    mount(900);
    vi.unstubAllGlobals();

    expect(observed).toHaveLength(1);
    expect(observed[0]).toHaveClass("alk-ws-file__bar");
  });
});
