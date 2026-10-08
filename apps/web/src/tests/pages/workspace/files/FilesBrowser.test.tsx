import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { Item } from "@/api/files";
import { FilesBrowser, viewStorageKey } from "@/pages/workspace/files/FilesBrowser";

// The browser driven through the real hooks: `fetch` is stubbed at the wire, so a sort or a
// filter is asserted by the URL the browser actually asked for, and the rows by what the
// treegrid painted from the response.

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
  item({ id: "nd_a", name: "alpha.txt", file: { size: 1200 } as Item["file"] }),
  item({ id: "nd_b", name: "bravo.md", file: { size: 40 } as Item["file"] }),
  item({ id: "nd_c", name: "charlie", kind: "folder" }),
  item({ id: "nd_d", name: "delta.sql", file: { size: 999_000 } as Item["file"] }),
];

/** Every request the browser made, in order. */
let calls: string[] = [];

function stubChildren(rows: Item[] = ROWS): void {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      // openapi-fetch hands `fetch` a Request, whose `toString()` is not its URL.
      const url =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      calls.push(url);
      return Promise.resolve(
        new Response(JSON.stringify({ value: rows, nextMarker: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    }),
  );
}

function mount(props: Partial<React.ComponentProps<typeof FilesBrowser>> = {}) {
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <FilesBrowser driveId={DRIVE} parentId={PARENT} platform="other" {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The row ids the treegrid has painted, in document order. */
function paintedIds(): string[] {
  return Array.from(document.querySelectorAll("[data-row-id]")).map(
    (element) => element.getAttribute("data-row-id") ?? "",
  );
}

async function rowsPainted(): Promise<void> {
  await waitFor(() => expect(paintedIds().length).toBe(ROWS.length));
}

beforeEach(() => {
  stubChildren();
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("the two views", () => {
  it("paints the same rows in the same order in list and in grid", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    const listed = paintedIds();
    expect(listed).toEqual(ROWS.map((row) => row.id));
    // The list view is the one with column headers; the grid has none.
    expect(screen.getAllByRole("columnheader").length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "Grid" }));
    await waitFor(() => expect(screen.queryAllByRole("columnheader")).toHaveLength(0));
    expect(paintedIds()).toEqual(listed);
  });

  it("shows a folder's aggregated dir_stats size and a file's own size", async () => {
    stubChildren([
      item({
        id: "nd_c",
        name: "charlie",
        kind: "folder",
        ...({ dirStats: { totalBytes: 2_400_000 } } as object),
      }),
      item({ id: "nd_a", name: "alpha.txt", file: { size: 1200 } as Item["file"] }),
    ]);
    mount();
    await screen.findByText("alpha.txt");
    expect(screen.getByText("2.4 MB")).toBeInTheDocument();
    expect(screen.getByText("1.2 KB")).toBeInTheDocument();
  });
});

describe("server-side sort", () => {
  const cases = [
    // Name is the default order, so its first click FLIPS rather than re-asking for asc.
    { header: "Name", first: "name desc", second: "name asc" },
    { header: "Kind", first: "kind asc", second: "kind desc" },
    { header: "Size", first: "size desc", second: "size asc" },
    { header: "Modified", first: "mtime desc", second: "mtime asc" },
  ] as const;

  /** Every `orderBy` the browser has asked the server for so far. */
  function ordersAsked(): string[] {
    return calls
      .map((url) => new URL(url, "http://localhost").searchParams.get("orderBy"))
      .filter((value): value is string => value !== null);
  }

  it.each(cases)("clicking $header asks the server for $first then $second", async (row) => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    // The first read carries the default order, never the column about to be clicked.
    expect(ordersAsked()).toEqual(["name asc"]);

    await user.click(screen.getByRole("button", { name: `Sort by ${row.header}` }));
    await waitFor(() => expect(ordersAsked()).toContain(row.first));

    await user.click(screen.getByRole("button", { name: `Sort by ${row.header}` }));
    await waitFor(() => expect(ordersAsked()).toContain(row.second));
  });

  it("marks only the active column with aria-sort", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(screen.getByRole("button", { name: "Sort by Size" }));

    const headers = screen.getAllByRole("columnheader");
    const sorted = headers.filter((h) => h.getAttribute("aria-sort") !== "none");
    expect(sorted).toHaveLength(1);
    expect(sorted[0]).toHaveAttribute("data-column", "size");
    expect(sorted[0]).toHaveAttribute("aria-sort", "descending");
  });

  it("does not re-sort the page it was handed", async () => {
    // The server owns the order; a header click must never shuffle the current rows,
    // because a locally sorted keyset page disagrees with the next page's boundary.
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(screen.getByRole("button", { name: "Sort by Size" }));
    expect(paintedIds()).toEqual(ROWS.map((row) => row.id));
  });
});

