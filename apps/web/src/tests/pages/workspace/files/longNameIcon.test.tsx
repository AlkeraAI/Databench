/**
 * A row's mark keeps its size beside a long name.
 *
 * The folder glyph was a flex item that shrank with the rest of the name cell,
 * so beside a 200-character name it was squeezed to a dot. The name is the one
 * that gives way now, behind its ellipsis. jsdom lays nothing out, so the rule
 * is read off the sheet and the markup is rendered to prove the glyph sits
 * where the rule keys.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

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

const LONG_NAME = "x".repeat(200);
const LONG = item({ id: "nd_long", kind: "folder", name: LONG_NAME, nameDisplay: LONG_NAME });
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
      if (url.includes("/children")) return json({ value: [LONG], nextMarker: null });
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


describe("a row with a 200-character name", () => {
  it("keeps its folder glyph at full size and lets the name give way", async () => {
    render(
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <MemoryRouter initialEntries={["/files/nd_home"]}>
          <Routes>
            <Route path="/files/:nodeId" element={<FilesScreen />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const name = await screen.findByText(LONG_NAME);
    const cell = name.closest<HTMLElement>('[role="gridcell"]')!;
    const glyph = cell.querySelector(".alk-language-icon");
    expect(glyph?.parentElement).toBe(cell);
    expect(within(cell).getByText(LONG_NAME)).toHaveClass("alk-files-grid__name");

    const sheet = readFileSync(
      join(process.cwd(), "src/pages/workspace/files/treegrid.css"),
      "utf8",
    ).replace(/\/\*[\s\S]*?\*\//g, "");
    const rule = sheet
      .split("}")
      .find((chunk) =>
        chunk
          .split("{")[0]!
          .split(",")
          .map((selector) => selector.trim())
          .includes(".alk-files-grid__cell > .alk-language-icon"),
      );
    expect(rule, "no rule holds the row glyph's size").toBeDefined();
    expect(rule).toMatch(/flex-shrink:\s*0/);
    const nameRule = sheet.split("}").find((chunk) => chunk.split("{")[0]!.trim() === ".alk-files-grid__name");
    expect(nameRule).toMatch(/min-width:\s*0/);
  });
});
