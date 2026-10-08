import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

// The three gaps a run against the real stack found in the page itself: the
// shortcut table bound to a fixed platform, two elements answering to
// "Breadcrumb", and create/upload reachable only by right-click or keystroke.

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
    capabilities: { can_read: true, can_write: true, can_share: true, can_delete: true },
    ...over,
  } as Item;
}

const HOME = item({ id: "nd_home", kind: "folder", name: "home", nameDisplay: "home" });

const SHARED = item({ id: "nd_shared", kind: "folder", name: "shared", nameDisplay: "shared" });

/** One write the page sent, with the precondition it carried. */
interface Wire {
  method: string;
  url: string;
  ifMatch: string | null;
  body: unknown;
}

interface StubOptions {
  /** The listing of one folder, by node id; any other folder lists `report.csv`. */
  childrenOf?: Readonly<Record<string, readonly Item[]>>;
  /** Every non-GET request the page sends, in order. */
  wire?: Wire[];
}

function stubApi(options: StubOptions = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      void init;
      const request = input instanceof Request ? input : null;
      const url = request ? request.url : String(input);
      const method = request?.method ?? "GET";
      if (request && method !== "GET") {
        options.wire?.push({
          method,
          url,
          ifMatch: request.headers.get("If-Match"),
          body: await request
            .clone()
            .json()
            .catch(() => null),
        });
      }
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/permissions")) return answer({ value: [] });
      if (url.includes("/versions")) return answer({ value: [] });
      for (const [folderId, rows] of Object.entries(options.childrenOf ?? {})) {
        if (url.includes(`/items/${folderId}/children`)) {
          return answer({ value: rows, nextMarker: null });
        }
      }
      if (url.includes("/search")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) {
        return answer({ value: [HOME, SHARED], nextMarker: null });
      }
      if (url.includes("/children")) return answer({ value: [item()], nextMarker: null });
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

/** Make this browser look like the one under test. `navigator.platform` is
 *  read-only, so it is redefined rather than assigned. */
function stubBrowserPlatform(platform: string, userAgent: string): void {
  Object.defineProperty(window.navigator, "platform", {
    value: platform,
    configurable: true,
  });
  Object.defineProperty(window.navigator, "userAgent", {
    value: userAgent,
    configurable: true,
  });
}

/** What a browser will stop on with Tab: the focusable elements, minus anything
 *  taken out of the order with a negative tabindex or disabled. */
const TABBABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

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

async function openContextMenu(): Promise<HTMLElement> {
  const surface = document.querySelector(".alk-files__drop") as HTMLElement;
  await userEvent.pointer({ target: surface, keys: "[MouseRight]" });
  return await screen.findByRole("menu");
}

const originalPlatform = Object.getOwnPropertyDescriptor(window.navigator, "platform");
const originalUserAgent = Object.getOwnPropertyDescriptor(window.navigator, "userAgent");

beforeEach(() => {
  stubViewport();
  stubApi();
});

afterEach(() => {
  vi.unstubAllGlobals();
  if (originalPlatform) Object.defineProperty(window.navigator, "platform", originalPlatform);
  if (originalUserAgent) Object.defineProperty(window.navigator, "userAgent", originalUserAgent);
});

describe("the route resolves the platform the shortcuts are read on", () => {
  it("reads Cmd on a Mac, with no platform prop passed", async () => {
    stubBrowserPlatform("MacIntel", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)");
    mount();
    await screen.findByText("report.csv");

    const menu = await openContextMenu();
    // The caption is produced by the same table the key dispatcher reads, so a
    // Mac caption IS proof the dispatcher will answer Cmd+Shift+N.
    expect(within(menu).getByRole("menuitem", { name: /New folder/ })).toHaveTextContent("⇧⌘N");
  });

  it("reads Ctrl on Windows, with no platform prop passed", async () => {
    stubBrowserPlatform("Win32", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)");
    mount();
    await screen.findByText("report.csv");

    const menu = await openContextMenu();
    expect(within(menu).getByRole("menuitem", { name: /New folder/ })).toHaveTextContent(
      "Ctrl+Shift+N",
    );
  });

  it("still lets a caller pin the platform", async () => {
    stubBrowserPlatform("Win32", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)");
    const client = createQueryClient();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/files/nd_home"]}>
          <Routes>
            <Route path="/files/:nodeId" element={<FilesScreen platform="mac" />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText("report.csv");

    const menu = await openContextMenu();
    expect(within(menu).getByRole("menuitem", { name: /New folder/ })).toHaveTextContent("⇧⌘N");
  });
});

// The keyboard move as a person does it: cut a row, walk to another folder, paste.
// The walk is the point — after it the cut row is no longer listed, so nothing on
// the page can look its version up any more, and a paste that tries sends the move
// with an empty `If-Match`, which the server refuses (428) and the page never shows.
// The clipboard has to carry the version the row was cut at.
describe("a cut survives the walk to the folder it is pasted in", () => {
  const platforms = [
    {
      name: "a Mac",
      platform: "MacIntel",
      ua: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      accel: "Meta",
    },
    {
      name: "Windows",
      platform: "Win32",
      ua: "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      accel: "Control",
    },
  ] as const;

  const other = item({ id: "nd_other", name: "notes.md", nameDisplay: "notes.md", etag: "et_9" });
  const archive = item({ id: "nd_archive", kind: "folder", name: "archive", nameDisplay: "archive" });
  /** Home lists the file and a folder; the folder lists something else entirely. */
  const walkable = { nd_home: [item(), archive], nd_archive: [other] } as const;

  /** Select report.csv at home, cut it, and walk into archive — where it is not listed. */
  async function cutAndWalk(user: ReturnType<typeof userEvent.setup>, accel: string): Promise<void> {
    const browser = screen.getByRole("region", { name: "Files" });
    await user.click(await within(browser).findByText("report.csv"));
    await user.keyboard(`{${accel}>}x{/${accel}}`);
    // Opening a folder row is an in-page route, the same walk the live page takes.
    await user.dblClick(within(browser).getByText("archive"));
    await within(browser).findByText("notes.md");
    expect(within(browser).queryByText("report.csv")).toBeNull();
    // Put the keyboard back in the listing without changing what is selected.
    (document.querySelector('[data-row-id="nd_other"]') as HTMLElement).focus();
  }

  it.each(platforms)(
    "on $name the paste moves the row with the etag it was cut at",
    async ({ platform, ua, accel }) => {
      stubBrowserPlatform(platform, ua);
      const wire: Wire[] = [];
      stubApi({ childrenOf: walkable, wire });
      mount();
      const user = userEvent.setup();
      await cutAndWalk(user, accel);

      await user.keyboard(`{${accel}>}v{/${accel}}`);

      await waitFor(() => expect(wire.some((call) => call.method === "PATCH")).toBe(true));
      const move = wire.find((call) => call.method === "PATCH");
      expect(move?.url).toContain("/items/nd_1");
      expect(move?.body).toEqual({ parentId: "nd_archive" });
      // Not the destination's row, and not blank: the version report.csv had when it was cut.
      expect(move?.ifMatch).toBe("et_1");
    },
  );

  it("the other platform's modifier is not a paste", async () => {
    stubBrowserPlatform("MacIntel", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)");
    const wire: Wire[] = [];
    stubApi({ childrenOf: walkable, wire });
    mount();
    const user = userEvent.setup();
    await cutAndWalk(user, "Meta");

    await user.keyboard("{Control>}v{/Control}");

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(wire.filter((call) => call.method === "PATCH")).toEqual([]);
  });
});

describe("exactly one element answers to Breadcrumb", () => {
  it("leaves the trail nav as the only one", async () => {
    mount();
    await screen.findByText("report.csv");

    expect(screen.getAllByLabelText("Breadcrumb")).toHaveLength(1);
    expect(screen.getByLabelText("Breadcrumb").tagName).toBe("NAV");
  });
});

describe("create and upload are reachable without a right-click", () => {
  it("offers all three as buttons in the browser bar", async () => {
    mount();
    await screen.findByText("report.csv");

    const group = screen.getByRole("group", { name: "Create" });
    for (const name of ["New folder", "Upload files", "Upload folder"]) {
      expect(within(group).getByRole("button", { name })).toBeInTheDocument();
    }
  });

  it("the New folder button opens the same form the menu item opens", async () => {
    mount();
    await screen.findByText("report.csv");

    // The menu item first, so the two paths are compared against one another
    // rather than against a hard-coded expectation of what either does.
    const menu = await openContextMenu();
    await userEvent.click(within(menu).getByRole("menuitem", { name: /New folder/ }));
    await screen.findByRole("form", { name: "New folder" });
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("form", { name: "New folder" })).not.toBeInTheDocument();
    });

    await userEvent.click(
      within(screen.getByRole("group", { name: "Create" })).getByRole("button", {
        name: "New folder",
      }),
    );
    expect(await screen.findByRole("form", { name: "New folder" })).toBeInTheDocument();
  });

  it("the New folder form takes the buttons' place in the bar instead of pushing the rows down", async () => {
    mount();
    await screen.findByText("report.csv");
    const bar = document.querySelector(".alk-files-browser__bar")!;
    const rowsBefore = screen.getAllByRole("row").length;

    await userEvent.click(
      within(screen.getByRole("group", { name: "Create" })).getByRole("button", {
        name: "New folder",
      }),
    );

    const form = await screen.findByRole("form", { name: "New folder" });
    // In the bar, where the buttons were — not a new block above the listing.
    expect(bar.contains(form)).toBe(true);
    expect(screen.queryByRole("group", { name: "Create" })).toBeNull();
    expect(screen.getAllByRole("row")).toHaveLength(rowsBefore);
    // A field called "name" makes the browser offer contact names for it.
    const field = screen.getByRole("textbox", { name: "Folder name" });
    expect(field).toHaveAttribute("autocomplete", "off");
    expect(field.getAttribute("name")).not.toBe("name");

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("group", { name: "Create" })).toBeInTheDocument();
  });

  it("puts every toolbar control and every sortable header in the tab order", async () => {
    mount();
    await screen.findByText("report.csv");

    // Everything the Tab key can land on, in the order it lands on it. A control a
    // mouse can use and a keyboard cannot is the defect — the row of buttons above
    // the listing and the headers that re-sort it have to be reachable too.
    const stops = [...document.querySelectorAll(TABBABLE)]
      .filter((element) => !element.hasAttribute("hidden"))
      .map((element) => element.getAttribute("aria-label") ?? element.textContent?.trim() ?? "");

    for (const control of ["New folder", "Upload files", "Upload folder", "List", "Grid"]) {
      expect(stops, `Tab never reaches ${control}`).toContain(control);
    }
    for (const column of ["Name", "Kind", "Size", "Modified"]) {
      expect(stops, `Tab never reaches the ${column} header`).toContain(`Sort by ${column}`);
    }
    // The bar comes before the listing it acts on, and the grid's single roving stop
    // comes after — its arrow keys take over from there, which is why the listing is
    // one stop and not one per row.
    const rowStop = document.querySelector('[data-row-id][tabindex="0"]');
    expect(rowStop).not.toBeNull();
    expect(stops.indexOf("Grid")).toBeLessThan(stops.indexOf(rowStop!.textContent!.trim()));
  });

  it("walks the bar, the headers and the listing with Tab alone", async () => {
    mount();
    await screen.findByText("report.csv");

    // From the top of the page, every stop is the Tab key's own.
    const stops: string[] = [];
    for (let press = 0; press < 30; press += 1) {
      await userEvent.tab();
      const at = document.activeElement as HTMLElement;
      stops.push(at.getAttribute("aria-label") ?? at.textContent?.trim() ?? at.tagName);
      if (at.hasAttribute("data-row-id")) break;
    }
    const order = ["New folder", "Upload files", "Upload folder", "List", "Grid", "Sort by Name"];
    const reached = order.map((name) => stops.indexOf(name));
    expect(reached, `Tab stopped on ${JSON.stringify(stops)}`).not.toContain(-1);
    expect([...reached].sort((a, b) => a - b)).toEqual(reached);
    expect(document.activeElement).toHaveAttribute("data-row-id", "nd_1");

    // One more Tab leaves the listing as a single stop rather than falling to the body.
    await userEvent.tab();
    expect(document.activeElement).not.toBe(document.body);
    expect(document.activeElement?.closest('[role="treegrid"]')).toBeNull();
  });

  it.each([
    ["Enter", "{Enter}"],
    ["Space", " "],
  ])("%s on New folder opens the name field", async (_key, keys) => {
    mount();
    await screen.findByText("report.csv");
    within(screen.getByRole("group", { name: "Create" }))
      .getByRole("button", { name: "New folder" })
      .focus();
    await userEvent.keyboard(keys);
    expect(await screen.findByRole("textbox", { name: "Folder name" })).toBeInTheDocument();
  });

  it("Enter on the upload buttons opens the pickers", async () => {
    mount();
    await screen.findByText("report.csv");
    const opened: string[] = [];
    (screen.getByLabelText("Upload files") as HTMLInputElement).click = () => opened.push("files");
    (screen.getByLabelText("Upload folder") as HTMLInputElement).click = () =>
      opened.push("folder");

    const group = screen.getByRole("group", { name: "Create" });
    within(group).getByRole("button", { name: "Upload files" }).focus();
    await userEvent.keyboard("{Enter}");
    within(group).getByRole("button", { name: "Upload folder" }).focus();
    await userEvent.keyboard(" ");

    expect(opened).toEqual(["files", "folder"]);
  });

  it("Space on Grid switches the view", async () => {
    mount();
    await screen.findByText("report.csv");
    const grid = within(screen.getByRole("group", { name: "View" })).getByRole("button", {
      name: "Grid",
    });
    expect(grid).toHaveAttribute("aria-pressed", "false");
    grid.focus();
    await userEvent.keyboard(" ");
    expect(grid).toHaveAttribute("aria-pressed", "true");
  });

  it("Escape cancels the New folder form, the way it cancels a rename", async () => {
    const wire: Wire[] = [];
    stubApi({ wire });
    mount();
    await screen.findByText("report.csv");

    await userEvent.click(
      within(screen.getByRole("group", { name: "Create" })).getByRole("button", {
        name: "New folder",
      }),
    );
    // The form opens focused, so Escape is the reflex reach for the way out — and
    // Cancel was the only one there was.
    const field = await screen.findByRole("textbox", { name: "Folder name" });
    await userEvent.type(field, "quarterlies");
    await userEvent.keyboard("{Escape}");

    expect(screen.queryByRole("form", { name: "New folder" })).toBeNull();
    expect(await screen.findByRole("group", { name: "Create" })).toBeInTheDocument();
    // Cancelled, not created: the name that was typed never left the browser.
    expect(wire.filter((call) => call.url.includes("/children"))).toEqual([]);
  });

  it("the upload buttons open the same hidden pickers the menu items open", async () => {
    mount();
    await screen.findByText("report.csv");

    const filePicker = screen.getByLabelText("Upload files") as HTMLInputElement;
    const folderPicker = screen.getByLabelText("Upload folder") as HTMLInputElement;
    const opened: string[] = [];
    filePicker.click = () => opened.push("files");
    folderPicker.click = () => opened.push("folder");

    const menu = await openContextMenu();
    await userEvent.click(within(menu).getByRole("menuitem", { name: /Upload files/ }));

    const group = screen.getByRole("group", { name: "Create" });
    await userEvent.click(within(group).getByRole("button", { name: "Upload files" }));
    await userEvent.click(within(group).getByRole("button", { name: "Upload folder" }));

    // The button reaches the very same input element the menu item does.
    expect(opened).toEqual(["files", "files", "folder"]);
  });
});