describe("keyboard focus and the roving tabindex", () => {
  async function focusFirst(user: ReturnType<typeof userEvent.setup>): Promise<void> {
    await user.click(screen.getByText("alpha.txt"));
  }

  it("arrow keys walk the rows and Home/End jump to the ends", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await focusFirst(user);

    await user.keyboard("{ArrowDown}");
    expect(document.querySelector('[data-row-id="nd_b"]')).toHaveAttribute("tabindex", "0");

    await user.keyboard("{End}");
    expect(document.querySelector('[data-row-id="nd_d"]')).toHaveAttribute("tabindex", "0");
    expect(document.querySelector('[data-row-id="nd_a"]')).toHaveAttribute("tabindex", "-1");

    await user.keyboard("{Home}");
    expect(document.querySelector('[data-row-id="nd_a"]')).toHaveAttribute("tabindex", "0");
  });

  it("announces the selection count and its negative twin", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await focusFirst(user);
    expect(screen.getByText("1 item selected")).toBeInTheDocument();

    await user.keyboard("{Shift>}{ArrowDown}{/Shift}");
    expect(screen.getByText("2 items selected")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.getByText("No items selected")).toBeInTheDocument();
  });

  it("accel+Down opens the focused row instead of moving the focus", async () => {
    // The shortcut table wins over navigation for a modified arrow: Cmd/Ctrl+Down is Open.
    const user = userEvent.setup();
    const onOpen = vi.fn();
    mount({ onOpen });
    await rowsPainted();
    await focusFirst(user);

    await user.keyboard("{Control>}{ArrowDown}{/Control}");
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen.mock.calls[0]?.[0]).toMatchObject({ id: "nd_a" });
    expect(document.querySelector('[data-row-id="nd_a"]')).toHaveAttribute("tabindex", "0");
  });
});

describe("selection through the reducer", () => {
  it("Shift+click takes the range and Ctrl+click toggles one row off", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();

    await user.click(screen.getByText("alpha.txt"));
    await user.keyboard("{Shift>}");
    await user.click(screen.getByText("charlie"));
    await user.keyboard("{/Shift}");
    expect(screen.getByText("3 items selected")).toBeInTheDocument();

    // The negative twin: Ctrl+click on a SELECTED row removes it rather than re-adding it.
    await user.keyboard("{Control>}");
    await user.click(screen.getByText("bravo.md"));
    await user.keyboard("{/Control}");
    expect(screen.getByText("2 items selected")).toBeInTheDocument();
    expect(document.querySelector('[data-row-id="nd_b"]')).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("Ctrl+A selects every visible row and a plain click collapses back to one", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(screen.getByText("alpha.txt"));

    await user.keyboard("{Control>}a{/Control}");
    expect(screen.getByText("4 items selected")).toBeInTheDocument();

    await user.click(screen.getByText("delta.sql"));
    expect(screen.getByText("1 item selected")).toBeInTheDocument();
  });

  it("a right-click takes the row under the pointer into the selection", async () => {
    mount();
    await rowsPainted();

    // Nothing is selected, so the menu that opens on this event would have no target at
    // all: every capability-gated row in it would be disabled. The right-click has to
    // select what it points at first.
    expect(document.querySelector('[data-row-id="nd_b"]')).toHaveAttribute(
      "aria-selected",
      "false",
    );
    fireEvent.contextMenu(screen.getByText("bravo.md"));
    expect(document.querySelector('[data-row-id="nd_b"]')).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("1 item selected")).toBeInTheDocument();
  });

  it("a right-click inside a multi-row selection leaves that selection alone", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(screen.getByText("alpha.txt"));
    await user.keyboard("{Shift>}");
    await user.click(screen.getByText("charlie"));
    await user.keyboard("{/Shift}");
    expect(screen.getByText("3 items selected")).toBeInTheDocument();

    // The negative twin of the row above: acting on "these three" must survive the click
    // that asks for the menu, so a right-click already inside the selection is inert.
    fireEvent.contextMenu(screen.getByText("bravo.md"));
    expect(screen.getByText("3 items selected")).toBeInTheDocument();
  });

  it("carries the selection across a view switch", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();
    await user.click(screen.getByText("alpha.txt"));
    await user.keyboard("{Shift>}");
    await user.click(screen.getByText("bravo.md"));
    await user.keyboard("{/Shift}");

    await user.click(screen.getByRole("button", { name: "Grid" }));
    expect(screen.getByText("2 items selected")).toBeInTheDocument();
    expect(document.querySelector('[data-row-id="nd_a"]')).toHaveAttribute("aria-selected", "true");
  });
});

