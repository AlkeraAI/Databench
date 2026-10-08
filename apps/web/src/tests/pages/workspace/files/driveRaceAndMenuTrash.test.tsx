import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FILES_NARROW_QUERY, FilesScreen } from "@/pages/workspace/files/FilesPage";

// Two customer-visible failures of the assembled page, driven through the real
// route over a scripted network:
//
//  * the drive read settling AFTER the listing's first paint (or a persisted
//    filter set applied before the drive id exists) must never leave the page
//    reading "0 shown" with no crumb and a dead rail — it waits, then converges;
//  * the context menu's Move to trash issues the SAME DELETE the keyboard does —
//    the right node, `If-Match` at the row's etag — and a refusal puts the row
//    back instead of leaving a phantom deletion on screen.

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    name: "report.csv",
    nameDisplay: "report.csv",
    nameEncoding: "utf-8",
    pathBytes: "/home/dana/report.csv",
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
      can_share: true,
      can_delete: true,
      can_rename: true,
      can_download: true,
    },
    ...over,
  } as Item;
}

const HOME = item({ id: "nd_home", kind: "folder", name: "home", nameDisplay: "home", parentId: "nd_root" });
const SHARED = item({ id: "nd_shared", kind: "folder", name: "shared", nameDisplay: "shared", parentId: "nd_root" });
const DOOMED = item({ id: "nd_doomed", kind: "folder", name: "cust-home", nameDisplay: "cust-home", etag: "7" });
const OTHER = item({ id: "nd_other", name: "keep.csv", nameDisplay: "keep.csv" });

interface Seen {
  method: string;
  url: string;
  headers: Record<string, string>;
}

interface Script {
  /** Resolve to release the drive read; until then it hangs. */
  releaseDrive: () => void;
  /** Every request the page made, in order. */
  seen: Seen[];
  /** The status the DELETE answers with. */
  trashStatus: number;
}

function headersOf(init?: RequestInit): Record<string, string> {
  const out: Record<string, string> = {};
  const raw = init?.headers;
  if (!raw) return out;
  // A `Headers` from the fetch runtime is not always the jsdom global, so it is
  // recognised by shape rather than by `instanceof`.
  if (typeof (raw as Headers).forEach === "function" && !Array.isArray(raw)) {
    (raw as Headers).forEach((value, key) => {
      out[key.toLowerCase()] = value;
    });
  } else if (Array.isArray(raw)) {
    for (const [key, value] of raw) out[key.toLowerCase()] = value;
  } else {
    for (const [key, value] of Object.entries(raw)) out[key.toLowerCase()] = String(value);
  }
  return out;
}

function stubApi(options: { holdDrive?: boolean; children?: Item[] } = {}): Script {
  const seen: Seen[] = [];
  let trashed = false;
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const script: Script = {
    releaseDrive: () => release(),
    seen,
    trashStatus: 200,
  };
  const rows = options.children ?? [DOOMED, OTHER];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // openapi-fetch hands over a built `Request`, so the method and the
      // headers live on it rather than on `init`.
      const url = input instanceof Request ? input.url : String(input);
      const method = (input instanceof Request ? input.method : (init?.method ?? "GET")).toUpperCase();
      const headers = input instanceof Request ? headersOf({ headers: input.headers }) : headersOf(init);
      seen.push({ method, url, headers });
      const answer = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        if (options.holdDrive) await gate;
        // The drive names the caller's own home; the root's `home` child is the
        // org-wide container and is nobody's landing place.
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", homeId: "nd_home", quotaBytes: 0 });
      }
      if (method === "DELETE") {
        if (script.trashStatus !== 200) {
          return answer({ code: "files.folder_locked", message: "in use" }, script.trashStatus);
        }
        trashed = true;
        return answer({
          id: "op_trash",
          driveId: "dr_1",
          kind: "trash",
          state: "done",
          done: 1,
          total: 1,
          bytes: 0,
          skipped: 0,
          conflicts: [],
          errors: [],
          undoableUntil: "2026-10-10T00:00:00Z",
        });
      }
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/permissions")) return answer({ value: [], nextMarker: null });
      if (url.includes("/versions") || url.includes("/activity")) {
        return answer({ value: [], nextMarker: null });
      }
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (url.includes("/items/nd_root/children")) {
        return answer({ value: [HOME, SHARED], nextMarker: null });
      }
      if (url.includes("/children")) {
        return answer({
          value: trashed ? rows.filter((row) => row.id !== DOOMED.id) : rows,
          nextMarker: null,
        });
      }
      if (url.includes("/items/")) return answer(HOME);
      return answer({});
    }),
  );
  return script;
}

function stubViewport(): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        media: query,
        matches: query === FILES_NARROW_QUERY ? false : false,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
}

