/**
 * What a feed row is worth after the data under it moves.
 *
 * Recent and Shared with me are the two surfaces whose rows the page does not
 * own: the feed fetches them and hands them up. Two things have to hold there
 * that hold in a folder listing — the details pane has to be showing THIS
 * second's row rather than the object captured when it was clicked, and Rename
 * (which the row menu offers on every surface) has to actually open an editor
 * and not leave a pending rename behind for some later listing to inherit.
 */

import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

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
    parentId: "nd_home",
    etag: "et_1",
    ctag: "ct_1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: {
      can_read: true,
      can_write: true,
      can_rename: true,
      can_share: true,
      can_delete: true,
      refusals: {},
    },
    ...over,
  } as unknown as Item;
}

const HOME = item({ id: "nd_home", kind: "folder", name: "home", nameDisplay: "home" });

/** The one row the feed hands up. Renaming it rewrites what the server answers,
 *  the way a rename anywhere else in the drive does. */
let feedRow: Item;

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      void init;
      const url = input instanceof Request ? input.url : String(input);
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });

      if (url.includes("/recent")) return answer({ value: [feedRow], nextMarker: null });
      if (url.includes("/sharedWithMe")) return answer({ value: [], nextMarker: null });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/search")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({
          id: "dr_1",
          orgId: "or_1",
          rootId: "nd_root",
          homeId: "nd_home",
          quotaBytes: 0,
        });
      }
      if (url.includes("/items/nd_root/children")) {
        return answer({ value: [HOME], nextMarker: null });
      }
      // The feed row is a real node: it LIVES in home, so the folder listing is
      // a later listing that holds the very same id.
      if (url.includes("/items/nd_home/children")) {
        return answer({ value: [feedRow], nextMarker: null });
      }
      if (url.includes("/children")) return answer({ value: [], nextMarker: null });
      if (url.includes("/items/")) return answer(HOME);
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

function mount(): QueryClient {
  const client = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/files/nd_home"]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen platform="other" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

/** The details pane itself — the page wraps it in a slot that carries the same
 *  label, so this takes the innermost. */
const pane = (): HTMLElement =>
  screen.getAllByRole("complementary", { name: "Details" }).at(-1) as HTMLElement;

const paneName = (): string =>
  (within(pane()).getByRole("heading", { level: 2 }).textContent ?? "").trim();

async function openRecent(): Promise<void> {
  const rail = await screen.findByRole("navigation", { name: "Places" });
  await userEvent.click(within(rail).getByRole("button", { name: /Recent/ }));
}

beforeEach(() => {
  feedRow = item({ id: "nd_recent", name: "opened-lately.csv", nameDisplay: "opened-lately.csv" });
  stubViewport();
  stubApi();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("the details pane on a feed", () => {
  it("shows the renamed row after a refetch, with no second click", async () => {
    const client = mount();
    await openRecent();

    const row = await screen.findByText("opened-lately.csv");
    await userEvent.click(row);
    await waitFor(() => expect(paneName()).toBe("opened-lately.csv"));

    // Renamed somewhere else — the drive answers the new name and the Files
    // reads are invalidated, exactly as a rename's own invalidation does.
    feedRow = item({ id: "nd_recent", name: "quarter-close.csv", nameDisplay: "quarter-close.csv" });
    await client.invalidateQueries();

    // The listing under the pane follows…
    expect(await screen.findByText("quarter-close.csv")).toBeInTheDocument();
    // …and so does the pane, which is looking at the same row.
    await waitFor(() => expect(paneName()).toBe("quarter-close.csv"));
    expect(pane().textContent).not.toContain("opened-lately.csv");
  });
});

describe("Rename on a feed row", () => {
  it("opens the editor on the row the reader picked", async () => {
    mount();
    await openRecent();
    const cell = (await screen.findByText("opened-lately.csv")).closest('[role="gridcell"]');
    expect(cell).not.toBeNull();

    fireEvent.contextMenu(cell as HTMLElement);
    const menu = await screen.findByRole("menu");
    fireEvent.click(within(menu).getByRole("menuitem", { name: /^Rename/ }));

    const field = await screen.findByRole("textbox");
    expect((field as HTMLInputElement).value).toBe("opened-lately.csv");
  });

  it("leaves no pending rename behind for the next listing to inherit", async () => {
    mount();
    await openRecent();
    const cell = (await screen.findByText("opened-lately.csv")).closest('[role="gridcell"]');

    fireEvent.contextMenu(cell as HTMLElement);
    const menu = await screen.findByRole("menu");
    fireEvent.click(within(menu).getByRole("menuitem", { name: /^Rename/ }));

    // Away from the feed, into the folder the row really lives in — which lists
    // that very node. The half-started rename does not travel with the reader.
    const rail = screen.getByRole("navigation", { name: "Places" });
    await userEvent.click(within(rail).getByRole("button", { name: /Home/ }));

    await screen.findByRole("navigation", { name: "Breadcrumb" });
    expect(screen.queryByRole("textbox")).toBeNull();
    // …and the row it would have ambushed is drawn as a row, name and all.
    expect(screen.getByText("opened-lately.csv")).toBeInTheDocument();
  });
});
