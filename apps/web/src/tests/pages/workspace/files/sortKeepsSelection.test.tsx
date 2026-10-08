/**
 * Sorting re-orders the rows. It does not throw the selection away.
 *
 * "Select everything, sort to check, then act" is the natural move, and it acted
 * on nothing: 25 selected rows became 0 the moment a column header was clicked,
 * and the details pane fell back to "Select an item to see its details.".
 *
 * The order lives in the request, so a header click starts a NEW listing query.
 * For the render between the click and the answer that query has no data, the
 * grid paints no rows, and the reconciler — which exists to hand the focus and
 * the selection to a neighbour when rows are trashed — read that empty frame as
 * "every row has left" and cleared both the selection and the tab stop.
 *
 * The keyboard is the other half, and it has its own cause. A header click leaves
 * the focus on the header button, where the grid answered no keystroke at all —
 * so the Cmd+A a reader reaches for next ran as the browser's own select-all over
 * the whole page. Neither half is asserted by focusing a row by hand: a reader
 * cannot, and a test that does proves only that the handler is attached.
 *
 * `fetch` is stubbed at the wire and sorts like the server would, so the
 * re-order is real and the selection is asserted by row id, never by position.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { Item } from "@/api/files";
import { FilesBrowser } from "@/pages/workspace/files/FilesBrowser";

const DRIVE = "drv_1";
const PARENT = "nd_parent";

function item(overrides: Partial<Item> & { id: string; name: string; size: number }): Item {
  return {
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    subtype: null,
    nameDisplay: overrides.name,
    nameEncoding: "utf-8",
    nameFlags: { windows_safe: true, macos_safe: true, display_warning: false },
    pathBytes: "",
    parentId: PARENT,
    path: null,
    etag: "e1",
    ctag: "c1",
    attrs: { mtime: "2026-01-01T00:00:00Z" },
    file: { size: overrides.size },
    symlink: null,
    object: null,
    lease: null,
    stale: false,
    trust: null,
    locked: false,
    held: false,
    capabilities: {},
    shared: false,
    trashed: false,
    ...overrides,
  } as unknown as Item;
}

/** Names and sizes disagree on purpose: a sort by size must really move the rows,
 *  so "the selection survived" cannot be confused with "nothing happened". */
const ROWS: Item[] = [
  item({ id: "nd_a", name: "alpha.txt", size: 5_000 }),
  item({ id: "nd_b", name: "bravo.md", size: 40 }),
  item({ id: "nd_c", name: "charlie.sql", size: 999_000 }),
  item({ id: "nd_d", name: "delta.csv", size: 12 }),
  item({ id: "nd_e", name: "echo.json", size: 70_000 }),
];

const KEYS: Record<string, (row: Item) => string | number> = {
  name: (row) => row.name,
  size: (row) => (row.file?.size as number | undefined) ?? 0,
};

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      const order = new URL(url, "http://localhost").searchParams.get("orderBy") ?? "name asc";
      const [field = "name", direction = "asc"] = order.split(" ");
      const key = KEYS[field] ?? KEYS.name!;
      const sorted = [...ROWS].sort((a, b) => {
        const left = key(a);
        const right = key(b);
        const cmp = left < right ? -1 : left > right ? 1 : 0;
        return direction === "desc" ? -cmp : cmp;
      });
      return Promise.resolve(
        new Response(JSON.stringify({ value: sorted, nextMarker: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    }),
  );
}

