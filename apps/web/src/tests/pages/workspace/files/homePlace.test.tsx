import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen, placesFromRoots } from "@/pages/workspace/files/FilesPage";

// `/home` is the ORG-WIDE container: it holds every member's home folder, and an org
// admin outranks it, so her listing of it is a list of colleagues' private folders named
// after their addresses. "Home" in the rail — and the landing `/files` redirects to —
// must be the caller's own `/home/<me>`, which only the server can name and which arrives
// as the drive's `homeId`. These tests pin that the client never takes the container for it.

const HOME_CONTAINER = "nd_home_container";
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

/** The drive's three root children, exactly as the server names them. */
const ROOT_ROWS: Item[] = [
  item({ id: HOME_CONTAINER, name: "home", nameDisplay: "home", pathBytes: "/home", path: "/home" }),
  item({ id: "nd_shared", name: "Shared", nameDisplay: "Shared" }),
  item({ id: "nd_teams", name: "Teams", nameDisplay: "Teams" }),
];

/** Every read the landing page makes, with the drive naming the caller's own home. */
function stubApi(options: { homeId?: string | null } = {}): void {
  const homeId = options.homeId === undefined ? MY_HOME : options.homeId;
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
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", homeId, quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) {
        return answer({ value: ROOT_ROWS, nextMarker: null });
      }
      if (url.includes("/children")) return answer({ value: [], nextMarker: null });
      if (url.includes("/items/")) return answer(item({ id: MY_HOME, nameDisplay: "dana" }));
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

function Where() {
  const location = useLocation();
  return <output data-testid="where">{location.pathname}</output>;
}

function mount(at = "/files") {
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <Where />
        <Routes>
          <Route path="/files" element={<FilesScreen />} />
          <Route path="/files/trash" element={<FilesScreen trash />} />
          <Route path="/files/:nodeId" element={<FilesScreen />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => stubViewport());
afterEach(() => vi.unstubAllGlobals());

describe("the Home place", () => {
  it("is never the root's `home` container, whatever the root listing holds", () => {
    // The container is a signpost over everyone's homes; resolving it as a place is what
    // put a teammate's private folder on the admin's first screen.
    expect(placesFromRoots(ROOT_ROWS)).toEqual({ shared: "nd_shared", teams: "nd_teams" });
  });

  it("lands `/files` on the caller's own home, not on the container", async () => {
    stubApi();
    mount("/files");

    await waitFor(() => {
      expect(screen.getByTestId("where").textContent).toBe(`/files/${MY_HOME}`);
    });
    expect(screen.getByTestId("where").textContent).not.toBe(`/files/${HOME_CONTAINER}`);
  });

  it("routes the rail's Home to the caller's own home", async () => {
    stubApi();
    mount("/files/nd_shared");

    const home = await screen.findByRole("button", { name: /Home/ });
    await waitFor(() => expect(home).toBeEnabled());
    await userEvent.click(home);

    await waitFor(() => {
      expect(screen.getByTestId("where").textContent).toBe(`/files/${MY_HOME}`);
    });
  });

  it("says so rather than opening somebody else's folder when the caller has no home", async () => {
    stubApi({ homeId: null });
    mount("/files");

    expect(await screen.findByText(/do not have a home folder/i)).toBeInTheDocument();
    expect(screen.getByTestId("where").textContent).toBe("/files");
  });
});
