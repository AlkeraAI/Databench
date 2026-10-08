import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

// The drive's `homeId` is the one node `/files` opens sight unseen. The server
// once answered it with a FILE — a receipt the founder had dropped into the
// `/home` container carried her id — and the page opened that PDF as a folder:
// its name in the trail over "0 of 0 shown", and no way out. Whatever the server
// names, a place a person cannot browse is not a place: the page lands on the
// root listing instead and the rail stops offering Home. The folder case stays
// exactly as it was, so a page that bounced every home would fail here too.

const RECEIPT = "Receipt-2505-0695 (1).pdf";
const RECEIPT_ID = "nd_receipt";
const MY_HOME = "nd_my_home";

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "folder",
    name: "thing",
    nameDisplay: "thing",
    nameEncoding: "utf-8",
    pathBytes: "/thing",
    path: "/thing",
    parentId: "nd_root",
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

const ROOT = item({ id: "nd_root", name: "", nameDisplay: "", pathBytes: "/", path: "/" });
const ROOT_ROWS: Item[] = [
  item({ id: "nd_home_container", name: "home", nameDisplay: "home", pathBytes: "/home" }),
  item({ id: "nd_shared", name: "Shared", nameDisplay: "Shared", pathBytes: "/Shared" }),
  item({ id: "nd_teams", name: "Teams", nameDisplay: "Teams", pathBytes: "/Teams" }),
];
const RECEIPT_ITEM = item({
  id: RECEIPT_ID,
  kind: "file",
  name: RECEIPT,
  nameDisplay: RECEIPT,
  pathBytes: `/home/${RECEIPT}`,
  path: `/home/${RECEIPT}`,
  parentId: "nd_home_container",
});
const HOME_FOLDER = item({
  id: MY_HOME,
  name: "dana",
  nameDisplay: "dana",
  pathBytes: "/home/dana",
  path: "/home/dana",
  parentId: "nd_home_container",
});

/** Every read the landing page makes, with the drive naming `home` as the caller's own. */
function stubApi(home: Item): string[] {
  const seen: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      seen.push(url);
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/search")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({
          id: "dr_1",
          orgId: "or_1",
          rootId: "nd_root",
          homeId: home.id,
          quotaBytes: 0,
        });
      }
      if (url.includes("/items/nd_root/children")) {
        return answer({ value: ROOT_ROWS, nextMarker: null });
      }
      if (url.includes("/children")) return answer({ value: [], nextMarker: null });
      if (url.includes("/items/nd_root")) return answer(ROOT);
      if (url.includes(`/items/${home.id}`)) return answer(home);
      if (url.includes("/items/")) return answer(item());
      return answer({});
    }),
  );
  return seen;
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

function Where() {
  const location = useLocation();
  return <output data-testid="where">{location.pathname}</output>;
}

function mount(at = "/files") {
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <Where />
        <Routes>
          <Route path="/files" element={<FilesScreen platform="mac" />} />
          <Route path="/files/trash" element={<FilesScreen trash platform="mac" />} />
          <Route path="/files/:nodeId" element={<FilesScreen platform="mac" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const where = () => screen.getByTestId("where").textContent;

beforeEach(() => stubViewport());
afterEach(() => vi.unstubAllGlobals());

describe("a home the drive names that is not a folder", () => {
  it("lands /files on the root listing instead of opening the file as a folder", async () => {
    const seen = stubApi(RECEIPT_ITEM);
    mount("/files");

    await waitFor(() => expect(where()).toBe("/files/nd_root"));
    // The root's own children are what the browser shows — not an empty page
    // titled after the receipt.
    const grid = await screen.findByRole("treegrid");
    await waitFor(() => expect(within(grid).getByText("Shared")).toBeInTheDocument());
    expect(seen.some((url) => url.includes("/items/nd_root/children"))).toBe(true);
    expect(screen.queryByText(RECEIPT)).toBeNull();
    // A home nobody can browse is not offered: the rail's Home is greyed, so a
    // click cannot send the person back through the same bounce.
    expect(screen.getByRole("button", { name: /Home/ })).toBeDisabled();
  });

  it("bounces a direct visit to the file-as-home to the root listing as well", async () => {
    stubApi(RECEIPT_ITEM);
    mount(`/files/${RECEIPT_ID}`);

    await waitFor(() => expect(where()).toBe("/files/nd_root"));
    // The trail follows the node once the root has loaded: the receipt's name
    // is gone from the page, not merely from the URL.
    const grid = await screen.findByRole("treegrid");
    await waitFor(() => expect(within(grid).getByText("Shared")).toBeInTheDocument());
    await waitFor(() => expect(screen.queryByText(RECEIPT)).toBeNull());
    expect(screen.getByRole("button", { name: /Home/ })).toBeDisabled();
  });

  it("still lands on a home that IS a folder, with the rail's Home lit", async () => {
    stubApi(HOME_FOLDER);
    mount("/files");

    await waitFor(() => expect(where()).toBe(`/files/${MY_HOME}`));
    const home = screen.getByRole("button", { name: /Home/ });
    await waitFor(() => expect(home).toBeEnabled());
    // And it stays there: a folder home is never bounced to the root.
    await screen.findByRole("treegrid");
    expect(where()).toBe(`/files/${MY_HOME}`);
  });
});
