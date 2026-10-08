/**
 * Where a tile in the Files grid view lands, at every width.
 *
 * One count, `columnsFor`, sized for the gaps and the padding, slices the tiles AND spells
 * the lane's tracks. A second count (CSS `auto-fill`) would disagree by one column across a
 * band of widths above each breakpoint, and the extra tile would wrap onto a second row of a
 * one-row, absolutely positioned lane, painting it over the next lane's first tile.
 *
 * The cases below hold the count honest against the room a lane actually has, hold every
 * tile at its own (row, column), and hold the keyboard moving through the grid one item at
 * a time while it does.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import {
  applySelection,
  columnsFor,
  lanesFor,
  TILE_GAP,
  TILE_MIN_WIDTH,
  Treegrid,
} from "@/pages/workspace/files/Treegrid";
import { emptySelection, type SelectionState } from "@/pages/workspace/files/state/selection";

function item(id: string, name: string): Item {
  return {
    id,
    ino: 1,
    driveId: "dr_1",
    kind: "file",
    name,
    nameDisplay: name,
    nameEncoding: "utf-8",
    etag: "et_1",
    attrs: { mtime: "2026-03-04T10:00:00Z" },
    file: { size: 4096 },
  } as Item;
}

/** The owner's listing: the eleven files that drew as nine. */
const ROWS: readonly Item[] = [
  "linked-q3.png",
  "prompt-run-volume.html",
  "TEMPLATE.md",
  "template-seed-1789658462229.js",
  "notes.txt",
  "run-log.json",
  "summary.csv",
  "diagram.svg",
  "readme.md",
  "sketch.png",
  "index.ts",
].map((name, index) => item(`nd_${index}`, name));

/** The width a lane of `n` tiles needs: the tracks, the gaps between them, and the lane's
 *  padding on both sides. Nothing here reads the implementation's arithmetic — it is what
 *  the browser has to fit. */
function laneWidthFor(columns: number): number {
  return columns * TILE_MIN_WIDTH + (columns - 1) * TILE_GAP + 2 * TILE_GAP;
}

function Harness({ width, rows = ROWS }: { width: number; rows?: readonly Item[] }) {
  const [selection, setSelection] = useState<SelectionState>(emptySelection);
  return (
    <QueryClientProvider client={createQueryClient()}>
      <Treegrid
        rows={rows}
        view="grid"
        selection={selection}
        onSelectionAction={(action) =>
          setSelection((current) => applySelection(current, action, rows))
        }
        orderBy={{ field: "name", direction: "asc" }}
        onSort={vi.fn()}
        platform="mac"
        initialRect={{ width, height: 1200 }}
      />
    </QueryClientProvider>
  );
}

interface Placed {
  readonly id: string;
  readonly row: number;
  readonly column: number;
}

/** Every tile on screen, placed the way the browser places it: down the lanes, and inside
 *  a lane across its declared tracks — wrapping to a further row when a lane holds more
 *  tiles than it has cells, which is precisely the overlap. */
function placements(container: HTMLElement, width: number): Placed[] {
  const lanes = [...container.querySelectorAll<HTMLElement>('[data-lane="grid"]')];
  const out: Placed[] = [];
  lanes.forEach((lane, laneIndex) => {
    const tracks = trackCountOf(lane, width);
    const tiles = [...lane.querySelectorAll<HTMLElement>('[role="gridcell"][data-row-id]')];
    tiles.forEach((tile, index) => {
      out.push({
        id: tile.dataset.rowId ?? "",
        // A lane is one row tall and sits at its own row's offset, so a tile that does not
        // fit in the lane's tracks is painted a row further down — over the next lane.
        row: laneIndex + Math.floor(index / tracks),
        column: index % tracks,
      });
    });
  });
  return out;
}

/** How many cells a lane offers, read off the element the browser lays out from. A lane
 *  that declares no tracks of its own is filled by the stylesheet's `auto-fill` instead,
 *  which is the count jsdom will not work out — so it is worked out here the way a browser
 *  would, from the lane's width. */