describe("the per-drive view preference", () => {
  it("survives a remount and does not leak into another drive", async () => {
    const user = userEvent.setup();
    const first = mount();
    await rowsPainted();
    await user.click(screen.getByRole("button", { name: "Grid" }));
    expect(window.localStorage.getItem(viewStorageKey(DRIVE))).toBe("grid");
    first.unmount();

    mount();
    await waitFor(() => expect(screen.queryAllByRole("columnheader")).toHaveLength(0));
    expect(screen.getByRole("button", { name: "Grid" })).toHaveAttribute("aria-pressed", "true");
  });

  it("adopts the stored view when the drive id arrives after the first render", async () => {
    // The real page mounts the browser before the drive query answers, so the
    // preference has to be adopted when the id becomes known. Reading it once at
    // mount reads the `undefined` key, which nothing ever writes.
    window.localStorage.setItem(viewStorageKey(DRIVE), "grid");
    const view = mount({ driveId: undefined });
    expect(screen.getByRole("button", { name: "List" })).toHaveAttribute("aria-pressed", "true");

    view.rerender(
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <MemoryRouter>
          <FilesBrowser driveId={DRIVE} parentId={PARENT} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Grid" })).toHaveAttribute("aria-pressed", "true"),
    );
    await waitFor(() => expect(screen.queryAllByRole("columnheader")).toHaveLength(0));
  });

  it("does not carry one drive's grid into a drive that has no preference", async () => {
    window.localStorage.setItem(viewStorageKey(DRIVE), "grid");
    const view = mount();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Grid" })).toHaveAttribute("aria-pressed", "true"),
    );

    view.rerender(
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <MemoryRouter>
          <FilesBrowser driveId="drv_other" parentId={PARENT} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "List" })).toHaveAttribute("aria-pressed", "true"),
    );
  });

  it("falls back to the list when storage refuses to answer", () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    try {
      mount();
      expect(screen.getByRole("button", { name: "List" })).toHaveAttribute("aria-pressed", "true");
    } finally {
      getItem.mockRestore();
    }
  });
});

describe("breadcrumbs", () => {
  const chain = [
    { id: "nd_root", name: "Files" },
    { id: "nd_home", name: "robin" },
    { id: PARENT, name: "reports" },
  ];

  it("renders the chain with the current folder as the page", async () => {
    mount({ chain });
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(
      within(trail)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["Files", "robin", "reports"]);
    // The name is a box of its own inside the segment, so the mark a segment
    // may wear stays on its line; the segment is what stands for the page.
    expect(
      within(trail).getByText("reports").closest(".alk-files-crumbs__current"),
    ).toHaveAttribute("aria-current", "page");
    expect(within(trail).queryByRole("button", { name: "reports" })).toBeNull();
  });

  it("navigates from an ancestor segment", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    mount({ chain, onNavigate });
    await user.click(screen.getByRole("button", { name: "robin" }));
    expect(onNavigate).toHaveBeenCalledWith("nd_home");
  });

  it("offers every segment as a drop target only when a drop handler is wired", async () => {
    const onDropToSegment = vi.fn();
    const wired = mount({ chain, onDropToSegment });
    expect(document.querySelectorAll('[data-drop-target="folder"]')).toHaveLength(3);
    wired.unmount();

    mount({ chain });
    expect(document.querySelectorAll('[data-drop-target="folder"]')).toHaveLength(0);
  });
});

