import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FILES_NARROW_QUERY, FilesScreen } from "@/pages/workspace/files/FilesPage";

// F-511 — Cmd+Z after a mis-move.
//
// A move small enough to run inline answers 200 with the changed node and names NO
// operation, so there is no id for `POST …/operations/{id}/undo` to invert. Undo for
// that step is the opposite move, issued by the browser and fenced on the version the
// answer carried — the version it was asked at is spent, because the move bumped it.
//
// Driven through the real assembled route over a scripted namespace that actually
// re-parents the node, so "the folder is back where it was" is asserted as the listing
// a person would look at, not only as a request.

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
const DEST = item({ id: "nd_dest", kind: "folder", name: "dest", nameDisplay: "dest", etag: "et_dest" });
const MOVER = item({ id: "nd_mover", kind: "folder", name: "mover", nameDisplay: "mover", etag: "et_mover_1" });
const ANCHOR = item({ id: "nd_anchor", name: "anchor.csv", nameDisplay: "anchor.csv", parentId: "nd_dest" });

interface Seen {
  method: string;
  url: string;
  ifMatch: string | undefined;
  parentId: string | undefined;
}

interface Script {
  /** Every request the page made, in order. */
  seen: Seen[];
  /** Only the moves, in order — what an undo is judged by. */
  moves: () => Seen[];
  /** Answer the next PATCH with this status instead of 200. */
  moveStatus: number;
}

