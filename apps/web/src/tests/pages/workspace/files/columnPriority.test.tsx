// What a listing draws when it does not have room for all five columns.
//
// Two failures, one cause. At 1280 px the listing gets ~430 px of the window, the
// five columns' natural width is 596, and the Owner column simply fell off the
// right-hand edge — present, sorted, and reachable only by scrolling the grid
// sideways. At 390 px the row folded instead: the header row was hidden outright and
// Kind, Size, Modified and Owner were still rendered with nothing in them, so every
// row read as an em-dash, a name, and another em-dash.
//
// The rule now is one rule: a column that does not fit is not RENDERED. The name is
// never one of them, the header always matches the cells, and nothing is painted
// empty.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

import type { Item } from "@/api/files";
import {
  FILES_COLUMNS,
  gridTemplateFor,
  visibleColumns,
} from "@/lib/files/columns";
import { Treegrid } from "@/pages/workspace/files/Treegrid";
import { emptySelection } from "@/pages/workspace/files/state/selection";

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    name: "report.csv",
    nameDisplay: "report.csv",
    nameEncoding: "utf-8",
    etag: "et_1",
    attrs: { owner: "Dana Reyes", mtime: "2026-03-04T10:00:00Z" },
    file: { size: 4096 },
    ...over,
  } as Item;
}

/** The grid at a listing width the browser reports for the scroller. */
function renderAt(width: number) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <Treegrid
      rows={[item()]}
      view="list"
      selection={emptySelection}
      onSelectionAction={vi.fn()}
      orderBy={{ field: "name", direction: "asc" }}
      onSort={vi.fn()}
      platform="mac"
        initialRect={{ width, height: 480 }}
      />
    </QueryClientProvider>,
  );
}

const ids = (columns: readonly { id: string }[]) => columns.map((column) => column.id);

afterEach(cleanup);

describe("which columns a listing this wide draws", () => {
  it("draws all five when there is room for all five", () => {
    expect(ids(visibleColumns(1200))).toEqual(["name", "kind", "size", "modified", "owner"]);
  });

  it("drops Owner first, at the width a 1280 px window actually leaves the listing", () => {
    // 430 px: the rail takes 240 and the details pane 300, which is how Owner ended
    // up off-screen behind a sideways scroll rather than simply not being drawn.
    const shown = ids(visibleColumns(430));
    expect(shown).not.toContain("owner");
    expect(shown).toContain("name");
  });

  it("drops the columns the name and the icon already imply before the ones they do not", () => {
    // Narrower and narrower, and the order things go in never reverses.
    const widths = [1200, 700, 560, 430, 390, 120];
    const shown = widths.map((width) => ids(visibleColumns(width)));
    for (let i = 1; i < shown.length; i += 1) {
      // Each step is a SUBSET of the one before it: a column never comes back as the
      // listing narrows, which is what makes the order a priority rather than a guess.
      expect(shown[i]!.every((id) => shown[i - 1]!.includes(id))).toBe(true);
    }
    expect(shown.at(-1)).toEqual(["name"]);
    // Owner goes before Kind, Kind before Size, Size before Modified.
    const wentAt = (id: string) => shown.findIndex((columns) => !columns.includes(id));
    expect(wentAt("owner")).toBeLessThan(wentAt("kind"));
    expect(wentAt("kind")).toBeLessThan(wentAt("size"));
    expect(wentAt("size")).toBeLessThan(wentAt("modified"));
  });

  it("keeps the name however narrow the listing gets", () => {
    for (const width of [0, 1, 40, 199]) {
      expect(ids(visibleColumns(width))).toEqual(["name"]);
    }
  });

  it("lays down one track per column it drew, none of which can collapse", () => {
    for (const width of [1200, 430, 390, 80]) {
      const shown = visibleColumns(width);
      const template = gridTemplateFor(shown);
      expect(template.match(/minmax\(/g) ?? []).toHaveLength(shown.length);
      // A floor of 0 is what let the trash row's name disappear; no track gets one.
      expect(template).not.toMatch(/minmax\(\s*0/);
    }
  });
});

describe("the rendered listing", () => {
  it("keeps a header, and a header cell for every cell in the row", () => {
    for (const width of [1200, 430, 390]) {
      renderAt(width);
      const headers = screen.getAllByRole("columnheader");
      expect(headers.length).toBeGreaterThan(0);

      const row = screen.getByRole("row", { name: /report\.csv/ });
      expect(within(row).getAllByRole("gridcell")).toHaveLength(headers.length);
      expect(headers[0]).toHaveTextContent("Name");
      cleanup();
    }
  });

  it("paints no empty placeholder for a column it did not draw", () => {
    renderAt(390);

    const row = screen.getByRole("row", { name: /report\.csv/ });
    const drawn = within(row)
      .getAllByRole("gridcell")
      .map((cell) => cell.getAttribute("data-column"));
    expect(drawn).not.toContain("owner");
    // The owner is real on this row — it is absent because the column is, not because
    // the value was missing, which is the difference between a drop and a stray "—".
    expect(row.textContent).not.toContain("Dana Reyes");
    expect(screen.queryByRole("columnheader", { name: /Owner/ })).toBeNull();
  });

  it("names only the columns it drew to a screen reader", () => {
    renderAt(390);
    const grid = screen.getByRole("treegrid");
    const drawn = visibleColumns(390).length;

    expect(grid).toHaveAttribute("aria-colcount", String(drawn));
    expect(drawn).toBeLessThan(FILES_COLUMNS.length);
  });
});