describe("the treegrid is reachable by Tab", () => {
  /** Every row the browser has painted that is a tab stop. */
  function tabbableRowIds(): string[] {
    return Array.from(document.querySelectorAll('[data-row-id][tabindex="0"]')).map(
      (element) => element.getAttribute("data-row-id") ?? "",
    );
  }

  it("puts the one tab stop on the first row of a freshly loaded listing", async () => {
    mount();
    await rowsPainted();
    // Nothing has been clicked, so the selection carries no focused row; the grid still
    // has to own exactly one tab stop or the keyboard can never enter it.
    expect(tabbableRowIds()).toEqual(["nd_a"]);
  });

  it("moves the single tab stop with the arrow keys once the grid has focus", async () => {
    const user = userEvent.setup();
    mount();
    await rowsPainted();

    const entry = document.querySelector<HTMLElement>('[data-row-id="nd_a"][tabindex="0"]');
    expect(entry).not.toBeNull();
    entry!.focus();

    // The first press claims the fallback row; the second is a real move off it.
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{ArrowDown}");

    await waitFor(() => expect(tabbableRowIds()).toEqual(["nd_b"]));
    expect(document.activeElement?.getAttribute("data-row-id")).toBe("nd_b");
  });

  it("keeps a tab stop on the grid itself when the folder is empty", async () => {
    stubChildren([]);
    mount();
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    const grid = await screen.findByRole("treegrid");
    await waitFor(() => expect(grid).toHaveAttribute("tabindex", "0"));
    expect(document.querySelectorAll("[data-row-id]")).toHaveLength(0);
    expect(grid).toHaveAccessibleName("Files");
  });
});

describe("revealing one row from somewhere else", () => {
  /** `scrollIntoView` is not implemented in jsdom; this both supplies it and
   *  records which row asked for it. */
  function watchScroll(): { rows: string[]; restore: () => void } {
    const rows: string[] = [];
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function record(this: Element) {
      rows.push(this.getAttribute("data-row-id") ?? "");
    };
    return {
      rows,
      restore: () => {
        Element.prototype.scrollIntoView = original;
      },
    };
  }

  function selectedIds(): string[] {
    return Array.from(document.querySelectorAll('[data-row-id][aria-selected="true"]')).map(
      (element) => element.getAttribute("data-row-id") ?? "",
    );
  }

  it("selects only that row, scrolls it into view and focuses it", async () => {
    const scroll = watchScroll();
    try {
      mount({ revealId: "nd_d" });
      await rowsPainted();
      await waitFor(() => expect(selectedIds()).toEqual(["nd_d"]));
      expect(scroll.rows).toContain("nd_d");
      expect(document.activeElement?.getAttribute("data-row-id")).toBe("nd_d");
    } finally {
      scroll.restore();
    }
  });

  it("leaves the listing alone when no row is named", async () => {
    const scroll = watchScroll();
    try {
      mount();
      await rowsPainted();
      expect(selectedIds()).toEqual([]);
      expect(scroll.rows).toEqual([]);
    } finally {
      scroll.restore();
    }
  });

  it("selects nothing for a row this listing does not hold", async () => {
    const scroll = watchScroll();
    try {
      mount({ revealId: "nd_missing" });
      await rowsPainted();
      expect(selectedIds()).toEqual([]);
      expect(scroll.rows).toEqual([]);
    } finally {
      scroll.restore();
    }
  });
});