function trackCountOf(lane: HTMLElement, width: number): number {
  const repeat = /^repeat\((\d+),/.exec(lane.style.gridTemplateColumns);
  if (repeat !== null) return Number(repeat[1]);
  return Math.max(1, Math.floor((width - 2 * TILE_GAP + TILE_GAP) / (TILE_MIN_WIDTH + TILE_GAP)));
}

/** The widths worth walking: every breakpoint from one column to eight, the width just
 *  below it, and the band just above it where two independent counts would disagree. */
const WIDTHS: number[] = [];
for (let columns = 1; columns <= 8; columns += 1) {
  const exact = laneWidthFor(columns);
  WIDTHS.push(exact - 1, exact, exact + 1, columns * TILE_MIN_WIDTH, columns * TILE_MIN_WIDTH + 5);
}
const UNIQUE_WIDTHS = [...new Set(WIDTHS)].filter((width) => width > 0).sort((a, b) => a - b);

afterEach(cleanup);

describe("how many columns a lane of this width has", () => {
  it.each(UNIQUE_WIDTHS)("fits the columns it claims at %i px", (width) => {
    const columns = columnsFor(width);
    expect(columns).toBeGreaterThanOrEqual(1);
    // One column is the floor whatever the width: a viewport too narrow for a tile shows
    // one anyway rather than an empty grid.
    if (columns > 1) expect(laneWidthFor(columns)).toBeLessThanOrEqual(width);
    // And it is the most that fit — the count is not merely safe, it is right.
    expect(laneWidthFor(columns + 1)).toBeGreaterThan(width);
  });

  it("never divides by zero columns", () => {
    for (const width of [0, 1, 40, Number.NaN]) {
      const { columns, lanes } = lanesFor(width, 11);
      expect(columns).toBe(1);
      expect(lanes).toBe(11);
    }
  });

  it("covers every row across the lanes it reports", () => {
    for (const width of UNIQUE_WIDTHS) {
      const { columns, lanes } = lanesFor(width, ROWS.length);
      expect(lanes * columns).toBeGreaterThanOrEqual(ROWS.length);
      expect((lanes - 1) * columns).toBeLessThan(ROWS.length);
    }
  });
});

describe("where the tiles land", () => {
  it.each(UNIQUE_WIDTHS)("gives every file its own cell at %i px", (width) => {
    const { container, unmount } = render(<Harness width={width} />);
    const placed = placements(container, width);
    expect(placed.map((tile) => tile.id)).toEqual(ROWS.map((row) => row.id));
    const cells = new Set(placed.map((tile) => `${tile.row}:${tile.column}`));
    expect(cells.size).toBe(ROWS.length);
    unmount();
  });

  it.each(UNIQUE_WIDTHS)("hands a lane no more tiles than it has cells at %i px", (width) => {
    const { container, unmount } = render(<Harness width={width} />);
    const lanes = [...container.querySelectorAll<HTMLElement>('[data-lane="grid"]')];
    expect(lanes.length).toBe(lanesFor(width, ROWS.length).lanes);
    for (const lane of lanes) {
      const tiles = lane.querySelectorAll('[role="gridcell"][data-row-id]');
      expect(tiles.length).toBeLessThanOrEqual(trackCountOf(lane, width));
    }
    unmount();
  });

  it("spells each lane's tracks itself rather than leaving the count to the stylesheet", () => {
    for (const width of UNIQUE_WIDTHS) {
      const { container, unmount } = render(<Harness width={width} />);
      const lanes = [...container.querySelectorAll<HTMLElement>('[data-lane="grid"]')];
      expect(lanes.length).toBeGreaterThan(0);
      for (const lane of lanes) {
        expect(lane.style.gridTemplateColumns).toBe(
          `repeat(${columnsFor(width)}, minmax(0, 1fr))`,
        );
      }
      unmount();
    }
  });

  it("tells a screen reader the same column count it drew", () => {
    for (const width of UNIQUE_WIDTHS) {
      const { container, unmount } = render(<Harness width={width} />);
      const grid = container.querySelector('[role="treegrid"]');
      const lane = container.querySelector<HTMLElement>('[data-lane="grid"]');
      expect(lane).not.toBeNull();
      expect(grid?.getAttribute("aria-colcount")).toBe(String(trackCountOf(lane!, width)));
      unmount();
    }
  });

  it("re-lays the lanes when the pane gets narrower, and still overlaps nothing", () => {
    const { container, rerender } = render(<Harness width={1200} />);
    const wide = placements(container, 1200);
    expect(new Set(wide.map((tile) => `${tile.row}:${tile.column}`)).size).toBe(ROWS.length);
    rerender(<Harness width={520} />);
    const narrow = placements(container, 520);
    expect(new Set(narrow.map((tile) => `${tile.row}:${tile.column}`)).size).toBe(ROWS.length);
    // A narrower pane really did drop columns — the case above is not passing by never
    // having changed anything.
    expect(Math.max(...narrow.map((tile) => tile.column))).toBeLessThan(
      Math.max(...wide.map((tile) => tile.column)),
    );
  });
});

describe("the keyboard through a grid this wide", () => {
  it.each([520, 664, 1200])("walks the rows one at a time at %i px", async (width) => {
    const user = userEvent.setup();
    const { container, unmount } = render(<Harness width={width} />);
    const first = container.querySelector<HTMLElement>('[data-row-id="nd_0"]');
    expect(first).not.toBeNull();
    first!.focus();
    // The first press enters the grid on the first file; from there the focus walks the
    // listing in its own order, one file per press, whatever the lanes look like.
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement?.getAttribute("data-row-id")).toBe("nd_0");
    for (let step = 1; step < ROWS.length; step += 1) {
      await user.keyboard("{ArrowDown}");
      expect(document.activeElement?.getAttribute("data-row-id")).toBe(`nd_${step}`);
    }
    // And the last file is the end of the walk, not the end of a lane.
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement?.getAttribute("data-row-id")).toBe(ROWS[ROWS.length - 1]!.id);
    unmount();
  });
});

