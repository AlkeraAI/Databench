// @vitest-environment jsdom
//
// The page acts on what the reader can see.
//
// `FilesScreen` keeps ONE selection and swaps what it draws underneath it —
// a folder listing, a feed, a search, My leases — so a selection made in the
// listing survived into a feed drawn over it. The menu, the keyboard and the
// details pane then pointed at rows nobody was looking at, and "Move to trash"
// is not a mistake a person can see being made.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

const DRIVE = "dr_1";
const ROOT = "nd_root";
const HOME = "nd_home";

function item(over: Partial<Item> = {}): Item {
  return {
    id: HOME,
    ino: 1,
    driveId: DRIVE,
    kind: "folder",
    name: "dana",
    nameDisplay: "dana",
    nameEncoding: "utf-8",
    parentId: ROOT,
    pathBytes: "/home/dana",
    path: "/home/dana",
    etag: "et_1",
    ctag: "ct_1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    lease: null,
    capabilities: {
      can_read: true,
      can_write: true,
      can_share: true,
      can_delete: true,
      can_rename: true,
      can_download: true,
      can_purge: false,
      can_lease: true,
      can_lease_request: true,
      can_lease_force: false,
    },
    ...over,
  } as Item;
}

/** In the folder the page stands in — the row that must NOT be acted on once a
 *  feed is drawn over the listing. */
const IN_MY_HOME = item({
  id: "nd_budget",
  kind: "file",
  name: "budget.xlsx",
  nameDisplay: "budget.xlsx",
  parentId: HOME,
});
/** The only row "Recent" shows. */
const OPENED_LATELY = item({
  id: "nd_recent",
  kind: "file",
  name: "opened-lately.md",
  nameDisplay: "opened-lately.md",
  parentId: "nd_elsewhere",
});

/** The only row a search finds. It lives in neither of the two folders above, which
 *  is the point: a search reaches across the drive, so its rows are the ones a reader
 *  can act on nowhere else. */
const FOUND_BY_SEARCH = item({
  id: "nd_found",
  kind: "file",
  name: "found-by-search.md",
  nameDisplay: "found-by-search.md",
  parentId: "nd_elsewhere",
  pathBytes: "/home/dana/elsewhere/found-by-search.md",
  path: "/home/dana/elsewhere/found-by-search.md",
});

interface Seen {
  url: string;
  method: string;
}
let calls: Seen[] = [];

function stubNetwork(): void {
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : String(input);
      const method = (input instanceof Request ? input.method : init?.method) ?? "GET";
      calls.push({ url, method: method.toUpperCase() });
      if (url.includes("/sharedWithMe")) return json({ value: [], nextMarker: null });
      if (url.includes("/recent")) return json({ value: [OPENED_LATELY], nextMarker: null });
      if (url.includes("/leases")) return json([]);
      if (url.includes("/permissions") || url.includes("/versions")) {
        return json({ value: [], nextMarker: null });
      }
      if (url.includes("/search")) {
        return json({ value: url.includes("q=found") ? [FOUND_BY_SEARCH] : [], nextMarker: null });
      }
      if (url.includes("/trash")) return json({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return json({ id: DRIVE, orgId: "or_1", rootId: ROOT, homeId: HOME, quotaBytes: 0 });
      }
      if (url.includes(`/items/${ROOT}/children`)) return json({ value: [], nextMarker: null });
      if (url.includes(`/items/${HOME}/children`)) {
        return json({ value: [IN_MY_HOME], nextMarker: null });
      }
      if (url.includes("/children")) return json({ value: [], nextMarker: null });
      if (url.includes(`/items/${ROOT}`)) {
        return json(item({ id: ROOT, name: "/", nameDisplay: "/", parentId: null }));
      }
      if (url.includes("/items/")) return json(item());
      return json({});
    }),
  );
}

function stubViewport(): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        media: query,
        matches: false,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
}

