/**
 * The Delete key trashes the selected row.
 *
 * On a Mac only Cmd+Backspace did; the forward-delete key did nothing at all,
 * so a person who reached for it was left looking at the row they meant to
 * throw away. Both now take the path the menu's Move to trash takes.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";
import type { Platform } from "@/pages/workspace/files/state/shortcuts";

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

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });


interface Write {
  method: string;
  url: string;
}

function stubApi(writes: Write[]): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = request ? request.url : String(input);
      const method = request?.method ?? init?.method ?? "GET";
      if (method !== "GET") {
        writes.push({ method, url });
        return json({ id: "op_1", kind: "trash", state: "done", undoable: true });
      }
      if (url.includes("/leases")) return json([]);
      if (url.includes("/permissions")) return json({ value: [] });
      if (url.includes("/versions")) return json({ value: [] });
      if (url.includes("/search")) return json({ value: [], nextMarker: null });
      if (url.includes("/trash")) return json({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return json({ id: DRIVE, orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) return json({ value: [HOME], nextMarker: null });
      if (url.includes("/children")) return json({ value: [item()], nextMarker: null });
      if (url.includes("/items/")) return json(HOME);
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

let writes: Write[] = [];

beforeEach(() => {
  writes = [];
  stubViewport();
  stubApi(writes);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function selectRow(platform: Platform): Promise<void> {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/files/nd_home"]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen platform={platform} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const row = await screen.findByRole("row", { name: /report\.csv/ });
  await userEvent.click(within(row).getByText("report.csv"));
  await waitFor(() => expect(row).toHaveAttribute("aria-selected", "true"));
}

const trashes = () => writes.filter((write) => write.method === "DELETE" || write.url.includes("/bulk"));

describe("trashing the selected row from the keyboard on a Mac", () => {
  it.each([
    ["the forward-delete key", "{Delete}"],
    ["Cmd+Backspace", "{Meta>}{Backspace}{/Meta}"],
  ])("%s moves it to the trash", async (_label, keys) => {
    await selectRow("mac");
    await userEvent.keyboard(keys);
    await waitFor(() => expect(trashes()).toHaveLength(1));
  });

  it("a bare Backspace still does nothing, as in Finder", async () => {
    await selectRow("mac");
    await userEvent.keyboard("{Backspace}");
    // Give a trash that should not happen the same chance to be sent.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(trashes()).toEqual([]);
  });
});