describe("the gap the count pays for", () => {
  /** `src/pages/workspace/files`, found from wherever vitest was started. */
  function fileAt(...candidates: string[]): string {
    const found = candidates.map((c) => join(process.cwd(), c)).find((c) => existsSync(c));
    if (found === undefined) throw new Error(`not found: ${candidates.join(", ")}`);
    return readFileSync(found, "utf8");
  }

  it("is the token the lane is actually styled with", () => {
    const css = fileAt(
      "src/pages/workspace/files/treegrid.css",
      "apps/web/src/pages/workspace/files/treegrid.css",
    );
    const lane = /\[data-view="grid"\] \.alk-files-grid__lane \{([^}]*)\}/.exec(css);
    expect(lane).not.toBeNull();
    const gap = /gap:\s*var\((--alk[A-Za-z0-9]+)\)/.exec(lane![1]!);
    const padding = /padding:\s*0\s+var\((--alk[A-Za-z0-9]+)\)/.exec(lane![1]!);
    expect(gap).not.toBeNull();
    // The count pays for the padding on both sides as well as the gaps between the tiles,
    // and it pays for them at ONE size — so the lane's padding has to be that same token.
    expect(padding?.[1]).toBe(gap![1]);
    // The stylesheet must NOT count the tracks a second time; that second count is the bug.
    expect(lane![1]).not.toMatch(/grid-template-columns/);

    const tokens = fileAt("../../packages/ui/src/theme/tokens.css", "packages/ui/src/theme/tokens.css");
    const value = new RegExp(`${gap![1]}:\\s*(\\d+)px`).exec(tokens);
    expect(value).not.toBeNull();
    expect(Number(value![1])).toBe(TILE_GAP);
  });
});
