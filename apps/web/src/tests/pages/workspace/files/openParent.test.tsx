// @vitest-environment jsdom
//
// Cmd+Up: the folder above this one.
//
// The trail on screen is the path this SESSION walked, so a link opened cold has
// exactly one segment — and reading the key off the trail alone made the
// keyboard's way up a no-op for every link anybody ever sent. The folder's own
// parent is the answer there.
//
// Where there genuinely is nothing above — the drive's root, a file shared
// without its folder — the key says so. A key that silently does nothing is read
// as a broken keyboard, and on the solo landing it would be read as the folder
// being one keystroke away.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen, NO_PARENT_HERE } from "@/pages/workspace/files/FilesPage";
import { SOLO_LINE } from "@/pages/workspace/files/SoloItem";

const DRIVE = "dr_1";
const ROOT = "nd_root";
const HOME = "nd_home";
const FOLDER = "nd_folder";
const FILE = "nd_file";

function item(over: Partial<Item> = {}): Item {
  return {
    id: FOLDER,
    ino: 1,
    driveId: DRIVE,
    kind: "folder",
    name: "reports",
    nameDisplay: "reports",
    nameEncoding: "utf-8",
    parentId: HOME,
    pathBytes: "/home/dana/reports",
    path: "/home/dana/reports",
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

const NODES: Record<string, Item> = {
  [ROOT]: item({ id: ROOT, name: "/", nameDisplay: "/", parentId: null, path: "/", pathBytes: "/" }),
  [HOME]: item({ id: HOME, name: "dana", nameDisplay: "dana", parentId: ROOT, path: "/home/dana", pathBytes: "/home/dana" }),
  [FOLDER]: item(),
  [FILE]: item({
    id: FILE,
    kind: "file",
    name: "q3.png",
    nameDisplay: "q3.png",
    parentId: FOLDER,
    file: {
      mime_type: "image/png",
      size: 12,
      content_hash: "sha256-test",
      scan_state: "clean",
      provider: "s3",
    },
  }),
};

function stubNetwork(refuse: Record<string, number> = {}): void {
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/content-grants")) {
        return json({ url: "http://files.localhost:8000/c/a.b.c", expiresAt: new Date(Date.now() + 300_000).toISOString(), kind: "file", etag: "et_1" });
      }
      if (url.includes("/leases")) return json([]);
      if (url.includes("/permissions") || url.includes("/versions")) return json({ value: [], nextMarker: null });
      if (url.includes("/search")) return json({ value: [], nextMarker: null });
      if (url.includes("/trash")) return json({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return json({ id: DRIVE, orgId: "or_1", rootId: ROOT, homeId: HOME, quotaBytes: 0 });
      }
      const children = /\/items\/([^/]+)\/children/.exec(url);
      if (children) {
        const id = children[1] as string;
        if (refuse[id]) return json({ code: "files.not_found", message: "no" }, refuse[id]);
        return json({ value: id === FOLDER ? [NODES[FILE] as Item] : [], nextMarker: null });
      }
      const one = /\/items\/([^/?]+)/.exec(url);
      if (one) {
        const id = one[1] as string;
        if (refuse[id]) return json({ code: "files.not_found", message: "no" }, refuse[id]);
        const node = NODES[id];
        return node ? json(node) : json({ code: "files.not_found", message: "no" }, 404);
      }
      return new Response("bytes", { status: 200, headers: { "content-type": "image/png" } });
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

function mount(at: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[at]}>
        <Where />
        <Routes>
          <Route path="/files" element={<FilesScreen platform="mac" />} />
          <Route path="/files/:nodeId" element={<FilesScreen platform="mac" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Cmd+Up, on the element the page hangs its keyboard table off. */
function pressOpenParent(): void {
  const listing = document.querySelector(".alk-files__drop");
  if (!listing) throw new Error("the listing never mounted");
  fireEvent.keyDown(listing, { key: "ArrowUp", metaKey: true });
}

const where = () => screen.getByTestId("where").textContent;

beforeEach(() => stubViewport());
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the way up on the trail", () => {
  it("opens the folder above the one a link landed on", async () => {
    stubNetwork();
    mount(`/files/${FOLDER}`);

    await screen.findByText("q3.png");
    fireEvent.click(await screen.findByRole("button", { name: /^Up( to |$)/u }));

    await waitFor(() => expect(where()).toBe(`/files/${HOME}`));
  });

  it("is offered disabled on the drive's own root", async () => {
    stubNetwork();
    mount(`/files/${ROOT}`);

    await waitFor(() => expect(document.querySelector(".alk-files__drop")).not.toBeNull());
    expect(await screen.findByRole("button", { name: /^Up( to |$)/u })).toBeDisabled();
    expect(where()).toBe(`/files/${ROOT}`);
  });
});

describe("Cmd+Up", () => {
  it("opens the folder above the one a link landed on, with no trail to read it off", async () => {
    stubNetwork();
    mount(`/files/${FOLDER}`);

    await screen.findByText("q3.png");
    pressOpenParent();

    await waitFor(() => expect(where()).toBe(`/files/${HOME}`));
  });

  it("says there is nothing above the drive's own root", async () => {
    stubNetwork();
    mount(`/files/${ROOT}`);

    await waitFor(() => expect(document.querySelector(".alk-files__drop")).not.toBeNull());
    pressOpenParent();

    expect(await screen.findByRole("alert")).toHaveTextContent(NO_PARENT_HERE);
    expect(where()).toBe(`/files/${ROOT}`);
  });

  it("says so for a file shared without its folder, rather than walking into it", async () => {
    // The folder is refused to this reader: the key must not send them to a page
    // that would only refuse them again.
    stubNetwork({ [FOLDER]: 403 });
    mount(`/files/${FILE}`);

    await screen.findByText(SOLO_LINE);
    pressOpenParent();

    expect(await screen.findByRole("alert")).toHaveTextContent(NO_PARENT_HERE);
    expect(where()).toBe(`/files/${FILE}`);
  });
});
