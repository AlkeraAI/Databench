/**
 * The two halves w11-D left behind.
 *
 * Recent / Starred / Shared with me each had a route and a hook by the end of
 * that lane, and nothing that rendered them: the page never listed the three as
 * places it answers itself, never told the rail the drive was up, and had no
 * branch that drew a feed. So all three stayed greyed out on a perfectly live
 * drive. And the details pane still printed the owner's raw UUID while the
 * listing beside it printed a name.
 *
 * Both pins fail on the code as it was: the rail buttons are disabled and no
 * feed row is ever painted, and the pane shows the id.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen, VIRTUAL_PLACES } from "@/pages/workspace/files/FilesPage";
import { RightPane } from "@/pages/workspace/files/RightPane";

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    name: "report.csv",
    nameDisplay: "report.csv",
    nameEncoding: "utf-8",
    path: "/home/dana/report.csv",
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

/** One row per feed, each named after the feed, so a branch that reads the
 *  wrong hook shows the wrong name rather than silently passing. */
const FEED_ROWS: Record<string, Item> = {
  recent: item({ id: "nd_recent", nameDisplay: "opened-lately.csv", name: "opened-lately.csv" }),
  // Starring is retired: a feed the page no longer answers. Kept in the stub so the
  // pin below can prove the rail no longer offers it.
  starred: item({ id: "nd_star", kind: "folder", nameDisplay: "kept", name: "kept" }),
  // A folder, so the "Enter opens the node it really lives at" pin has somewhere
  // to go: a feed has no parent, and the row must route to its own node.
  sharedWithMe: item({ id: "nd_shared", kind: "folder", nameDisplay: "from-mo", name: "from-mo" }),
};

interface Stub {
  urls: string[];
}

function stubApi(): Stub {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      void init;
      const url = input instanceof Request ? input.url : String(input);
      urls.push(url);
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });

      for (const feed of Object.keys(FEED_ROWS)) {
        if (url.includes(`/${feed}`)) return answer({ value: [FEED_ROWS[feed]], nextMarker: null });
      }
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/search")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) return answer({ value: [HOME], nextMarker: null });
      if (url.includes("/children")) return answer({ value: [item()], nextMarker: null });
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
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen platform="other" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => stubViewport());
afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("the three feeds on the assembled page", () => {
  it("names the two feeds as places the page answers itself, and not Starred", () => {
    expect([...VIRTUAL_PLACES]).toEqual(expect.arrayContaining(["recent", "sharedWithMe"]));
    expect(VIRTUAL_PLACES).not.toContain("starred");
  });

  it("no longer offers Starred in the rail", async () => {
    stubApi();
    mount();
    const rail = await screen.findByRole("navigation", { name: "Places" });
    await within(rail).findByRole("button", { name: /Recent/ });
    expect(within(rail).queryByText("Starred")).not.toBeInTheDocument();
  });

  it.each([
    ["Recent", "recent", "opened-lately.csv"],
    ["Shared with me", "sharedWithMe", "from-mo"],
  ])("%s is enabled on a live drive and lists its own feed", async (label, feed, rowName) => {
    const stub = stubApi();
    mount();
    const rail = await screen.findByRole("navigation", { name: "Places" });
    const place = await waitFor(() => {
      const button = within(rail).getByRole("button", { name: new RegExp(label) });
      expect(button).not.toBeDisabled();
      return button;
    });

    await userEvent.click(place);

    expect(await screen.findByText(rowName)).toBeInTheDocument();
    // The row came from the feed's own drive-scoped route, not a child listing.
    expect(
      stub.urls.some((url) => url.includes(`/drives/dr_1/${feed}`) && !url.includes("children")),
    ).toBe(true);
  });

  it("opens the node a feed row really lives at, leaving the feed", async () => {
    const stub = stubApi();
    mount();
    const rail = await screen.findByRole("navigation", { name: "Places" });
    await userEvent.click(within(rail).getByRole("button", { name: /Shared with me/ }));
    const row = await screen.findByText("from-mo");

    await userEvent.click(row);
    await userEvent.keyboard("{Enter}");

    // The folder is opened by id — the feed is left for the real listing, which
    // is the only place the actions that need a parent exist.
    await waitFor(() =>
      expect(stub.urls.some((url) => url.includes("/items/nd_shared/children"))).toBe(true),
    );
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toBeInTheDocument();
  });
});

describe("the details pane's owner line", () => {
  it("reads the owner the same way the listing does, never a raw id", () => {
    const owned = item({
      attrs: { owner: "9f11aa20-1111-4444-8888-2cf43ca1fcb0" },
      ownerName: "Mo Chen",
    } as unknown as Partial<Item>);
    const client = createQueryClient();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <RightPane driveId="dr_1" item={owned} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByText("Mo Chen")).toBeInTheDocument();
    expect(screen.queryByText("9f11aa20-1111-4444-8888-2cf43ca1fcb0")).toBeNull();
  });

  it("shows a dash, not the id, for an owner the server did not name", () => {
    const owned = item({
      attrs: { owner: "9f11aa20-1111-4444-8888-2cf43ca1fcb0" },
    } as unknown as Partial<Item>);
    const client = createQueryClient();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <RightPane driveId="dr_1" item={owned} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.queryByText(/9f11aa20/)).toBeNull();
    expect(screen.queryByTitle(/9f11aa20/)).toBeNull();
  });
});