describe("a row's adornment", () => {
  /** The chip a listing hands in for every row; only two rows have anything. */
  const adornment = (row: Item) =>
    row.id === "nd_a" ? <span data-testid={`chip-${row.id}`}>writing…</span> : null;

  it("rides beside the name in both views, and leaves the name where it was", async () => {
    const user = userEvent.setup();
    mount({ rowAdornment: adornment });
    await rowsPainted();

    // The name is still painted — the adornment is added to the cell, not
    // substituted for it, which is the difference between a chip and a rename.
    const cell = document.querySelector('[data-row-id="nd_a"]') as HTMLElement;
    expect(within(cell).getByText("alpha.txt")).toBeInTheDocument();
    expect(within(cell).getByTestId("chip-nd_a")).toBeInTheDocument();
    // A row with nothing to say wears nothing.
    expect(screen.queryByTestId("chip-nd_b")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Grid" }));
    await rowsPainted();
    const tile = document.querySelector('[data-row-id="nd_a"]') as HTMLElement;
    expect(within(tile).getByText("alpha.txt")).toBeInTheDocument();
    expect(within(tile).getByTestId("chip-nd_a")).toBeInTheDocument();
  });

  it("leaves a chipped row draggable, unlike a row being renamed", async () => {
    const dragDrop = {
      onDragStart: vi.fn(),
      droppable: () => true,
      onDropOnRow: vi.fn(),
    };
    const { unmount } = mount({ rowAdornment: adornment, dragDrop });
    await rowsPainted();

    const lane = (document.querySelector('[data-row-id="nd_a"]') as HTMLElement).closest(
      '[role="row"]',
    ) as HTMLElement;
    expect(lane).toHaveAttribute("draggable", "true");
    fireEvent.dragStart(lane);
    expect(dragDrop.onDragStart).toHaveBeenCalled();
    unmount();

    // The contrast that makes the rule legible: replacing the name cell is how
    // the grid is told a row is being renamed, and a renaming row must not be
    // dragged out from under the input.
    const renaming = {
      onDragStart: vi.fn(),
      droppable: () => true,
      onDropOnRow: vi.fn(),
    };
    mount({
      renderNameOverride: (row: Item) => (row.id === "nd_a" ? <input aria-label="Name" /> : null),
      dragDrop: renaming,
    });
    await rowsPainted();
    const renamingLane = (document.querySelector('[data-row-id="nd_a"]') as HTMLElement).closest(
      '[role="row"]',
    ) as HTMLElement;
    expect(renamingLane).toHaveAttribute("draggable", "false");
  });
});

describe("a row that leaves the listing", () => {
  let client = createQueryClient({ retry: false });

  beforeEach(() => {
    client = createQueryClient({ retry: false });
  });

  /** The browser over a listing the test can take a row out of, the way a trash
   *  or a move out of the folder does. */
  function browser(gone: string | null) {
    return (
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <FilesBrowser
            driveId={DRIVE}
            parentId={PARENT}
            platform="other"
            omit={gone === null ? undefined : (row) => row.id === gone}
          />
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  /** The row carrying the roving tab stop. */
  function tabStop(): HTMLElement | null {
    return document.querySelector<HTMLElement>('[data-row-id][tabindex="0"]');
  }

  it("hands the focus and the selection to the next row, not back to the click anchor", async () => {
    const user = userEvent.setup();
    const { rerender } = render(browser(null));
    await rowsPainted();

    // Click the first row, then arrow down to the third. The anchor stays behind
    // at the first, which is where a reset selection would snap back to.
    await user.click(document.querySelector('[data-row-id="nd_a"]') as HTMLElement);
    await user.keyboard("{ArrowDown}{ArrowDown}");
    await waitFor(() => expect(document.activeElement).toHaveAttribute("data-row-id", "nd_c"));

    rerender(browser("nd_c"));
    await waitFor(() => expect(paintedIds()).toEqual(["nd_a", "nd_b", "nd_d"]));

    // The neighbour below, focused in the DOM — not the document, not the anchor.
    await waitFor(() => expect(document.activeElement).toHaveAttribute("data-row-id", "nd_d"));
    expect(tabStop()).toHaveAttribute("data-row-id", "nd_d");
    expect(document.querySelector('[data-row-id="nd_d"]')).toHaveAttribute("aria-selected", "true");
    expect(document.querySelector('[data-row-id="nd_a"]')).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("falls back to the row above when the last one goes", async () => {
    const user = userEvent.setup();
    const { rerender } = render(browser(null));
    await rowsPainted();

    await user.click(document.querySelector('[data-row-id="nd_d"]') as HTMLElement);
    await waitFor(() => expect(document.activeElement).toHaveAttribute("data-row-id", "nd_d"));

    rerender(browser("nd_d"));
    await waitFor(() => expect(document.activeElement).toHaveAttribute("data-row-id", "nd_c"));
    expect(tabStop()).toHaveAttribute("data-row-id", "nd_c");
  });
});
