import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import { resetRealtimeStatus, useRealtimeStatus } from "@/api/events/status";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FILES_NARROW_QUERY, FilesScreen } from "@/pages/workspace/files/FilesPage";
import { SAVED_COPY_LINE } from "@/pages/workspace/files/liveRoot/liveCopy";
import { filesLiveFact } from "@/tests/fixtures/statusFacts";

// The page as the route actually renders it: `FilesScreen` over a stubbed network, with
// nothing hand-mounted. Each assertion names a surface AND the slot it has to appear in,
// because the failure this test exists to catch is a surface that is written, exported and
// unit-tested but never reaches the frame — which no per-surface test can see.

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

const HOME = item({ id: "nd_home", kind: "folder", name: "home", nameDisplay: "home" });
const SHARED = item({ id: "nd_shared", kind: "folder", name: "shared", nameDisplay: "shared" });
const LEASED = item({
  id: "nd_2",
  name: "model.ckpt",
  nameDisplay: "model.ckpt",
  lease: { holder: "dana", holderDisplay: "Dana", machine: "gpu-1", acquiredAt: "2026-09-08T10:00:00Z" },
} as unknown as Partial<Item>);

/** A row the machine holding the folder is writing right now. */
const WRITING = item({
  id: "nd_3",
  name: "out.log",
  nameDisplay: "out.log",
  live: { state: "writing" },
} as unknown as Partial<Item>);

/** A row a person just uploaded into the folder, on its way to the machine. */
const INBOUND = item({
  id: "nd_4",
  name: "from-laptop.csv",
  nameDisplay: "from-laptop.csv",
  live: { state: "inbound" },
} as unknown as Partial<Item>);

interface Stub {
  urls: string[];
}

/** The folder on screen, held by a machine that is writing into it. */
const MOUNTED = item({
  id: "nd_home",
  kind: "folder",
  name: "home",
  nameDisplay: "home",
  lease: {
    holder: "Dana",
    machine: "gpu-1",
    purpose: "chat",
    live: true,
    status: filesLiveFact("live"),
    pending: 0,
    since: "2026-09-16T10:00:00Z",
    last_sync_at: "2026-09-16T10:05:00Z",
    expires_at: "2100-01-01T00:00:00Z",
  },
} as unknown as Partial<Item>);

/** One fetch stub for every read the assembled page makes on mount. */
function stubApi(
  options: { children?: Item[]; trashEntries?: unknown[]; folder?: Item } = {},
): Stub {
  const urls: string[] = [];
  const children = options.children ?? [item(), LEASED, WRITING, INBOUND];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // The search read is a raw `fetch` with a relative url, so the stub must not try to
      // build a `Request` out of it — jsdom has no base to resolve one against.
      void init;
      const url = input instanceof Request ? input.url : String(input);
      urls.push(url);
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });

      if (url.includes("/leases")) {
        return answer([
          { nodeId: "nd_2", epoch: 1, machine: "gpu-1", lastSyncAt: "2026-09-08T10:00:00Z" },
        ]);
      }
      if (url.includes("/permissions")) {
        return answer({ value: [], nextMarker: null });
      }
      if (url.includes("/search")) {
        return answer({ value: [item({ id: "nd_hit", nameDisplay: "budget.csv" })], nextMarker: null });
      }
      if (url.includes("/trash")) {
        return answer({ entries: options.trashEntries ?? [], nextMarker: null });
      }
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) {
        return answer({ value: [HOME, SHARED], nextMarker: null });
      }
      if (url.includes("/children")) {
        return answer({ value: children, nextMarker: null });
      }
      if (url.includes("/items/")) {
        return answer(options.folder ?? item({ id: "nd_home", kind: "folder", nameDisplay: "home" }));
      }
      return answer({});
    }),
  );
  return { urls };
}

/** A wide viewport, so the details pane is the third column rather than a sheet. */
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
          <Route path="/files" element={<FilesScreen />} />
          <Route path="/files/trash" element={<FilesScreen trash />} />
          <Route path="/files/:nodeId" element={<FilesScreen />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  stubViewport();
  resetFrameBus();
  // The stream is delivering, as it is for a signed-in reader: with it down the
  // page is required to stop claiming to be live, which is its own case.
  resetRealtimeStatus();
  useRealtimeStatus.getState().setSse("connected");
});
afterEach(() => {
  vi.unstubAllGlobals();
  resetFrameBus();
  resetRealtimeStatus();
});

