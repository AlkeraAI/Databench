/**
 * The Files treegrid's sortable column headers.
 *
 * A header is a real button that names what it does ("Sort by Name"), the header
 * CELL carries `aria-sort`, and exactly one column — the sorted one — wears a small
 * arrow immediately after its title. The arrow is decoration, so it is hidden from
 * the accessibility tree and the direction is read from `aria-sort` instead.
 *
 * `fetch` is stubbed at the wire and the stub SORTS like the server would, so a
 * direction flip is asserted by the rows the grid actually painted rather than by
 * the click that asked for it.
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

function item(overrides: Partial<Item> & { id: string; name: string }): Item {
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
    attrs: null,
    file: null,
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

const ROWS: Item[] = [
  item({
    id: "nd_a",
    name: "alpha.txt",
    file: { size: 1200 } as Item["file"],
    attrs: { mtime: "2026-01-03T00:00:00Z" } as Item["attrs"],
  }),
  item({
    id: "nd_b",
    name: "bravo.md",
    file: { size: 40 } as Item["file"],
    attrs: { mtime: "2026-01-01T00:00:00Z" } as Item["attrs"],
  }),
  item({
    id: "nd_c",
    name: "charlie.sql",
    file: { size: 999_000 } as Item["file"],
    attrs: { mtime: "2026-01-02T00:00:00Z" } as Item["attrs"],
  }),
];

/** What a row sorts by, per `orderBy` field, so the stub can stand in for the server. */
const KEYS: Record<string, (row: Item) => string | number> = {
  name: (row) => row.name,
  mtime: (row) => (row.attrs?.mtime as string | undefined) ?? "",
  size: (row) => (row.file?.size as number | undefined) ?? 0,
  kind: (row) => row.kind,
};

let calls: string[] = [];

/** A server that honours `orderBy`: the grid never re-sorts a page, so the only way
 *  the painted rows can reverse is by the request carrying the flipped direction. */
function stubApi(): void {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      calls.push(url);
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

async function rowsPainted(): Promise<void> {
  await waitFor(() => expect(paintedIds().length).toBe(ROWS.length));
}

function headerFor(column: string): HTMLElement {
  const header = document.querySelector<HTMLElement>(
    `[role="columnheader"][data-column="${column}"]`,
  );
  if (header === null) throw new Error(`no header cell for ${column}`);
  return header;
}

/** Every column that is currently wearing an arrow, with the direction it points. */
function arrows(): [column: string, direction: string][] {
  return Array.from(document.querySelectorAll<HTMLElement>("[data-sort-arrow]")).map((arrow) => [
    arrow.closest("[role='columnheader']")?.getAttribute("data-column") ?? "",
    arrow.getAttribute("data-direction") ?? "",
  ]);
}

beforeEach(() => {
  stubApi();
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("the sort arrow", () => {
  it("sits on the sorted column alone, and moves when another column is sorted", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();

    // The default order is name ascending, so the page opens with the arrow already
    // on Name — a sorted grid that shows no arrow is the bug this pins.
    expect(arrows()).toEqual([["name", "asc"]]);

    await user.click(screen.getByRole("button", { name: "Sort by Modified" }));
    await waitFor(() => expect(arrows()).toEqual([["modified", "desc"]]));
    expect(headerFor("name")).toHaveAttribute("aria-sort", "none");
    expect(headerFor("modified")).toHaveAttribute("aria-sort", "descending");
  });

  it("flips the arrow and reverses the rows on a second click of the same column", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    const ascending = paintedIds();
    expect(ascending).toEqual(["nd_a", "nd_b", "nd_c"]);
    expect(headerFor("name")).toHaveAttribute("aria-sort", "ascending");

    await user.click(screen.getByRole("button", { name: "Sort by Name" }));
    await waitFor(() => expect(paintedIds()).toEqual([...ascending].reverse()));
    expect(arrows()).toEqual([["name", "desc"]]);
    expect(headerFor("name")).toHaveAttribute("aria-sort", "descending");

    await user.click(screen.getByRole("button", { name: "Sort by Name" }));
    await waitFor(() => expect(paintedIds()).toEqual(ascending));
    expect(arrows()).toEqual([["name", "asc"]]);
    expect(headerFor("name")).toHaveAttribute("aria-sort", "ascending");
  });

  it("is hidden from the accessibility tree, which reads the direction off aria-sort", async () => {
    mount();
    await rowsPainted();
    const arrow = document.querySelector("[data-sort-arrow]");
    expect(arrow).not.toBeNull();
    expect(arrow).toHaveAttribute("aria-hidden");
    // The glyph must not leak into the button's name, which says what the click does.
    expect(screen.getByRole("button", { name: "Sort by Name" })).toHaveTextContent("Name");
  });

  it("leaves a column the server cannot order without a sort control", async () => {
    mount();
    await rowsPainted();
    expect(screen.queryByRole("button", { name: "Sort by Owner" })).toBeNull();
    expect(headerFor("owner")).toHaveAttribute("aria-sort", "none");
  });
});

describe("keyboard", () => {
  it.each([
    { key: "{Enter}", id: "Enter" },
    { key: " ", id: "Space" },
  ])("$id on a focused header sorts that column", async ({ key }) => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();

    const header = screen.getByRole("button", { name: "Sort by Size" });
    header.focus();
    expect(header).toHaveFocus();
    await user.keyboard(key);

    await waitFor(() => expect(arrows()).toEqual([["size", "desc"]]));
    expect(headerFor("size")).toHaveAttribute("aria-sort", "descending");
    // Reached the server, not just the local aria state.
    expect(
      calls.some((url) => new URL(url, "http://localhost").searchParams.get("orderBy") === "size desc"),
    ).toBe(true);
  });
});
