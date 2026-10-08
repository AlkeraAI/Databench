import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

// Undo after a rename.
//
// `PATCH …/items/{id}` queues an operation for exactly one thing — a move too large to
// run inline — so a rename is ALWAYS answered with the node and never names an
// operation. The page's only path to the undo stack ran off that operation, so a
// rename left the stack empty: no toast, and Cmd+Z did nothing, while a trash beside
// it offered both. The inverse is the old name, put back, fenced on the version the
// rename produced.
//
// Driven through the real assembled route over a namespace that actually renames the
// node, so "the name is back" is asserted as the listing a person reads — and the
// fencing is asserted as the `If-Match` the inverse carried, because the version the
// rename was ASKED at is spent.

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    name: "notes.txt",
    nameDisplay: "notes.txt",
    nameEncoding: "utf-8",
    pathBytes: "/home/dana/notes.txt",
    path: "/home/dana/notes.txt",
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

const HOME = item({
  id: "nd_home",
  kind: "folder",
  name: "home",
  nameDisplay: "home",
  parentId: "nd_root",
  etag: "et_home",
});
const NOTES = item({ id: "nd_notes" });

interface Seen {
  method: string;
  ifMatch: string | undefined;
  name: string | undefined;
}

interface Script {
  /** Only the renames, in order — what an undo is judged by. */
  renames: () => Seen[];
}

function stubApi(): Script {
  // The one piece of server state this turns on: what each node is called, and at
  // which version. A rename fenced on a spent version must fail here.
  const names = new Map<string, string>([
    ["nd_home", "home"],
    ["nd_notes", "notes.txt"],
  ]);
  const etags = new Map<string, string>([
    ["nd_home", "et_home"],
    ["nd_notes", "et_1"],
  ]);
  let version = 1;
  const seen: Seen[] = [];

  const node = (id: string): Item =>
    item({
      ...(id === "nd_home" ? HOME : NOTES),
      name: names.get(id) ?? "",
      nameDisplay: names.get(id) ?? "",
      etag: etags.get(id) ?? "",
    });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = request.url;
      const method = request.method.toUpperCase();
      const body =
        method === "PATCH" ? ((await request.clone().json()) as { name?: string }) : undefined;
      if (method === "PATCH") {
        seen.push({
          method,
          ifMatch: request.headers.get("if-match") ?? undefined,
          name: body?.name,
        });
      }
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
        // The etag rides inside the write: a caller holding a spent one changes
        // nothing, which is the whole reason the inverse must carry the NEW one.
        if (etags.get(id) !== request.headers.get("if-match")) {
          return answer({ code: "files.precondition_failed", message: "version moved on" }, 412);
        }
        if (body?.name !== undefined) names.set(id, body.name);
        version += 1;
        etags.set(id, `et_${version}`);
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
        return answer({
          value: children[1] === "nd_home" ? [node("nd_notes")] : [],
          nextMarker: null,
        });
      }
      const one = /\/items\/([^/?]+)(\?|$)/.exec(url);
      if (one) return answer(node(one[1]));
      return answer({});
    }),
  );
  return { renames: () => seen.filter((request) => request.name !== undefined) };
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
          <Route path="/files/:nodeId" element={<FilesScreen platform="mac" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const rowNamed = (name: string) =>
  within(screen.getByRole("treegrid")).getByRole("row", { name: new RegExp(name) });

/** Rename the row the way a person does: the row's own menu, then the editor. */
async function renameRow(from: string, to: string): Promise<void> {
  const row = await waitFor(() => rowNamed(from));
  fireEvent.contextMenu(within(row).getAllByRole("gridcell")[0] as HTMLElement);
  const menu = await screen.findByRole("menu");
  fireEvent.click(within(menu).getByRole("menuitem", { name: /^Rename/ }));
  const field = await screen.findByRole("textbox", { name: "New name" });
  fireEvent.change(field, { target: { value: to } });
  fireEvent.keyDown(field, { key: "Enter" });
}

/** Cmd+Z as the page hears it — bound to the window, not to the toast. */
function accel(shift = false): void {
  fireEvent.keyDown(window, { key: "z", metaKey: true, shiftKey: shift });
}

beforeEach(() => stubViewport());
afterEach(() => vi.unstubAllGlobals());

describe("undo after a rename", () => {
  it("offers the toast a trash gets, and its Undo puts the old name back", async () => {
    const script = stubApi();
    mount();

    await renameRow("notes.txt", "minutes.txt");
    await waitFor(() => expect(rowNamed("minutes.txt")).toBeInTheDocument());

    // The prompt a rename never had. It names the file that was renamed, so the
    // reader can tell which of several renames they are being offered back.
    await screen.findByText("Renamed notes.txt");
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));

    await waitFor(() => expect(rowNamed("notes.txt")).toBeInTheDocument());
    const [wrote, undid] = script.renames();
    expect(wrote?.name).toBe("minutes.txt");
    expect(wrote?.ifMatch).toBe("et_1");
    // The inverse asks for the old name, fenced on the version the rename produced —
    // the one it was asked at is spent, and re-using it is a guaranteed 412.
    expect(undid?.name).toBe("notes.txt");
    expect(undid?.ifMatch).toBe("et_2");
  });

  it("Cmd+Z reverts a rename after the toast is gone, and Cmd+Shift+Z redoes it", async () => {
    const script = stubApi();
    mount();

    await renameRow("notes.txt", "minutes.txt");
    await waitFor(() => expect(rowNamed("minutes.txt")).toBeInTheDocument());

    // Dismissing the prompt is not giving up the step: the history lives on the page.
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();

    accel();
    await waitFor(() => expect(rowNamed("notes.txt")).toBeInTheDocument());

    // The inverse of the inverse is the original, fenced on the version the undo
    // produced — so redo is a real round trip, not a replay of the first request.
    accel(true);
    await waitFor(() => expect(rowNamed("minutes.txt")).toBeInTheDocument());

    expect(script.renames().map((request) => request.name)).toEqual([
      "minutes.txt",
      "notes.txt",
      "minutes.txt",
    ]);
    expect(script.renames().map((request) => request.ifMatch)).toEqual(["et_1", "et_2", "et_3"]);
  });

  it("does not reach past the rename: Cmd+Z with nothing renamed writes nothing", async () => {
    const script = stubApi();
    mount();

    await waitFor(() => expect(rowNamed("notes.txt")).toBeInTheDocument());
    accel();

    await waitFor(() => expect(rowNamed("notes.txt")).toBeInTheDocument());
    expect(script.renames()).toHaveLength(0);
  });
});