function mount() {
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <FilesBrowser driveId={DRIVE} parentId={PARENT} platform="other" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function paintedIds(): string[] {
  return Array.from(document.querySelectorAll("[data-row-id]")).map(
    (element) => element.getAttribute("data-row-id") ?? "",
  );
}

function selectedIds(): string[] {
  return Array.from(document.querySelectorAll('[data-row-id][aria-selected="true"]'))
    .map((element) => element.getAttribute("data-row-id") ?? "")
    .sort();
}

/** The rows Tab can land on: the roving tab stop, which follows the focused row. */
function tabStops(): string[] {
  return Array.from(document.querySelectorAll('[data-row-id][tabindex="0"]')).map(
    (element) => element.getAttribute("data-row-id") ?? "",
  );
}

function rowFor(id: string): HTMLElement {
  const row = document.querySelector<HTMLElement>(`[data-row-id="${id}"]`);
  if (row === null) throw new Error(`no row painted for ${id}`);
  return row;
}

async function rowsPainted(): Promise<void> {
  await waitFor(() => expect(paintedIds().length).toBe(ROWS.length));
}

/** Three rows, picked one at a time the way a person does. */
async function pickThree(user: ReturnType<typeof userEvent.setup>): Promise<string[]> {
  const picked = ["nd_a", "nd_c", "nd_e"];
  await user.click(rowFor("nd_a"));
  for (const id of ["nd_c", "nd_e"]) {
    await user.keyboard("{Control>}");
    await user.click(rowFor(id));
    await user.keyboard("{/Control}");
  }
  await waitFor(() => expect(selectedIds()).toEqual([...picked].sort()));
  return picked;
}

beforeEach(() => {
  stubApi();
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("a selection across a re-sort", () => {
  it("holds the same rows after the order changes", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    const picked = await pickThree(user);
    const before = paintedIds();

    await user.click(screen.getByRole("button", { name: "Sort by Size" }));

    // The rows really moved — otherwise "the selection survived" proves nothing.
    // Size opens descending: the question a reader asks of it is what is biggest.
    await waitFor(() => expect(paintedIds()).toEqual(["nd_c", "nd_e", "nd_a", "nd_b", "nd_d"]));
    expect(paintedIds()).not.toEqual(before);
    expect(selectedIds()).toEqual([...picked].sort());
  });

  it("holds them across a sort back, so no order is the one that loses them", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    const picked = await pickThree(user);

    await user.click(screen.getByRole("button", { name: "Sort by Size" }));
    await waitFor(() => expect(paintedIds()[0]).toBe("nd_c"));
    await user.click(screen.getByRole("button", { name: "Sort by Name" }));
    await waitFor(() => expect(paintedIds()[0]).toBe("nd_a"));

    expect(selectedIds()).toEqual([...picked].sort());
  });

  it("leaves the tab stop on the row the reader was on, not back at the top", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await pickThree(user);

    await user.click(screen.getByRole("button", { name: "Sort by Size" }));
    await waitFor(() => expect(paintedIds()[0]).toBe("nd_c"));

    // nd_e is the row the last click landed on. Reconciled away, the tab stop fell
    // back to whichever row the new order happened to put first — so Tab came back
    // into the listing somewhere the reader had never been.
    expect(tabStops()).toEqual(["nd_e"]);
  });

  it("keeps select-all with the grid after a header click, so it never reaches the page", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await pickThree(user);

    // Where a header click leaves the keyboard: on the header button, which is what
    // a sort is clicked with. The test does not put the focus anywhere else, because
    // a reader cannot either.
    const header = screen.getByRole("button", { name: "Sort by Size" });
    await user.click(header);
    await waitFor(() => expect(paintedIds()[0]).toBe("nd_c"));
    expect(document.activeElement).toBe(header);

    // The recovery keystroke a person reaches for when a selection looks wrong.
    // Answered by the header instead of the grid, it ran as the browser's own
    // select-all over the whole page — nav, breadcrumb and details pane painted in
    // the text highlight — while the selection stayed exactly as it was.
    const seen: boolean[] = [];
    const watch = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === "a") seen.push(event.defaultPrevented);
    };
    document.addEventListener("keydown", watch);
    try {
      await user.keyboard("{Control>}a{/Control}");
      await waitFor(() => expect(selectedIds()).toEqual(ROWS.map((row) => row.id).sort()));
      // Consumed by the grid, so nothing is left for the browser to run.
      expect(seen).toEqual([true]);
    } finally {
      document.removeEventListener("keydown", watch);
    }
  });

  it("still hands the selection on when rows genuinely leave the listing", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await pickThree(user);

    // The reconciler is not being disabled, only kept away from the empty frame a
    // new query renders: a row that really went must still drop out.
    ROWS.splice(ROWS.findIndex((row) => row.id === "nd_c"), 1);
    await user.click(screen.getByRole("button", { name: "Sort by Size" }));
    await waitFor(() => expect(paintedIds()).toHaveLength(4));

    expect(selectedIds()).toEqual(["nd_a", "nd_e"]);
    ROWS.splice(2, 0, item({ id: "nd_c", name: "charlie.sql", size: 999_000 }));
  });
});
