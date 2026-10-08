import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen, currentPlace } from "@/pages/workspace/files/FilesPage";

// The rail names PLACES. Which one it marks is a fact about where the browser is
// standing, not about what was last clicked — so landing on Home marks Home even
// though nobody touched the rail, and a folder inside Home marks nothing at all.

const MY_HOME = "nd_my_home";
const IN_HOME = "nd_inside_home";
const SHARED = "nd_shared";

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "folder",
    name: "thing",
    nameDisplay: "thing",
    nameEncoding: "utf-8",
    pathBytes: "/home/dana/thing",
    path: "/home/dana/thing",
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

const ROOT_ROWS: Item[] = [
  item({ id: "nd_home_container", name: "home", nameDisplay: "home" }),
  item({ id: SHARED, name: "Shared", nameDisplay: "Shared" }),
  item({ id: "nd_teams", name: "Teams", nameDisplay: "Teams" }),
];

const NODES: Record<string, Item> = {
  [MY_HOME]: item({ id: MY_HOME, name: "dana", nameDisplay: "dana", parentId: "nd_home_container" }),
  [IN_HOME]: item({ id: IN_HOME, name: "Notes", nameDisplay: "Notes", parentId: MY_HOME }),
  [SHARED]: item({ id: SHARED, name: "Shared", nameDisplay: "Shared", parentId: "nd_root" }),
};

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/search")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", homeId: MY_HOME, quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) return answer({ value: ROOT_ROWS, nextMarker: null });
      if (url.includes("/children")) return answer({ value: [], nextMarker: null });
      const match = /\/items\/([^/?]+)/.exec(url);
      if (match) return answer(NODES[match[1] ?? ""] ?? item({ id: match[1] }));
      return answer({});
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

function mount(at: string) {
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path="/files" element={<FilesScreen />} />
          <Route path="/files/trash" element={<FilesScreen trash />} />
          <Route path="/files/:nodeId" element={<FilesScreen />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Every rail entry that is marked as the page's current place. */
function markedPlaces(): string[] {
  return screen
    .getAllByRole("navigation", { name: "Places" })
    .flatMap((rail) => Array.from(rail.querySelectorAll('[aria-current="page"]')))
    .map((node) => node.textContent?.trim() ?? "");
}

beforeEach(() => stubViewport());
afterEach(() => vi.unstubAllGlobals());

describe("which rail place the page is standing on", () => {
  it("marks Home when the node IS the drive's home", async () => {
    stubApi();
    mount(`/files/${MY_HOME}`);

    await waitFor(() => expect(markedPlaces()).toEqual(["Home"]));
  });

  it("marks nothing when the node is a folder INSIDE home", async () => {
    stubApi();
    mount(`/files/${IN_HOME}`);

    // The listing has settled — Home is on the rail and reachable — and still nothing is current.
    await waitFor(() => expect(screen.getByRole("button", { name: /Home/ })).toBeEnabled());
    expect(markedPlaces()).toEqual([]);
  });

  it("marks only Trash in the trash view", async () => {
    stubApi();
    mount("/files/trash");

    await waitFor(() => expect(markedPlaces()).toEqual(["Trash"]));
  });
});

describe("currentPlace", () => {
  const places = { home: MY_HOME, shared: SHARED } as const;

  it("is the place whose own node is on screen", () => {
    expect(currentPlace({ trash: false, nodeId: MY_HOME, place: undefined, placeNodeIds: places })).toBe("home");
    expect(currentPlace({ trash: false, nodeId: SHARED, place: undefined, placeNodeIds: places })).toBe("shared");
  });

  it("is nothing for a descendant of a place", () => {
    expect(
      currentPlace({ trash: false, nodeId: IN_HOME, place: undefined, placeNodeIds: places }),
    ).toBeUndefined();
  });

  it("is Trash in the trash view whatever node the URL last carried", () => {
    expect(currentPlace({ trash: true, nodeId: MY_HOME, place: undefined, placeNodeIds: places })).toBe("trash");
  });

  it("is the feed the page is rendering, which has no node of its own", () => {
    expect(currentPlace({ trash: false, nodeId: MY_HOME, place: "recent", placeNodeIds: places })).toBe("recent");
    expect(currentPlace({ trash: false, nodeId: IN_HOME, place: "sharedWithMe", placeNodeIds: places })).toBe(
      "sharedWithMe",
    );
  });

  it("is nothing before the drive has named a single place", () => {
    expect(
      currentPlace({ trash: false, nodeId: MY_HOME, place: undefined, placeNodeIds: undefined }),
    ).toBeUndefined();
  });
});