function mount(at = "/files/nd_home") {
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path="/files" element={<FilesScreen platform="mac" />} />
          <Route path="/files/trash" element={<FilesScreen trash platform="mac" />} />
          <Route path="/files/:nodeId" element={<FilesScreen platform="mac" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const browserMain = () => screen.getByRole("region", { name: "Files" });
const placeButton = (name: string) =>
  within(screen.getByRole("navigation", { name: "Places" })).getByRole("button", { name: new RegExp(`^${name}$`) });
const rowNamed = (name: string) =>
  within(screen.getByRole("treegrid")).getByRole("row", { name: new RegExp(name) });

beforeEach(() => stubViewport());
afterEach(() => vi.unstubAllGlobals());

describe("the drive resolving after the listing's first paint (F-500)", () => {
  it("waits for the drive instead of painting an empty listing, then converges", async () => {
    const script = stubApi({ holdDrive: true });
    mount();

    // Before the drive lands nothing is known: the page says so rather than
    // counting nothing as a listing.
    await screen.findByText("Opening your files…");
    expect(browserMain().textContent).not.toMatch(/0 shown/);
    expect(screen.queryByRole("treegrid")).toBeNull();
    // Nor a trail — an empty trail over a folder that holds rows is the same lie
    // as "0 shown", so the whole browser waits rather than painting a husk.
    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).toBeNull();
    expect(placeButton("Home")).toBeDisabled();

    script.releaseDrive();

    // Everything past here converges on its own commit: the rows arrive with the
    // listing, the trail and the rail a beat later as their effects settle. Each
    // claim therefore waits for the state it is about rather than riding the
    // previous wait's timing.
    await waitFor(() => expect(rowNamed("cust-home")).toBeInTheDocument());
    await waitFor(() => expect(browserMain().textContent).toMatch(/2 of 2 shown/));
    // The trail spells the drive's root the way the rail does: the stored name is
    // `home`, the product's own name for it is `Home`, and one place has one spelling.
    await waitFor(() =>
      expect(within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByText("Home")).toBeInTheDocument(),
    );
    await waitFor(() => expect(placeButton("Home")).toBeEnabled());
    // Shared is off the rail for now; Home is the one node-addressed place left.
    // Asserted only once the converged page is on screen, so the absence is the
    // product's answer and not a frame that has yet to be drawn.
    expect(screen.queryByRole("button", { name: /^Shared$/ })).toBeNull();
  });

  it("applies a persisted filter set and view before the drive exists and still converges", async () => {
    const script = stubApi({ holdDrive: true });
    window.localStorage.setItem("alkera.files.view.dr_1", "grid");
    mount("/files/nd_home?chips=kind%3Afiles%2Cowner%3Ame%2Csize%3Asmall");

    await screen.findByText("Opening your files…");
    expect(browserMain().textContent).not.toMatch(/0 shown/);

    script.releaseDrive();

    await waitFor(() => expect(rowNamed("cust-home")).toBeInTheDocument());
    // The stored view is keyed by the drive, so the browser can only read it once
    // the drive exists — it is applied from an effect, a commit behind the rows.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Grid" })).toHaveAttribute("aria-pressed", "true"),
    );
    const listing = script.seen.find((request) => request.url.includes("/items/nd_home/children"));
    expect(listing?.url).toContain("dr_1");
    expect(listing?.url).not.toContain("undefined");
    window.localStorage.removeItem("alkera.files.view.dr_1");
  });

});

describe("Move to trash from the context menu (F-499)", () => {
  it("a right-click selects the row under the pointer and the menu's trash issues the DELETE the keyboard would", async () => {
    const script = stubApi();
    mount();
    const row = await waitFor(() => rowNamed("cust-home"));

    fireEvent.contextMenu(within(row).getAllByRole("gridcell")[0] as HTMLElement);

    await waitFor(() => expect(row).toHaveAttribute("aria-selected", "true"));

    const menu = await screen.findByRole("menu");
    const trash = within(menu).getByRole("menuitem", { name: /^Move to trash/ });
    expect(trash).not.toHaveAttribute("aria-disabled", "true");
    fireEvent.click(trash);

    await waitFor(() => {
      const sent = script.seen.find((request) => request.method === "DELETE");
      expect(sent).toBeDefined();
      expect(sent?.url).toContain("/drives/dr_1/items/nd_doomed");
      expect(sent?.url).toContain("permanent=false");
      expect(sent?.headers["if-match"]).toBe("7");
      expect(sent?.headers["idempotency-key"]).toBeTruthy();
    });
    // One converged listing: the trashed row is gone from it and the untouched
    // row is still in it — asserted together so neither claim reads a listing the
    // other has not seen.
    await waitFor(() => {
      expect(screen.queryByRole("row", { name: /cust-home/ })).toBeNull();
      expect(screen.getByRole("row", { name: /keep\.csv/ })).toBeInTheDocument();
    });
  });

  it("puts the row back when the server refuses the trash with a 409", async () => {
    const script = stubApi();
    script.trashStatus = 409;
    mount();
    const row = await waitFor(() => rowNamed("cust-home"));

    fireEvent.contextMenu(within(row).getAllByRole("gridcell")[0] as HTMLElement);
    const menu = await screen.findByRole("menu");
    await act(async () => {
      fireEvent.click(within(menu).getByRole("menuitem", { name: /^Move to trash/ }));
    });

    await waitFor(() => expect(script.seen.some((request) => request.method === "DELETE")).toBe(true));
    // The refused row is back in the same listing the untouched row is in.
    await waitFor(() => {
      expect(rowNamed("cust-home")).toBeInTheDocument();
      expect(rowNamed("keep.csv")).toBeInTheDocument();
    });
  });
});