/** A save the machine holding the folder just made, as the stream delivers it. */
const nodeFrame = (parentId: string): RealtimeEventFrame => ({
  type: "file_node.changed",
  entity: "file_node",
  entity_id: "nd_3",
  version: 2,
  org_id: "or_1",
  drive_id: "dr_1",
  parent_id: parentId,
});

describe("the assembled Files page", () => {
  it("puts the search field in the toolbar slot, with nothing beside it", async () => {
    stubApi();
    mount();

    const toolbar = document.querySelector(".alk-files__toolbar");
    expect(toolbar).not.toBeNull();
    // The search field is inside the head's toolbar, not merely somewhere on the page: a
    // surface rendered into the wrong slot is the bug.
    await waitFor(() => {
      expect(within(toolbar as HTMLElement).getByRole("searchbox")).toBeInTheDocument();
    });
    // The filter chips are retired from the toolbar, and so is the Search button that
    // outlived them: the field IS the search bar, and nothing beside it claims to run one.
    expect(within(toolbar as HTMLElement).queryAllByRole("button")).toEqual([]);
    expect(within(toolbar as HTMLElement).queryByRole("group", { name: "Filters" })).toBeNull();
  });

  it("renders the browser and its threshold footer in the browser slot", async () => {
    stubApi();
    mount();

    const browser = screen.getByRole("region", { name: "Files" });
    await waitFor(() => {
      expect(within(browser).getByText("report.csv")).toBeInTheDocument();
    });
    // The footer is the soft-threshold count, fed by the real hook over the stubbed page.
    expect(within(browser).getByText(/shown$/)).toBeInTheDocument();
  });

  it("shows the details pane in the pane slot", async () => {
    stubApi();
    mount();

    // The shell's column and the pane's own region are both "Details": the outer one is
    // the slot, and the assembly is only right if the pane is rendered *inside* it.
    const panes = await screen.findAllByRole("complementary", { name: "Details" });
    const column = panes.find((node) => node.classList.contains("alk-files__pane"));
    expect(column).toBeDefined();
    expect((column as HTMLElement).querySelector(".alk-files__pane-slot")).not.toBeNull();
    expect(panes.some((node) => node !== column && column?.contains(node))).toBe(true);
  });

  it("opens the page's row menu from the pane's own … button", async () => {
    // The pane is built from the wired actions, not beside them. Built outside
    // them it has no menu to open, and the button — which is the only opener a
    // touch screen has for the row a person is reading — never renders at all.
    stubApi();
    mount();

    const browser = screen.getByRole("region", { name: "Files" });
    await waitFor(() => expect(within(browser).getByText("report.csv")).toBeInTheDocument());
    await userEvent.click(within(browser).getByText("report.csv"));

    const slot = document.querySelector(".alk-files__pane-slot") as HTMLElement;
    const opener = await within(slot).findByRole("button", { name: "Actions for report.csv" });
    await userEvent.click(opener);

    const menu = await screen.findByRole("menu");
    // The row's own menu, not some other list: it offers the actions for the
    // file the pane is showing.
    expect(within(menu).getByRole("menuitem", { name: /^Share…/ })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: /^Rename/ })).toBeInTheDocument();
  });

  it("replaces the listing with search results once the query is long enough", async () => {
    stubApi();
    const user = userEvent.setup();
    mount();

    const field = await screen.findByRole("searchbox");
    await user.type(field, "budget");

    const browser = screen.getByRole("region", { name: "Files" });
    await waitFor(
      () => {
        expect(within(browser).getByText("budget.csv")).toBeInTheDocument();
      },
      { timeout: 4_000 },
    );
    // The listing is gone while a search answers — the results are not stacked under it.
    expect(within(browser).queryByText("report.csv")).not.toBeInTheDocument();
  });

  it("mounts the upload tray inside the browser's drop zone", async () => {
    stubApi();
    mount();

    const browser = screen.getByRole("region", { name: "Files" });
    await waitFor(() => {
      expect(browser.querySelector(".alk-files__drop")).not.toBeNull();
    });
    expect(browser.querySelector(".alk-files-tray")).not.toBeNull();
  });

  it("chips the row the machine is writing, and leaves the inherited facet unsaid", async () => {
    stubApi();
    mount();

    const browser = screen.getByRole("region", { name: "Files" });
    await waitFor(() => {
      expect(within(browser).getByText("model.ckpt")).toBeInTheDocument();
    });
    // What a row is doing on the machine is the row's own business, and it is
    // said on the row.
    expect(within(browser).getByText("writing…")).toBeInTheDocument();
    // A file the reader themselves put there, still on its way to it, says
    // nothing: it settles on its own.
    expect(within(browser).queryByText("Sending to the workspace…")).toBeNull();
    // Who holds the folder is the folder's, and it is said once above the rows
    // — not on every row that merely inherits the facet from the mount.
    expect(within(browser).queryByText(/In use by/)).toBeNull();
  });

  it("says what a leased folder's listing is showing, and says nothing over a plain one", async () => {
    const { unmount } = ((): { unmount: () => void } => {
      stubApi({ folder: MOUNTED });
      return mount();
    })();

    // The one line that answers "am I looking at what the machine has now?" —
    // rendered above the rows, from the folder's own lease.
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Live. Dana is working on gpu-1"),
    );
    unmount();
    vi.unstubAllGlobals();

    stubViewport();
    stubApi();
    mount();
    await screen.findByText("model.ckpt");
    // A folder nobody holds is not "last saved" — it just is.
    expect(screen.queryByText(/^Live\./)).toBeNull();
    expect(screen.queryByText(/^Last saved/)).toBeNull();
  });

  it("stops claiming live when the stream is down, whatever the lease says", async () => {
    useRealtimeStatus.getState().setSse("down");
    stubApi({ folder: MOUNTED });
    mount();

    // The browser cannot learn about a save with no stream, so it must not
    // promise one — even though the lease itself still says live.
    expect(
      await screen.findByText(SAVED_COPY_LINE),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^Live\./)).toBeNull();
  });

  it("re-reads the leased folder when the stream says something in it changed", async () => {
    const stub = stubApi({ folder: MOUNTED });
    mount();
    await screen.findByText("model.ckpt");

    const childrenReads = () =>
      stub.urls.filter((url) => url.includes("/items/nd_home/children")).length;
    const before = childrenReads();
    // A save inside the folder on screen: the page asked to be told about this
    // folder, so the listing is re-read without anybody clicking refresh.
    publishFrame(nodeFrame("nd_home"));
    await waitFor(() => expect(childrenReads()).toBeGreaterThan(before));

    // A save in a folder nobody has open costs nothing.
    const settled = childrenReads();
    publishFrame(nodeFrame("nd_elsewhere"));
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(childrenReads()).toBe(settled);
  });

  it("opens My leases from the rail, listing the folders I hold", async () => {
    const user = userEvent.setup();
    stubApi();
    mount();

    const rail = await screen.findByRole("navigation", { name: "Places" });
    const place = await within(rail).findByRole("button", { name: "My leases" });
    await waitFor(() => expect(place).toBeEnabled());
    await user.click(place);

    const mine = await screen.findByRole("list", { name: "My leases" });
    expect(within(mine).getByText("gpu-1")).toBeInTheDocument();
    // The listing it replaced is gone, not stacked under it.
    expect(screen.queryByText("model.ckpt")).toBeNull();
  });

  it("opens the context menu from the keyboard on the browser it is mounted on", async () => {
    stubApi();
    const user = userEvent.setup();
    mount();

    const drop = screen.getByRole("region", { name: "Files" }).querySelector(".alk-files__drop");
    expect(drop).not.toBeNull();
    (drop as HTMLElement).focus();
    await user.keyboard("{Shift>}{F10}{/Shift}");

    // The menu is FilesActions', so seeing it proves the actions layer is wrapped
    // around the browser rather than merely exported.
    expect(await screen.findByRole("menu")).toBeInTheDocument();
  });

  it("renders the trash page on the /files/trash route", async () => {
    stubApi({ trashEntries: [] });
    mount("/files/trash");

    const trash = screen.getByRole("region", { name: "Trash" });
    await waitFor(() => {
      expect(trash.querySelector(".alk-files--trash")).not.toBeNull();
    });
    // The rail marks Trash current, so the page and the place agree.
    expect(screen.getByRole("link", { name: "Trash" })).toHaveAttribute("aria-current", "page");
  });

  it("Restore to… opens the folder picker, and Cancel restores nothing", async () => {
    // The button must reach a real folder picker, not one the page never supplied.
    const stub = stubApi({
      trashEntries: [
        {
          trashOpId: "top_1",
          originalParentId: "nd_home",
          originalPath: "/home",
          deletedAt: "2026-09-08T10:00:00Z",
          purgeAfter: "2026-10-08T10:00:00Z",
          timeLeftSeconds: 86_400,
          item: item({ id: "nd_gone", name: "old.csv", nameDisplay: "old.csv" }),
        },
      ],
    });
    mount("/files/trash");
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Restore old.csv to another folder" }),
    );
    const dialog = await screen.findByRole("dialog", { name: /^Restore to/ });
    expect(within(dialog).getByRole("button", { name: "Restore here" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: /^Restore to/ })).toBeNull());
    expect(stub.urls.some((url) => url.includes("/restore"))).toBe(false);
  });
});

