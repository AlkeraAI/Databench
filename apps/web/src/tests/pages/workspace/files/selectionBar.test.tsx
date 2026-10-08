import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

// The bar under the listing: what is selected, how big it is, and the actions
// that can act on all of it. It exists because a multi-selection had nowhere to
// report itself — the details pane speaks for one row, and the count of what a
// bulk action is about to touch was only ever in the reader's head.

const DRIVE = "dr_1";

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: DRIVE,
    kind: "file",
    name: "report.csv",
    nameDisplay: "report.csv",
    nameEncoding: "utf-8",
    pathBytes: "/home/dana/report.csv",
    path: "/home/dana/report.csv",
    etag: "et_1",
    ctag: "ct_1",
    file: sized(1_000),
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: {
      can_read: true,
      can_write: true,
      can_share: true,
      can_delete: true,
      can_rename: true,
      can_download: true,
    },
    ...over,
  } as Item;
}

/** A file facet with the one field the bar reads; the rest are the server's. */
function sized(size: number): Item["file"] {
  return { mime_type: "text/csv", size, content_hash: "sha256-test", scan_state: "clean" } as Item["file"];
}

const HOME = item({ id: "nd_home", kind: "folder", name: "home", nameDisplay: "home" });
const ONE = item({ id: "nd_a", name: "a.csv", nameDisplay: "a.csv", file: sized(1_200) });
const TWO = item({ id: "nd_b", name: "b.csv", nameDisplay: "b.csv", file: sized(11_200_000) });
/** No size of its own until its stats land — a total must not call that zero. */
const DIR = item({ id: "nd_c", kind: "folder", name: "raw", nameDisplay: "raw", file: null });

let wire: { method: string; url: string }[] = [];

function stubApi(rows: readonly Item[]): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = request ? request.url : String(input);
      const method = request?.method ?? init?.method ?? "GET";
      if (method !== "GET") wire.push({ method, url });
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/permissions")) return answer({ value: [] });
      if (url.includes("/versions")) return answer({ value: [] });
      if (url.includes("/search")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: DRIVE, orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) return answer({ value: [HOME], nextMarker: null });
      if (url.includes("/children")) return answer({ value: rows, nextMarker: null });
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

function mount() {
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/files/nd_home"]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Click `first`, then Shift-click `last`: the range a file manager selects. */
async function selectRange(first: string, last: string): Promise<void> {
  const user = userEvent.setup();
  await user.click(screen.getByText(first));
  await user.keyboard("{Shift>}");
  await user.click(screen.getByText(last));
  await user.keyboard("{/Shift}");
}

function bar(): HTMLElement | null {
  return screen.queryByRole("group", { name: "Selection" });
}

beforeEach(() => {
  wire = [];
  stubViewport();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the selection bar under the listing", () => {
  it("stays out of the way until more than one row is selected", async () => {
    stubApi([ONE, TWO]);
    mount();
    await screen.findByText("a.csv");
    expect(bar()).toBeNull();

    const user = userEvent.setup();
    await user.click(screen.getByText("a.csv"));
    // One row speaks for itself in the details pane; a bar for it would be a
    // second copy of the same facts.
    expect(bar()).toBeNull();
  });

  it("counts the selection and adds up what it weighs", async () => {
    stubApi([ONE, TWO]);
    mount();
    await screen.findByText("a.csv");
    await selectRange("a.csv", "b.csv");

    const summary = await screen.findByRole("group", { name: "Selection" });
    // 1,200 B + 11,200,000 B, in the units every other storage figure uses.
    expect(summary).toHaveTextContent("2 items");
    expect(summary).toHaveTextContent("11.2 MB");
  });

  it("counts a folder whose size is unknown without calling it zero", async () => {
    stubApi([ONE, DIR]);
    mount();
    await screen.findByText("a.csv");
    await selectRange("a.csv", "raw");

    const summary = await screen.findByRole("group", { name: "Selection" });
    expect(summary).toHaveTextContent("2 items");
    // A floor, not a total: the folder's stats have not landed, and calling
    // that zero would understate what a bulk copy is about to move.
    expect(summary).toHaveTextContent("at least 1.2 KB");
  });

  it("offers the bulk actions, and none of the single-row ones", async () => {
    stubApi([ONE, TWO]);
    mount();
    await screen.findByText("a.csv");
    await selectRange("a.csv", "b.csv");

    const summary = await screen.findByRole("group", { name: "Selection" });
    const labels = within(summary)
      .getAllByRole("button")
      .map((button) => button.textContent?.trim());
    expect(labels).toEqual([
      "Download",
      "Copy",
      "Cut",
      "Move to…",
      "Copy to…",
      "Duplicate",
      "Move to trash",
    ]);
    // Rename and Share act on one row: the menu does not offer them here, so
    // neither does the bar — one list, read twice.
    expect(labels).not.toContain("Rename");
    expect(labels).not.toContain("Share…");
  });

  it("runs the action on the whole selection", async () => {
    stubApi([ONE, TWO]);
    mount();
    await screen.findByText("a.csv");
    await selectRange("a.csv", "b.csv");

    const summary = await screen.findByRole("group", { name: "Selection" });
    await userEvent.click(within(summary).getByRole("button", { name: "Move to trash" }));

    // A trash is a DELETE on the node, one per row.
    await waitFor(() => {
      expect(wire.filter((sent) => sent.method === "DELETE").length).toBe(2);
    });
    // Both rows, not just the one the pointer was last on.
    expect(wire.some((sent) => sent.url.includes("nd_a"))).toBe(true);
    expect(wire.some((sent) => sent.url.includes("nd_b"))).toBe(true);
  });

  it("leaves out an action the selection cannot run", async () => {
    const sealed = item({
      id: "nd_b",
      name: "b.csv",
      nameDisplay: "b.csv",
      capabilities: {
        can_read: true,
        can_download: false,
        can_delete: true,
        can_write: true,
      } as Item["capabilities"],
    });
    stubApi([ONE, sealed]);
    mount();
    await screen.findByText("a.csv");
    await selectRange("a.csv", "b.csv");

    const summary = await screen.findByRole("group", { name: "Selection" });
    // A refused action is not a button here: the bar is what CAN be done to the
    // selection, and the menu is where a refusal is explained.
    expect(within(summary).queryByRole("button", { name: "Download" })).toBeNull();
    expect(within(summary).getByRole("button", { name: "Move to trash" })).toBeInTheDocument();
  });
});
