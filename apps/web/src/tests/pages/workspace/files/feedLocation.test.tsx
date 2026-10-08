/**
 * Recent says which folder each row lives in.
 *
 * A feed draws from the whole drive, so its rows are the one listing where the
 * name alone does not identify a row: three files called `qa-report.md` in three
 * different folders arrived as three rows that looked like the page repeating
 * itself. The folder is the column that tells them apart — and it is a way into
 * that folder, which is the shortest route out of a feed.
 *
 * A folder's own listing never draws it: every row in one is already in the
 * folder the reader is standing in.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";
import { locationOf, visibleColumns } from "@/lib/files/columns";

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
    ctag: "ct_1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: { can_read: true, can_write: true, can_share: true, can_delete: true },
    ...over,
  } as Item;
}

const HOME = item({ id: "nd_home", kind: "folder", name: "home", nameDisplay: "home" });

/** Two files of the same name, in two folders, plus one the caller holds a grant
 *  on without holding anything above it — the server names no folder for that
 *  one, and the row must name none either. */
const RECENT: Item[] = [
  item({
    id: "nd_a",
    name: "qa-report.md",
    nameDisplay: "qa-report.md",
    parentId: "nd_reports",
    parentName: "Reports",
  } as Partial<Item>),
  item({
    id: "nd_b",
    name: "qa-report.md",
    nameDisplay: "qa-report.md",
    parentId: "nd_archive",
    parentName: "Archive",
  } as Partial<Item>),
  item({
    id: "nd_c",
    name: "pay.md",
    nameDisplay: "pay.md",
    parentId: "nd_salaries",
  } as Partial<Item>),
];

function stubApi(): { urls: string[] } {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      urls.push(url);
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.includes("/recent")) return answer({ value: RECENT, nextMarker: null });
      if (url.includes("/sharedWithMe")) return answer({ value: [], nextMarker: null });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/search")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_reports/children")) {
        return answer({ value: [RECENT[0]], nextMarker: null });
      }
      if (url.includes("/children")) return answer({ value: [item()], nextMarker: null });
      if (url.includes("/items/nd_reports")) {
        return answer(item({ id: "nd_reports", kind: "folder", nameDisplay: "Reports" }));
      }
      if (url.includes("/items/")) return answer(HOME);
      return answer({});
    }),
  );
  return { urls };
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

function mount(at = "/files/nd_home") {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen platform="other" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Open Recent from the rail and wait for its rows. */
async function openRecent(): Promise<void> {
  const rail = await screen.findByRole("navigation", { name: "Places" });
  const place = await waitFor(() => {
    const button = within(rail).getByRole("button", { name: /Recent/ });
    expect(button).not.toBeDisabled();
    return button;
  });
  await userEvent.click(place);
  await screen.findByText("pay.md");
}

beforeEach(() => stubViewport());
afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("the Location column", () => {
  it("belongs to a feed and to no folder listing", () => {
    const listing = visibleColumns(1600).map((column) => column.id);
    const feed = visibleColumns(1600, { location: true }).map((column) => column.id);

    expect(listing).not.toContain("location");
    expect(feed).toContain("location");
    // The four the listing already draws keep the order and the standing they had.
    expect(feed.filter((id) => id !== "location")).toEqual(listing);
  });

  it("survives the width a 1280 px window actually leaves the listing", () => {
    // 430 px: the rail takes 240 and the details pane 300. Recent rendered Name and
    // Modified there and nothing else, so the one column that tells two rows of the
    // same name apart was the one the width took — and Recent is ordered by Modified
    // anyway, which is the column a reader can infer without being shown it.
    const shown = visibleColumns(430, { location: true }).map((column) => column.id);

    expect(shown).toContain("location");
    expect(shown.indexOf("location")).toBeGreaterThan(-1);
  });

  it("outlasts Modified and Size in a feed, and only in a feed", () => {
    const wentAt = (id: string, options: { location?: boolean }) =>
      [1600, 700, 560, 430, 390, 240, 120].findIndex(
        (width) => !visibleColumns(width, options).some((column) => column.id === id),
      );

    expect(wentAt("location", { location: true })).toBeGreaterThan(
      wentAt("modified", { location: true }),
    );
    expect(wentAt("location", { location: true })).toBeGreaterThan(
      wentAt("size", { location: true }),
    );
    // A folder's own listing is untouched: every row in one is already in the folder
    // the reader is standing in, so Modified keeps the standing it had there.
    expect(wentAt("modified", {})).toBeGreaterThan(wentAt("size", {}));
    expect(visibleColumns(430).map((column) => column.id)).toContain("modified");
  });

  it("goes before the name does when the listing runs out of room", () => {
    const wide = visibleColumns(1600, { location: true }).map((column) => column.id);
    const narrow = visibleColumns(240, { location: true }).map((column) => column.id);

    expect(wide).toContain("location");
    expect(narrow).toEqual(["name"]);
  });

  it("names no folder for a row the server named none for", () => {
    expect(locationOf(RECENT[2])).toBeNull();
    expect(locationOf(RECENT[0])).toEqual({ id: "nd_reports", name: "Reports", home: false });
  });
});

describe("Recent", () => {
  it("heads a Location column and fills it with each row's folder", async () => {
    stubApi();
    mount();
    await openRecent();

    expect(screen.getByRole("columnheader", { name: "Location" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reports" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Archive" })).toBeInTheDocument();
  });

  it("tells two files of the same name apart by the folder each is in", async () => {
    stubApi();
    mount();
    await openRecent();

    // Two rows, one name — so the only thing that distinguishes them is the folder.
    expect(screen.getAllByText("qa-report.md")).toHaveLength(2);
    const cells = screen
      .getAllByRole("gridcell")
      .filter((cell) => cell.getAttribute("data-column") === "location")
      .map((cell) => cell.textContent);
    expect(cells).toEqual(["Reports", "Archive", ""]);
  });

  it("opens the folder when its name is clicked", async () => {
    const stub = stubApi();
    mount();
    await openRecent();

    await userEvent.click(screen.getByRole("button", { name: "Reports" }));

    await waitFor(() =>
      expect(stub.urls.some((url) => url.includes("/items/nd_reports/children"))).toBe(true),
    );
  });

  it("offers nothing to press for a folder the caller may not read", async () => {
    stubApi();
    mount();
    await openRecent();

    // The payslip's row is there; the folder it came out of is not named on it,
    // so there is no link into a folder that would answer 404.
    expect(screen.getByText("pay.md")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "salaries" })).toBeNull();
  });
});