// The page's verbs, end to end over the real hooks. What these prove is the
// wiring no per-surface test can: that a write's answer reaches the undo stack,
// and that Cmd+Z then posts the inverse against the id the SERVER named.
describe("the assembled page's verbs", () => {
  interface Wire {
    method: string;
    url: string;
    body: unknown;
  }

  /** The reads the page makes on mount, plus the write routes this block drives:
   *  a copy (always 202 with an operation), a rename the server ran as one, the
   *  trash (200 with its operation; 204 only for the purge) and the undo. */
  function stubWrites(wire: Wire[]): void {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        void init;
        const request = input instanceof Request ? input : null;
        const url = request ? request.url : String(input);
        const method = request?.method ?? "GET";
        wire.push({
          method,
          url,
          body:
            request && method !== "GET" && method !== "DELETE"
              ? await request
                  .clone()
                  .json()
                  .catch(() => null)
              : null,
        });
        const answer = (body: unknown, status = 200) =>
          new Response(JSON.stringify(body), {
            status,
            headers: { "content-type": "application/json" },
          });

        if (url.includes("/operations/") && url.endsWith("/undo")) {
          return answer({ id: "op_c2", driveId: "dr_1", kind: "undo", state: "running" }, 202);
        }
        if (url.includes("/copy")) {
          return answer(
            {
              id: "op_c1",
              driveId: "dr_1",
              kind: "copy",
              state: "running",
              undoable: false,
            },
            202,
          );
        }
        if (method === "DELETE") {
          // The purge is the only 204; the trash answers with the operation it
          // ran as, which is the handle the toast and Cmd+Z need.
          if (url.includes("permanent=true")) return new Response(null, { status: 204 });
          return answer(
            {
              id: "op_t1",
              driveId: "dr_1",
              kind: "trash",
              state: "done",
              undoable: true,
              undoableUntil: "2026-10-08T00:00:00Z",
            },
            200,
          );
        }
        if (method === "PATCH") {
          return answer(
            {
              id: "op_r1",
              driveId: "dr_1",
              kind: "rename",
              state: "done",
              undoable: true,
              undoableUntil: "2026-10-08T00:00:00Z",
            },
            202,
          );
        }
        if (url.includes("/leases")) return answer([]);
        // Selecting a row opens the pane, which reads the node's grants and its
        // versions; both are lists, so the catch-all `{}` would crash it.
        if (url.includes("/permissions")) return answer({ value: [] });
        if (url.includes("/versions")) return answer({ value: [] });
        if (url.includes("/search")) return answer({ value: [], nextMarker: null });
        if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
        if (/\/files\/drives\/?(\?|$)/.test(url)) {
          return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
        }
        if (url.includes("/items/nd_root/children")) {
          return answer({ value: [HOME, SHARED], nextMarker: null });
        }
        if (url.includes("/children")) return answer({ value: [item()], nextMarker: null });
        if (url.includes("/items/")) {
          return answer(item({ id: "nd_home", kind: "folder", nameDisplay: "home" }));
        }
        return answer({});
      }),
    );
  }

  it("a copy answers 202, and Cmd+Z offers nothing the server would refuse", async () => {
    const wire: Wire[] = [];
    stubWrites(wire);
    mount();

    const browser = screen.getByRole("region", { name: "Files" });
    await waitFor(() => expect(within(browser).getByText("report.csv")).toBeInTheDocument());
    await userEvent.click(within(browser).getByText("report.csv"));

    const drop = document.querySelector(".alk-files__drop") as HTMLElement;
    fireEvent.keyDown(drop, { key: "c", ctrlKey: true });
    fireEvent.keyDown(drop, { key: "v", ctrlKey: true });

    await waitFor(() =>
      expect(wire.find((call) => call.url.includes("/copy"))?.body).toMatchObject({
        parentId: "nd_home",
      }),
    );
    // No undo prompt: the toast is the undo surface, and a copy has no inverse
    // to offer. (What a copy owes the person instead is progress, which is the
    // operation tray's job, not this one's.)
    expect(screen.queryByRole("status")).toBeNull();

    // A copy records no inverse, and the answer says so, so the copy never
    // reaches the undo stack: Cmd+Z sends nothing rather than posting an undo
    // the server answers `files.not_undoable` to.
    fireEvent.keyDown(window, { key: "z", metaKey: true });
    await waitFor(() => expect(wire.some((call) => call.url.includes("/copy"))).toBe(true));
    expect(wire.some((call) => call.url.includes("/undo"))).toBe(false);
  });

  it("a trash answers with its operation and Cmd+Z restores through it", async () => {
    const wire: Wire[] = [];
    stubWrites(wire);
    mount();

    const browser = screen.getByRole("region", { name: "Files" });
    await waitFor(() => expect(within(browser).getByText("report.csv")).toBeInTheDocument());
    await userEvent.click(within(browser).getByText("report.csv"));

    const drop = document.querySelector(".alk-files__drop") as HTMLElement;
    fireEvent.keyDown(drop, { key: "Delete" });
    await waitFor(() => expect(wire.some((call) => call.method === "DELETE")).toBe(true));

    // The toast can only name a step the DELETE's own answer put on the stack,
    // so its presence is the proof the 200 body reached the undo seam at all.
    const toast = await screen.findByRole("status");
    expect(toast).toHaveTextContent("Moved report.csv to trash");

    // And the restore is the server inverting op_t1 — the id it minted — not the
    // browser replaying what it thinks it deleted.
    fireEvent.keyDown(window, { key: "z", metaKey: true });
    await waitFor(() =>
      expect(wire.some((call) => call.url.includes("/operations/op_t1/undo"))).toBe(true),
    );
    expect(wire.find((call) => call.url.includes("/undo"))?.method).toBe("POST");
  });

  it("a rename the server ran as an operation is undone by Cmd+Z", async () => {
    const wire: Wire[] = [];
    stubWrites(wire);
    mount();

    const browser = screen.getByRole("region", { name: "Files" });
    await waitFor(() => expect(within(browser).getByText("report.csv")).toBeInTheDocument());
    await userEvent.click(within(browser).getByText("report.csv"));

    const drop = document.querySelector(".alk-files__drop") as HTMLElement;
    fireEvent.keyDown(drop, { key: "F2" });
    const field = await screen.findByRole("textbox", { name: "New name" });
    await userEvent.clear(field);
    await userEvent.type(field, "budget.csv");
    fireEvent.keyDown(field, { key: "Enter" });

    await waitFor(() => expect(wire.some((call) => call.method === "PATCH")).toBe(true));
    expect(wire.find((call) => call.method === "PATCH")?.body).toMatchObject({
      name: "budget.csv",
    });

    // The seam this test exists for: the rename owns its own mutation, so its
    // answer reaches the page only if the component reports it. Without that,
    // Cmd+Z after a rename posted nothing at all.
    fireEvent.keyDown(window, { key: "z", metaKey: true });
    await waitFor(() =>
      expect(wire.some((call) => call.url.includes("/operations/op_r1/undo"))).toBe(true),
    );
  });

  it("the Upload files item opens a real picker rather than nothing", async () => {
    const wire: Wire[] = [];
    stubWrites(wire);
    mount();

    const browser = screen.getByRole("region", { name: "Files" });
    await waitFor(() => expect(within(browser).getByText("report.csv")).toBeInTheDocument());

    const picker = screen.getByLabelText("Upload files") as HTMLInputElement;
    const clicks: string[] = [];
    picker.addEventListener("click", () => clicks.push("files"));
    fireEvent.contextMenu(document.querySelector(".alk-files__drop") as HTMLElement, {
      clientX: 10,
      clientY: 10,
    });
    await screen.findByRole("menu");
    fireEvent.click(document.querySelector('[data-item="upload-files"]') as Element);

    // The menu item reaches the input the browser needs in order to show a file
    // dialog at all; before this wiring it reached no handler.
    expect(clicks).toEqual(["files"]);
    expect(picker.multiple).toBe(true);
  });

  it("a column-header click re-orders the very listing the footer pages", async () => {
    // The bug this pins: the grid owned its order privately while the soft-threshold
    // footer paged the URL's order, so the first header click left the pager behind
    // `Load more` fetching markers for a listing nobody could see any more.
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = input instanceof Request ? input.url : String(input);
        urls.push(url);
        const answer = (body: unknown) =>
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        if (url.includes("/leases")) return answer([]);
        if (url.includes("/search")) return answer({ value: [], nextMarker: null });
        if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
        if (/\/files\/drives\/?(\?|$)/.test(url)) {
          return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
        }
        if (url.includes("/items/nd_root/children")) {
          return answer({ value: [HOME, SHARED], nextMarker: null });
        }
        // Every folder page but the last hands back a marker, so the ceiling effect —
        // the pager `Load more` raises — keeps paging under whatever order is current.
        if (url.includes("/nd_home/children")) {
          const marker = new URL(url, "http://localhost").searchParams.get("marker");
          return answer({ value: [item()], nextMarker: marker ? null : "mk_1" });
        }
        if (url.includes("/items/")) {
          return answer(item({ id: "nd_home", kind: "folder", nameDisplay: "home" }));
        }
        return answer({});
      }),
    );
    const user = userEvent.setup();
    mount();

    const browser = screen.getByRole("region", { name: "Files" });
    await waitFor(() => expect(within(browser).getByText("report.csv")).toBeInTheDocument());

    const pagedOrders = () =>
      urls
        .filter((url) => url.includes("/nd_home/children") && url.includes("marker="))
        .map((url) => new URL(url, "http://localhost").searchParams.get("orderBy"));

    await waitFor(() => expect(pagedOrders()).toEqual(["name asc"]));

    await user.click(within(browser).getByRole("button", { name: "Sort by Size" }));

    // One order, one listing: the pager follows the clicked column. Before the lift it
    // never asked for a size-ordered marker at all.
    await waitFor(() => expect(pagedOrders()).toContain("size desc"));
  });
});

