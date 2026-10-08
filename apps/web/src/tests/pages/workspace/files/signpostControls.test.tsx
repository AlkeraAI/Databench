import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

// `/`, `home/` and `Teams/` are signposts: the only rows under them are the
// homes and team folders the server's own ensure creates, and every direct
// write there is refused with `files.container_readonly`. An org admin is a
// writer on all three by descent, so `capabilities.can_write` comes back true
// and the page offered New folder, Upload files and Upload folder on a folder
// that can never take one — a button whose only outcome is a 422. What is
// pinned here is that the bar offers them inside a real folder and nowhere
// else.

const DRIVE = {
  id: "dr_1",
  orgId: "or_1",
  rootId: "nd_root",
  homeId: "nd_home",
  quotaBytes: 100_000_000_000,
};

function folder(id: string, name: string, parentId: string | null): Item {
  return {
    id,
    ino: 3,
    driveId: "dr_1",
    kind: "folder",
    name,
    nameDisplay: name,
    nameEncoding: "utf-8",
    pathBytes: `/${name}`,
    path: null,
    parentId,
    etag: "0",
    ctag: "0",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    // What an org admin really gets on a signpost: writer by descent. The
    // refusal is the container's, so the page cannot key off this alone.
    capabilities: {
      can_read: true,
      can_write: true,
      can_share: true,
      can_delete: true,
      can_rename: true,
      can_download: true,
    },
  } as unknown as Item;
}

const NODES: Record<string, Item> = {
  nd_root: folder("nd_root", "", null),
  nd_home: folder("nd_home", "home", "nd_root"),
  nd_teams: folder("nd_teams", "Teams", "nd_root"),
  nd_shared: folder("nd_shared", "Shared", "nd_root"),
  nd_mine: folder("nd_mine", "ana", "nd_home"),
};

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const answer = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });
      if (/\/files\/drives\/?(\?|$)/.test(url)) return answer(DRIVE);
      if (url.includes("/items/nd_root/children")) {
        return answer({
          value: [NODES.nd_home, NODES.nd_teams, NODES.nd_shared],
          nextMarker: null,
        });
      }
      if (url.includes("/children")) return answer({ value: [], nextMarker: null });
      if (url.includes("/leases")) return answer([]);
      const node = Object.keys(NODES).find((id) => url.includes(`/items/${id}`));
      if (node) return answer(NODES[node]);
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
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen platform="mac" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const CREATE_LABELS = ["New folder", "Upload files", "Upload folder"] as const;

beforeEach(() => {
  stubViewport();
  stubApi();
});
afterEach(() => vi.unstubAllGlobals());

describe("the create controls follow the container, not the role", () => {
  it.each([
    ["the drive root", "nd_root"],
    ["home/", "nd_home"],
    ["Teams/", "nd_teams"],
  ])("offers no way to add something inside %s", async (_where, nodeId) => {
    mount(`/files/${nodeId}`);
    await waitFor(() => expect(screen.getByRole("treegrid")).toBeTruthy());
    // The bar is drawn before the folder read lands, so the rule is asserted
    // once the page knows which folder it is on.
    await waitFor(() => {
      for (const label of CREATE_LABELS) {
        expect(screen.queryByRole("button", { name: label })).toBeNull();
      }
    });
  });

  it.each([
    ["Shared, a real folder under the root", "nd_shared"],
    ["a member's own home", "nd_mine"],
  ])("still offers all three inside %s", async (_where, nodeId) => {
    mount(`/files/${nodeId}`);
    await waitFor(() => expect(screen.getByRole("treegrid")).toBeTruthy());
    for (const label of CREATE_LABELS) {
      expect(screen.getByRole("button", { name: label })).toBeTruthy();
    }
  });
});