function stubApi(): Script {
  // The one piece of server state this exercise turns on: where each node lives,
  // and at which version. An undo fenced on a stale version must fail here.
  const parents = new Map<string, string>([
    ["nd_home", "nd_root"],
    ["nd_dest", "nd_home"],
    ["nd_mover", "nd_home"],
    ["nd_anchor", "nd_dest"],
  ]);
  const etags = new Map<string, string>([
    ["nd_home", "et_home"],
    ["nd_dest", "et_dest"],
    ["nd_mover", "et_mover_1"],
    ["nd_anchor", "et_1"],
  ]);
  const byId = new Map<string, Item>([
    ["nd_home", HOME],
    ["nd_dest", DEST],
    ["nd_mover", MOVER],
    ["nd_anchor", ANCHOR],
  ]);
  let version = 1;
  const seen: Seen[] = [];
  const script: Script = {
    seen,
    moves: () => seen.filter((request) => request.method === "PATCH"),
    moveStatus: 200,
  };

  const node = (id: string): Item =>
    item({ ...byId.get(id), parentId: parents.get(id) ?? null, etag: etags.get(id) ?? "" });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // openapi-fetch hands over a built `Request`, so the method, the body and
      // the headers live on it rather than on `init`.
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = request.url;
      const method = request.method.toUpperCase();
      const body = method === "PATCH" ? ((await request.clone().json()) as { parentId?: string }) : undefined;
      seen.push({
        method,
        url,
        ifMatch: request.headers.get("if-match") ?? undefined,
        parentId: body?.parentId,
      });
      const answer = (payload: unknown, status = 200) =>
        new Response(JSON.stringify(payload), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (method === "PATCH") {
        const id = /\/items\/([^/?]+)/.exec(url)?.[1] ?? "";
        if (script.moveStatus !== 200) {
          return answer(
            { code: "files.precondition_failed", message: "version moved on" },
            script.moveStatus,
          );
        }
        if (etags.get(id) !== request.headers.get("if-match")) {
          return answer({ code: "files.precondition_failed", message: "version moved on" }, 412);
        }
        if (body?.parentId !== undefined) parents.set(id, body.parentId);
        version += 1;
        etags.set(id, `et_mover_${version}`);
        return answer(node(id));
      }
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/permissions")) return answer({ value: [], nextMarker: null });
      if (url.includes("/versions") || url.includes("/activity")) {
        return answer({ value: [], nextMarker: null });
      }
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      const children = /\/items\/([^/]+)\/children/.exec(url);
      if (children) {
        const parent = children[1];
        const value = [...parents.entries()]
          .filter(([, held]) => held === parent)
          .map(([id]) => node(id));
        return answer({ value, nextMarker: null });
      }
      const one = /\/items\/([^/?]+)(\?|$)/.exec(url);
      if (one) return answer(node(one[1]));
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
          <Route path="/files/:nodeId" element={<FilesScreen platform="mac" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const rowNamed = (name: string) =>
  within(screen.getByRole("treegrid")).getByRole("row", { name: new RegExp(name) });
const noRowNamed = (name: string) =>
  within(screen.getByRole("treegrid")).queryByRole("row", { name: new RegExp(name) });

/** Open the menu over a row and pick one of its commands. */
async function command(rowName: string, label: RegExp): Promise<void> {
  const row = await waitFor(() => rowNamed(rowName));
  fireEvent.contextMenu(within(row).getAllByRole("gridcell")[0] as HTMLElement);
  const menu = await screen.findByRole("menu");
  const entry = within(menu).getByRole("menuitem", { name: label });
  expect(entry).not.toHaveAttribute("aria-disabled", "true");
  fireEvent.click(entry);
}

/** Cmd+Z / Cmd+Shift+Z as the page hears them.
 *
 *  Dispatched at the window, which is where `useUndoStack` binds them — that is
 *  the whole point of the binding living on the page rather than on the toast. */
function accel(shift = false): void {
  fireEvent.keyDown(window, { key: "z", metaKey: true, shiftKey: shift });
}

/** Walk into the folder the way a person does. */
async function openFolder(name: string): Promise<void> {
  const row = await waitFor(() => rowNamed(name));
  // Opening lives on the row's first cell, which is the one carrying the row semantics.
  fireEvent.doubleClick(within(row).getAllByRole("gridcell")[0] as HTMLElement);
}

beforeEach(() => stubViewport());
afterEach(() => vi.unstubAllGlobals());

describe("undo after a move the server ran inline (F-511)", () => {
  it("cut, walk, paste, then Cmd+Z puts the folder back under the folder it came from", async () => {
    const script = stubApi();
    mount();

    await command("mover", /^Cut/);
    await openFolder("dest");
    await waitFor(() => expect(rowNamed("anchor.csv")).toBeInTheDocument());
    // The cut row is not listed here, so the paste has nothing to read it off:
    // everything the undo needs must have ridden the report.
    expect(noRowNamed("mover")).toBeNull();

    await command("anchor.csv", /^Paste/);
    await waitFor(() => expect(rowNamed("mover")).toBeInTheDocument());
    expect(script.moves()).toHaveLength(1);
    expect(script.moves()[0]).toMatchObject({ parentId: "nd_dest", ifMatch: "et_mover_1" });

    accel();

    // One inverse, fenced on the version the MOVE produced — the version it was
    // asked at is spent, so re-sending it would be a guaranteed 412.
    await waitFor(() => expect(script.moves()).toHaveLength(2));
    expect(script.moves()[1]).toMatchObject({ parentId: "nd_home", ifMatch: "et_mover_2" });
    expect(script.moves()[1].url).toContain("/items/nd_mover");
    // And the listing agrees: it is gone from the destination.
    await waitFor(() => expect(noRowNamed("mover")).toBeNull());
  });

  it("Move to… then Cmd+Z puts it back, and Cmd+Shift+Z sends it again", async () => {
    const script = stubApi();
    mount();

    await command("mover", /^Move to…/);
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });
    // One click picks the folder row in the picker's treegrid; Move here lands in it.
    await userEvent.click(await within(dialog).findByText("dest"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Move here" }));

    await waitFor(() => expect(script.moves()).toHaveLength(1));
    expect(script.moves()[0]).toMatchObject({ parentId: "nd_dest", ifMatch: "et_mover_1" });
    await waitFor(() => expect(noRowNamed("mover")).toBeNull());

    accel();
    await waitFor(() => expect(script.moves()).toHaveLength(2));
    expect(script.moves()[1]).toMatchObject({ parentId: "nd_home", ifMatch: "et_mover_2" });
    // Back in the folder it came from, which is the one on screen.
    await waitFor(() => expect(rowNamed("mover")).toBeInTheDocument());

    // The redo inverts the inverse, fenced on what the inverse produced.
    accel(true);
    await waitFor(() => expect(script.moves()).toHaveLength(3));
    expect(script.moves()[2]).toMatchObject({ parentId: "nd_dest", ifMatch: "et_mover_3" });
    await waitFor(() => expect(noRowNamed("mover")).toBeNull());
  });

  it("a refused undo says why and leaves the step on the stack", async () => {
    const script = stubApi();
    mount();

    await command("mover", /^Move to…/);
    const dialog = await screen.findByRole("dialog", { name: /^Move 1 item to/ });
    await userEvent.click(await within(dialog).findByText("dest"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Move here" }));
    await waitFor(() => expect(script.moves()).toHaveLength(1));

    const toast = await screen.findByRole("status");
    // The toast names where the node LANDED, read off the answer: the server
    // decides the destination, so echoing the folder that was picked would be a
    // guess wherever the two differ.
    expect(within(toast).getByText("Moved mover to dest")).toBeInTheDocument();

    script.moveStatus = 412;
    await userEvent.click(within(toast).getByRole("button", { name: "Undo" }));

    expect(
      await within(toast).findByText("Someone changed this while you were working."),
    ).toBeInTheDocument();

    // The step is still there: with the world settled, the same Cmd+Z works.
    script.moveStatus = 200;
    accel();
    await waitFor(() => expect(script.moves()).toHaveLength(3));
    expect(script.moves()[2]).toMatchObject({ parentId: "nd_home", ifMatch: "et_mover_2" });
  });
});