describe("a refused New folder", () => {
  it("says the drive is full and leaves the form open holding the name", async () => {
    const writes: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        void init;
        const request = input instanceof Request ? input : null;
        const url = request ? request.url : String(input);
        const method = request?.method ?? "GET";
        const answer = (body: unknown, status = 200) =>
          new Response(JSON.stringify(body), {
            status,
            headers: { "content-type": "application/json" },
          });
        if (method !== "GET") {
          writes.push(`${method} ${new URL(url, "http://x").pathname}`);
          // The one refusal a create hits and cannot recover from on its own.
          return answer({ code: "files.quota_bytes", message: "2.0 TB of 2.0 TB used." }, 507);
        }
        if (url.includes("/leases")) return answer([]);
        if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
        if (/\/files\/drives\/?(\?|$)/.test(url)) {
          return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
        }
        if (url.includes("/children")) return answer({ value: [item()], nextMarker: null });
        if (url.includes("/items/")) {
          return answer(item({ id: "nd_home", kind: "folder", nameDisplay: "home" }));
        }
        return answer({});
      }),
    );
    const user = userEvent.setup();
    mount();

    const browser = screen.getByRole("region", { name: "Files" });
    await waitFor(() => expect(within(browser).getByText("report.csv")).toBeInTheDocument());

    await user.click(within(browser).getByRole("button", { name: "New folder" }));
    const field = await screen.findByRole("textbox", { name: "Folder name" });
    await user.type(field, "drafts{Enter}");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Your organization is out of storage.");
    expect(alert.getAttribute("data-code")).toBe("files.quota_bytes");
    // The folder does not exist, so the form that would create it is still on
    // screen with the typed name in it.
    expect(screen.getByRole("textbox", { name: "Folder name" })).toHaveValue("drafts");
    expect(writes).toEqual(["POST /api/v1/files/drives/dr_1/items/nd_home/children"]);
  });
});