function mount() {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[`/files/${HOME}`]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen platform="other" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Select `budget.xlsx` in the folder listing and prove the page is acting on
 *  it — so the assertions after the switch mean something. */
async function selectTheHiddenRow(): Promise<void> {
  const row = await screen.findByRole("row", { name: /budget\.xlsx/ });
  fireEvent.click(within(row).getAllByRole("gridcell")[0] as HTMLElement);
  fireEvent.contextMenu(within(row).getAllByRole("gridcell")[0] as HTMLElement);
  const menu = await screen.findByRole("menu", { name: "Actions for budget.xlsx" });
  expect(within(menu).getByRole("menuitem", { name: /^Move to trash/ })).not.toHaveAttribute(
    "aria-disabled",
    "true",
  );
  await userEvent.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
}

async function showRecent(): Promise<void> {
  const recent = await screen.findByRole("button", { name: /Recent/ });
  await waitFor(() => expect(recent).toBeEnabled());
  await userEvent.click(recent);
  expect(await screen.findByText("opened-lately.md")).toBeInTheDocument();
  // The listing is the feed's now. The details pane is deliberately NOT part of
  // this check: what it shows while the selection is stale is one of the things
  // the cases below are about.
  const feed = await screen.findByRole("treegrid", { name: "Recent" });
  expect(within(feed).queryByText("budget.xlsx")).toBeNull();
}

beforeEach(() => {
  calls = [];
  stubViewport();
  stubNetwork();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("a feed drawn over the listing", () => {
  it("leaves the hidden listing's selection behind", async () => {
    mount();
    await selectTheHiddenRow();
    await showRecent();

    // The menu opened over the feed, not over one of its rows: with the folder
    // listing's selection carried in it read "Actions for budget.xlsx" and
    // offered to trash a row that is no longer on screen.
    fireEvent.contextMenu(screen.getByRole("treegrid", { name: "Recent" }));
    const menu = await screen.findByRole("menu");
    expect(menu).toHaveAccessibleName("Actions");
    // Nothing is selected on the surface that is showing, so the rows that act
    // on a row are not in the menu at all — the folder's own are.
    expect(within(menu).queryByRole("menuitem", { name: /^Move to trash/ })).toBeNull();
    expect(within(menu).queryByRole("menuitem", { name: /^Rename/ })).toBeNull();
    expect(within(menu).getByRole("menuitem", { name: /^Paste/ })).toBeInTheDocument();
  });

  it("does not let Delete reach a row the reader cannot see", async () => {
    mount();
    await selectTheHiddenRow();
    await showRecent();

    fireEvent.keyDown(screen.getByRole("treegrid", { name: "Recent" }), { key: "Delete" });

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(calls.filter((call) => call.method === "DELETE")).toEqual([]);
    // Nothing was written about the hidden row at all — the reads its details
    // pane made while it was on screen are GETs and are not the question here.
    expect(calls.filter((call) => call.method !== "GET" && call.url.includes("nd_budget"))).toEqual(
      [],
    );
  });

  it("acts on the feed's own row when one is picked there", async () => {
    mount();
    await selectTheHiddenRow();
    await showRecent();

    const row = await screen.findByRole("row", { name: /opened-lately\.md/ });
    fireEvent.contextMenu(within(row).getAllByRole("gridcell")[0] as HTMLElement);

    const menu = await screen.findByRole("menu");
    expect(menu).toHaveAccessibleName("Actions for opened-lately.md");
    expect(within(menu).getByRole("menuitem", { name: /^Move to trash/ })).not.toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("acts on a search result the reader picked", async () => {
    // Everything Rename, Share and Move to trash need is a selected row, and a search
    // reported none: clicking a result selected nothing and the whole menu came back
    // disabled with "Select an item first.", so a file reached only through search
    // could not be renamed, shared or trashed at all.
    mount();
    await screen.findByText("budget.xlsx");
    await userEvent.type(screen.getByRole("searchbox", { name: /search/i }), "found");

    const row = await screen.findByRole("row", { name: /found-by-search\.md/ });
    const cell = within(row).getAllByRole("gridcell")[0] as HTMLElement;
    fireEvent.click(cell);
    fireEvent.contextMenu(cell);

    const menu = await screen.findByRole("menu");
    expect(menu).toHaveAccessibleName("Actions for found-by-search.md");
    expect(within(menu).getByRole("menuitem", { name: /^Rename/ })).not.toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(menu).not.toHaveTextContent("Select an item first.");
  });

  it("leaves the search's selection behind when the search is cleared", async () => {
    mount();
    await screen.findByText("budget.xlsx");
    const box = screen.getByRole("searchbox", { name: /search/i });
    await userEvent.type(box, "found");
    const row = await screen.findByRole("row", { name: /found-by-search\.md/ });
    fireEvent.click(within(row).getAllByRole("gridcell")[0] as HTMLElement);
    await screen.findByRole("row", { name: /found-by-search\.md/ });

    await userEvent.clear(box);
    await screen.findByText("budget.xlsx");

    fireEvent.contextMenu(screen.getByRole("treegrid", { name: /Files|dana/ }));
    const menu = await screen.findByRole("menu");
    expect(menu).toHaveAccessibleName("Actions");
  });

  it("does not carry the feed's selection back into the folder listing", async () => {
    mount();
    await showRecent();
    const row = await screen.findByRole("row", { name: /opened-lately\.md/ });
    fireEvent.contextMenu(within(row).getAllByRole("gridcell")[0] as HTMLElement);
    await screen.findByRole("menu", { name: "Actions for opened-lately.md" });
    await userEvent.keyboard("{Escape}");

    await userEvent.click(await screen.findByRole("button", { name: /^Home/ }));
    expect(await screen.findByText("budget.xlsx")).toBeInTheDocument();

    fireEvent.contextMenu(screen.getByRole("treegrid", { name: /Files|dana/ }));
    const menu = await screen.findByRole("menu");
    expect(menu).toHaveAccessibleName("Actions");
  });
});
