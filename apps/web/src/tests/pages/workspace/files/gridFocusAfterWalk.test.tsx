// @vitest-environment jsdom
//
// The keyboard has to survive walking into a folder.
//
// Cmd+Down opens the row under the roving tab stop. The row it was on is not in
// the folder it opened, so the listing reconciles the focus away to nothing, the
// browser drops focus out to the document — and the very next keystroke reaches
// nothing at all. Cmd+Up, the way back out, was swallowed; a person had to click
// a row before the keyboard worked again.
//
// The fix is where the roving tab stop lives, so this drives it from the page:
// the keystrokes go to whatever actually holds the focus, never to a container
// picked by the test.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

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
  [FILE]: item({ id: FILE, kind: "file", name: "q3.txt", nameDisplay: "q3.txt", parentId: FOLDER }),
};

const CHILDREN: Record<string, Item[]> = {
  [ROOT]: [NODES[HOME] as Item],
  [HOME]: [NODES[FOLDER] as Item],
  [FOLDER]: [NODES[FILE] as Item],
};

function stubNetwork(): void {
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/leases")) return json([]);
      if (url.includes("/permissions") || url.includes("/versions")) {
        return json({ value: [], nextMarker: null });
      }
      if (url.includes("/search")) return json({ value: [], nextMarker: null });
      if (url.includes("/trash")) return json({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return json({ id: DRIVE, orgId: "or_1", rootId: ROOT, homeId: HOME, quotaBytes: 0 });
      }
      const children = /\/items\/([^/]+)\/children/.exec(url);
      if (children) {
        return json({ value: CHILDREN[children[1] as string] ?? [], nextMarker: null });
      }
      const one = /\/items\/([^/?]+)/.exec(url);
      if (one) {
        const node = NODES[one[1] as string];
        return node ? json(node) : json({ code: "files.not_found", message: "no" }, 404);
      }
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
          <Route path="/files/:nodeId" element={<FilesScreen platform="mac" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const where = () => screen.getByTestId("where").textContent;

/** Click the one folder row, the way a pointer does — which focuses it. */
async function pickTheRow(): Promise<void> {
  const row = await screen.findByText("reports");
  await userEvent.click(row);
  await waitFor(() => expect(document.activeElement?.closest("[data-row-id]")).not.toBeNull());
}

/** The keystroke goes wherever the focus actually is — which is the whole point. */
function press(key: string, modifiers: Record<string, boolean> = {}): void {
  fireEvent.keyDown(document.activeElement ?? document.body, { key, ...modifiers });
}

beforeEach(() => {
  stubViewport();
  stubNetwork();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("walking into a folder from the keyboard", () => {
  it("leaves the focus on a row of the folder it opened", async () => {
    mount(`/files/${HOME}`);
    await pickTheRow();

    press("ArrowDown", { metaKey: true });
    await waitFor(() => expect(where()).toBe(`/files/${FOLDER}`));
    await screen.findByText("q3.txt");

    await waitFor(() => {
      const active = document.activeElement as HTMLElement | null;
      expect(active).not.toBe(document.body);
      expect(active?.closest('[role="treegrid"]')).not.toBeNull();
    });
  });

  it("answers the next Cmd+Up without anything being clicked in between", async () => {
    mount(`/files/${HOME}`);
    await pickTheRow();

    press("ArrowDown", { metaKey: true });
    await waitFor(() => expect(where()).toBe(`/files/${FOLDER}`));
    await screen.findByText("q3.txt");

    press("ArrowUp", { metaKey: true });
    await waitFor(() => expect(where()).toBe(`/files/${HOME}`));
  });

  it("does not take a focus something else has claimed", async () => {
    // The listing arriving must not pull the caret out of a control the person
    // is using. The search field is outside the grid and keeps what it took.
    mount(`/files/${HOME}`);
    await pickTheRow();
    press("ArrowDown", { metaKey: true });
    await waitFor(() => expect(where()).toBe(`/files/${FOLDER}`));

    const search = screen.getByRole("searchbox");
    search.focus();
    await screen.findByText("q3.txt");
    await waitFor(() => expect(document.activeElement).toBe(search));
  });
});
