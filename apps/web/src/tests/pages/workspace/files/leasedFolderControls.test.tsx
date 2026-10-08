// A folder a chat's machine is holding says which chat holds it. Under a lease
// that does not admit writes from the web it is read-only on the Files page for
// the whole lease; under one that does, the page offers the same writes as the
// chat's Files tab, and the drive hands them to the machine.
//
// While the lease lasts the machine is the writer and the rows on screen are
// the saved copy. So every write from this page is refused before any request:
// the create buttons, the menu rows that would add to the folder or change a
// row in it, the keys those rows share, a drop from the desktop, and the same
// again in a folder inside the leased one. Reading, downloading and copying
// out keep working, and everything comes back on its own when the lease ends.
//
// The refusal is NOT a permission: the reader's grant is untouched, which is
// why it reads off the lease facet rather than off `can_write`, and why the
// grant's own refusal is named first when both apply.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { resetRealtimeStatus, useRealtimeStatus } from "@/api/events/status";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import {
  buildContextMenuItems,
  IN_LEASED_FOLDER,
  LEASED_HERE,
} from "@/pages/workspace/files/contextMenuItems";
import type { DropEntry } from "@/pages/workspace/files/dropHandlers";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";
import { filesLiveFact } from "@/tests/fixtures/statusFacts";

const CREATE_LABELS = ["New folder", "Upload files", "Upload folder"] as const;
/** The menu rows that change a row of the listing, and the rows that add to it. */
const ROW_WRITES = ["Rename", "Cut", "Move to…", "Move to trash"] as const;
const READ_ROWS = ["Open", "Download", "Copy", "Copy to…", "Share…", "Copy link", "Details"] as const;

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    name: "report.csv",
    nameDisplay: "report.csv",
    parentId: "nd_work",
    etag: "et_1",
    stale: false,
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

const PLAIN_FOLDER = item({
  id: "nd_work",
  kind: "folder",
  name: "work",
  nameDisplay: "work",
  parentId: "nd_home",
} as unknown as Partial<Item>);

/** The facet a box's lease puts on the folder and on everything under it. */
const chatLease = (over: Record<string, unknown> = {}) => ({
  holder: "Dana",
  machine: "gpu-1",
  purpose: "chat",
  live: true,
  status: filesLiveFact("live"),
  pending: 0,
  since: "2026-09-16T10:00:00Z",
  last_sync_at: "2026-09-16T10:05:00Z",
  expires_at: "2100-01-01T00:00:00Z",
  chat_id: "ch_1",
  chat_title: "Q3 warehouse spike",
  can_open_chat: true,
  ...over,
});

/** The same folder while a machine is holding it and writing into it. */
const leasedFolder = (over: Record<string, unknown> = {}): Item =>
  ({ ...PLAIN_FOLDER, lease: chatLease(over) }) as unknown as Item;

/** A folder inside the leased one: it carries the facet the way every node
 *  under a lease does, and nothing else says it is held. */
const subfolderOf = (folder: Item): Item =>
  ({
    ...item({ id: "nd_sub", kind: "folder", name: "notes", nameDisplay: "notes" }),
    lease: folder.lease,
    stale: folder.stale,
  }) as unknown as Item;

const REPORT = item();

interface Seen {
  method: string;
  path: string;
}

interface Script {
  /** Every request that would change something, in order. */
  writes: () => Seen[];
}

/** The folder read is answered from a box the test can swap mid-run, so the
 *  lease can end the way it really does: the server stops sending the facet and
 *  a frame tells the page to look again. No remount, no second render tree. */
function stubApi(folder: { current: Item }, listed?: { current: Item }): Script {
  const seen: Seen[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = request.url;
      const path = new URL(url).pathname;
      const method = request.method.toUpperCase();
      if (method !== "GET" && method !== "HEAD") seen.push({ method, path });
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/permissions")) return answer({ value: [], nextMarker: null });
      if (url.includes("/search")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children"))
        return answer({ value: [HOME], nextMarker: null });
      if (url.includes("/items/nd_home/children"))
        return answer({ value: [folder.current], nextMarker: null });
      if (url.includes("/items/nd_work/children")) {
        const sub = subfolderOf(folder.current);
        return answer({
          value: [{ ...REPORT, lease: folder.current.lease }, sub],
          nextMarker: null,
        });
      }
      if (url.includes("/items/nd_sub/children")) {
        return answer({
          value: [{ ...REPORT, id: "nd_deep", parentId: "nd_sub", lease: folder.current.lease }],
          nextMarker: null,
        });
      }
      if (url.includes("/children")) return answer({ value: [], nextMarker: null });
      if (url.includes("/items/nd_work")) return answer(folder.current);
      if (url.includes("/items/nd_sub")) return answer(listed?.current ?? subfolderOf(folder.current));
      if (url.includes("/items/")) return answer(HOME);
      return answer({});
    }),
  );
  return { writes: () => seen };
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

function mount(at = "/files/nd_work") {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen platform="mac" />} />
          <Route path="/chat/:chatId" element={<p>the chat page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The machine gave the folder back: the stream says the lease row moved, which
 *  is the page's own cue to re-read the folder. */
const leaseFrame = (): RealtimeEventFrame => ({
  type: "file_lease.changed",
  entity: "file_lease",
  entity_id: "lse_1",
  lease_node_id: "nd_work",
  version: 2,
  org_id: "or_1",
  drive_id: "dr_1",
});

const createButtons = () =>
  CREATE_LABELS.map((label) => screen.getByRole("button", { name: label }));

const grid = () => screen.getByRole("treegrid");
const rowNamed = (name: string) =>
  within(grid()).getByRole("row", { name: new RegExp(`^${name}`) });
const listing = () => document.querySelector<HTMLElement>(".alk-files__drop")!;

/** The first cell of a row: the one the name is printed in. */
const firstCell = (name: string) =>
  within(rowNamed(name)).getAllByRole("gridcell")[0] as HTMLElement;

/** Select a row the way a person does: a click on it. The mark is the ROW's —
 *  a treegrid selects rows, not cells. */
async function select(name: string): Promise<HTMLElement> {
  await waitFor(() => rowNamed(name));
  fireEvent.click(firstCell(name));
  await waitFor(() => expect(rowNamed(name)).toHaveAttribute("aria-selected", "true"));
  return rowNamed(name);
}

/** What jsdom has no `DataTransfer` for: the fields a desktop drop reads. */
function desktopDrag(entries: DropEntry[]) {
  return {
    types: ["Files"],
    items: entries.map((entry) => ({
      kind: "file",
      webkitGetAsEntry: () => entry,
      getAsFile: () => null,
    })),
    effectAllowed: "",
    dropEffect: "",
    setData: () => undefined,
    getData: () => "",
  };
}

function fileEntry(name: string): DropEntry {
  const file = new File(["x"], name, { lastModified: Date.UTC(2020, 0, 1) });
  return { isFile: true, isDirectory: false, name, file: (onSuccess) => onSuccess(file) };
}

/** Let anything a refused action could have started settle, so "no request"
 *  is asserted after the point a request would have been sent. */
const settled = () => new Promise((resolve) => setTimeout(resolve, 60));

async function awaitHeld(): Promise<void> {
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "New folder" })).toBeDisabled();
  });
}

async function openMenuOn(name: string): Promise<HTMLElement> {
  await waitFor(() => rowNamed(name));
  await userEvent.pointer({ keys: "[MouseRight]", target: firstCell(name) });
  return screen.findByRole("menu");
}

/** The row with exactly this label. A row's accessible name is its label and
 *  then its shortcut, so "Open" must not match "Open in new tab": the label may
 *  be followed by a shortcut glyph or nothing, never by another word. */
const menuRow = (menu: HTMLElement, label: string) => {
  const exact = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return within(menu).getByRole("menuitem", { name: new RegExp(`^${exact}(?!\\s[a-z])`) });
};

beforeEach(() => {
  stubViewport();
  resetFrameBus();
  resetRealtimeStatus();
  useRealtimeStatus.getState().setSse("connected");
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  resetFrameBus();
  resetRealtimeStatus();
});

describe("the create controls inside a leased folder", () => {
  it("greys out all three and says the folder is read-only for the lease", async () => {
    stubApi({ current: leasedFolder() });
    mount();

    await waitFor(() => expect(screen.getByRole("group", { name: "Create" })).toBeInTheDocument());
    await awaitHeld();
    for (const button of createButtons()) {
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute("aria-disabled", "true");
      expect(button).toHaveAttribute("title", LEASED_HERE);
    }
  });

  it("leaves them alone in a folder nobody is holding", async () => {
    stubApi({ current: PLAIN_FOLDER });
    mount();

    await waitFor(() => expect(screen.getByRole("group", { name: "Create" })).toBeInTheDocument());
    for (const button of createButtons()) {
      expect(button).toBeEnabled();
      expect(button).not.toHaveAttribute("title");
    }
  });

  it("is refused while the folder is handed back, not only while it is live", async () => {
    // Mid hand-back the machine is still flushing, so a file put here now races
    // the flush and loses. The badge treats it as a moving state and so does this.
    stubApi({ current: leasedFolder({ grantable_after: "2100-01-01T00:00:00Z" }) });
    mount();

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Upload files" })).toBeDisabled();
    });
  });

  it("stays refused while the holder is behind: the lease has not ended", async () => {
    // The user's rule is "until the lease on the folder ends". A machine that
    // stopped syncing has not let go, so the saved-copy line and the read-only
    // folder go together rather than one cancelling the other.
    stubApi({ current: leasedFolder({}) });
    const folder = {
      current: { ...leasedFolder({ status: filesLiveFact("paused_behind") }), stale: true } as Item,
    };
    stubApi(folder);
    mount();

    await awaitHeld();
    expect(screen.getByRole("status")).toHaveTextContent("last synced copy");
  });

  it("hands them back when the lease ends, with no reload", async () => {
    const folder = { current: leasedFolder() };
    stubApi(folder);
    mount();
    await awaitHeld();
    expect(screen.getByRole("status")).toHaveTextContent("is working on");

    // The machine released the folder. The server drops the facet; the frame is
    // what tells the page to look.
    folder.current = PLAIN_FOLDER;
    publishFrame(leaseFrame());

    await waitFor(
      () => {
        expect(screen.getByRole("button", { name: "New folder" })).toBeEnabled();
      },
      { timeout: 3000 },
    );
    for (const button of createButtons()) {
      expect(button).toBeEnabled();
      expect(button).not.toHaveAttribute("title");
    }
    expect(screen.queryByText(/is working on/)).toBeNull();
  });

  it("lets go of a lease that has run out, whatever its flag still says", async () => {
    stubApi({ current: leasedFolder({ expires_at: "2000-01-01T00:00:00Z" }) });
    mount();

    await waitFor(() => expect(screen.getByRole("group", { name: "Create" })).toBeInTheDocument());
    for (const button of createButtons()) expect(button).toBeEnabled();
    expect(screen.queryByText(/is working on/)).toBeNull();
  });
});

describe("the one line over a leased folder", () => {
  // It replaced two sentences that said the same thing above it — what a chat's
  // files ride, and which chat holds the folder. One line now carries the state,
  // the chat and the way there; the other two must not come back.
  const GONE = [
    "These are the chat's files. Anyone who can open the chat can see them.",
    /This folder is leased by the chat/,
  ] as const;

  /** The line, once the page has read the lease it describes. Before that the
   *  only status on screen is the one saying the listing is still opening. */
  async function liveLine(): Promise<HTMLElement> {
    await awaitHeld();
    return screen.getByRole("status");
  }

  it("names the chat instead of the machine, and links to it", async () => {
    stubApi({ current: leasedFolder() });
    mount();

    const live = await liveLine();
    expect(live).toHaveTextContent("Live. Dana is working on Q3 warehouse spike");
    expect(live).not.toHaveTextContent("gpu-1");
    for (const sentence of GONE) expect(screen.queryByText(sentence)).toBeNull();

    const link = within(live).getByRole("link", { name: "Q3 warehouse spike" });
    expect(link).toHaveAttribute("href", "/chat/ch_1");
    await userEvent.click(link);
    expect(await screen.findByText("the chat page")).toBeInTheDocument();
  });

  it("names the chat as plain text when the reader may not open it", async () => {
    stubApi({ current: leasedFolder({ can_open_chat: false }) });
    mount();

    const live = await liveLine();
    expect(live).toHaveTextContent("Live. Dana is working on Q3 warehouse spike");
    expect(within(live).queryByRole("link")).toBeNull();
  });

  it("capitalises a holder the server resolved no name for", async () => {
    // The drive's fallback is the word "someone", and it opens the sentence.
    stubApi({ current: leasedFolder({ holder: null, holder_name: null }) });
    mount();

    expect(await liveLine()).toHaveTextContent("Live. Someone is working on Q3 warehouse spike");
  });

  it("names the machine when the lease is not a chat's, and says nothing over an unleased folder", async () => {
    stubApi({ current: PLAIN_FOLDER });
    const { unmount } = mount();
    await screen.findByText("report.csv");
    expect(screen.queryByText(/is working on/)).toBeNull();
    unmount();
    vi.unstubAllGlobals();
    stubViewport();

    stubApi({ current: leasedFolder({ chat_id: null, chat_title: "", purpose: "box" }) });
    mount();
    const live = await liveLine();
    expect(live).toHaveTextContent("Live. Dana is working on gpu-1");
    expect(within(live).queryByRole("link")).toBeNull();
  });
});

describe("the menu inside a leased folder", () => {
  it("refuses every row that would write, and leaves every row that reads", async () => {
    stubApi({ current: leasedFolder() });
    mount();
    await awaitHeld();
    // Something on the clipboard, so Paste is refused for the lease and not for
    // an empty clipboard.
    const row = await select("report.csv");
    fireEvent.keyDown(row, { key: "c", metaKey: true });

    const menu = await openMenuOn("report.csv");
    for (const label of CREATE_LABELS) {
      const entry = menuRow(menu, label);
      expect(entry).toHaveAttribute("aria-disabled", "true");
      expect(entry).toHaveAttribute("title", LEASED_HERE);
    }
    for (const label of ["Paste", "Duplicate"]) {
      const entry = menuRow(menu, label);
      expect(entry).toHaveAttribute("aria-disabled", "true");
      expect(entry).toHaveAttribute("title", LEASED_HERE);
    }
    for (const label of ROW_WRITES) {
      const entry = menuRow(menu, label);
      expect(entry).toHaveAttribute("aria-disabled", "true");
      expect(entry).toHaveAttribute("title", IN_LEASED_FOLDER);
    }
    for (const label of READ_ROWS) {
      expect(menuRow(menu, label)).not.toHaveAttribute("aria-disabled");
    }
  });

  it("refuses the same rows on a folder inside the leased one", async () => {
    stubApi({ current: leasedFolder() });
    mount();
    await awaitHeld();

    const menu = await openMenuOn("notes");
    for (const label of ROW_WRITES) {
      expect(menuRow(menu, label)).toHaveAttribute("title", IN_LEASED_FOLDER);
    }
    expect(menuRow(menu, "Open")).not.toHaveAttribute("aria-disabled");
  });
});

describe("a leased folder whose lease admits writes from the web", () => {
  // The chat's Files tab offers uploads, new folders and renames in such a
  // folder: the drive hands them to the machine and keeps both sides when the
  // two cross. The Files page is the same folder and offers the same.
  async function heldLine(): Promise<void> {
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Live. Dana is working on Q3 warehouse spike",
      ),
    );
  }

  it("keeps the create buttons and still names the chat holding it", async () => {
    stubApi({ current: leasedFolder({ inbound: true }) });
    mount();
    await heldLine();
    await screen.findByText("report.csv");

    for (const button of createButtons()) {
      expect(button).toBeEnabled();
      expect(button).not.toHaveAttribute("title");
    }
  });

  it("offers Upload files, New folder and Rename in the menu", async () => {
    stubApi({ current: leasedFolder({ inbound: true }) });
    mount();
    await heldLine();

    const menu = await openMenuOn("report.csv");
    for (const label of ["Upload files", "New folder", "Rename", ...ROW_WRITES]) {
      const entry = menuRow(menu, label);
      expect(entry).not.toHaveAttribute("aria-disabled");
      expect(entry).not.toHaveAttribute("title");
    }
  });

  it("offers Rename on a folder inside it", async () => {
    stubApi({ current: leasedFolder({ inbound: true }) });
    mount();
    await heldLine();

    const menu = await openMenuOn("notes");
    expect(menuRow(menu, "Rename")).not.toHaveAttribute("aria-disabled");
  });

  it("still refuses them under a lease that does not admit web writes", async () => {
    stubApi({ current: leasedFolder({ inbound: false }) });
    mount();
    await awaitHeld();

    const menu = await openMenuOn("report.csv");
    expect(menuRow(menu, "Upload files")).toHaveAttribute("title", LEASED_HERE);
    expect(menuRow(menu, "Rename")).toHaveAttribute("title", IN_LEASED_FOLDER);
  });
});

describe("the keys inside a leased folder", () => {
  const script = { current: null as Script | null };

  async function heldWithRow(): Promise<HTMLElement> {
    script.current = stubApi({ current: leasedFolder() });
    mount();
    await awaitHeld();
    return select("report.csv");
  }

  it("F2 opens no rename box and says why", async () => {
    const row = await heldWithRow();
    fireEvent.keyDown(row, { key: "F2" });

    expect(await screen.findByRole("alert")).toHaveTextContent(IN_LEASED_FOLDER);
    expect(within(grid()).queryByRole("textbox")).toBeNull();
  });

  it("Cmd+Backspace trashes nothing", async () => {
    const row = await heldWithRow();
    fireEvent.keyDown(row, { key: "Backspace", metaKey: true });

    expect(await screen.findByRole("alert")).toHaveTextContent(IN_LEASED_FOLDER);
    await settled();
    expect(script.current!.writes()).toEqual([]);
    expect(rowNamed("report.csv")).toBeInTheDocument();
  });

  it("Cmd+X cuts nothing and Cmd+V pastes nothing", async () => {
    const row = await heldWithRow();
    fireEvent.keyDown(row, { key: "x", metaKey: true });
    expect(await screen.findByRole("alert")).toHaveTextContent(IN_LEASED_FOLDER);

    // A copy is a read and is allowed; the paste it sets up is not.
    fireEvent.keyDown(row, { key: "c", metaKey: true });
    fireEvent.keyDown(row, { key: "v", metaKey: true });
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(LEASED_HERE));
    await settled();
    expect(script.current!.writes()).toEqual([]);
  });

  it("Cmd+Shift+N opens no New folder form", async () => {
    const row = await heldWithRow();
    fireEvent.keyDown(row, { key: "n", metaKey: true, shiftKey: true });

    expect(await screen.findByRole("alert")).toHaveTextContent(LEASED_HERE);
    expect(screen.queryByRole("form", { name: "New folder" })).toBeNull();
  });

  it("the same keys work in a folder nobody holds", async () => {
    script.current = stubApi({ current: PLAIN_FOLDER });
    mount();
    const row = await select("report.csv");
    fireEvent.keyDown(row, { key: "F2" });

    expect(within(grid()).getByRole("textbox")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("a drop from the desktop into a leased folder", () => {
  it("is refused on the listing, with no request", async () => {
    const script = stubApi({ current: leasedFolder() });
    mount();
    await awaitHeld();

    fireEvent.drop(listing(), { dataTransfer: desktopDrag([fileEntry("from-laptop.csv")]) });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("work is leased and read-only until the lease ends.");
    await settled();
    expect(script.writes()).toEqual([]);
  });

  it("is refused on a folder row inside it, with no request", async () => {
    const script = stubApi({ current: leasedFolder() });
    mount();
    await awaitHeld();
    const notes = await waitFor(() => rowNamed("notes"));

    fireEvent.drop(notes, { dataTransfer: desktopDrag([fileEntry("from-laptop.csv")]) });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("notes is leased and read-only until the lease ends.");
    await settled();
    expect(script.writes()).toEqual([]);
  });

  it("goes through on the listing of a folder nobody holds", async () => {
    const script = stubApi({ current: PLAIN_FOLDER });
    mount();
    await screen.findByText("report.csv");

    fireEvent.drop(listing(), { dataTransfer: desktopDrag([fileEntry("from-laptop.csv")]) });

    await waitFor(() => expect(script.writes().length).toBeGreaterThan(0));
    expect(script.writes()[0]!.path).toMatch(/\/uploads$/);
  });
});

describe("a folder inside the leased one, opened on its own", () => {
  // The lease is inherited: the subfolder's item carries the same facet, and
  // that is the only thing the page reads. A deep link straight to it, with no
  // trail walked, has to answer the same as the folder the lease is on.
  it("is read-only and wears the same line", async () => {
    const folder = { current: leasedFolder() };
    const script = stubApi(folder);
    mount("/files/nd_sub");

    await waitFor(() => expect(screen.getByRole("group", { name: "Create" })).toBeInTheDocument());
    await awaitHeld();
    for (const button of createButtons()) expect(button).toHaveAttribute("title", LEASED_HERE);
    const live = await screen.findByRole("status");
    expect(within(live).getByRole("link", { name: "Q3 warehouse spike" })).toHaveAttribute(
      "href",
      "/chat/ch_1",
    );

    const menu = await openMenuOn("report.csv");
    for (const label of ROW_WRITES) {
      expect(menuRow(menu, label)).toHaveAttribute("title", IN_LEASED_FOLDER);
    }
    await userEvent.keyboard("{Escape}");

    fireEvent.drop(listing(), { dataTransfer: desktopDrag([fileEntry("x.csv")]) });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "notes is leased and read-only until the lease ends.",
    );
    await settled();
    expect(script.writes()).toEqual([]);
  });
});

describe("buildContextMenuItems and a held folder", () => {
  const held = (row: Item) => row.id === "nd_held";
  const heldRow = item({ id: "nd_held" });
  const menu = (over: {
    canWriteHere?: boolean;
    leasedHere?: boolean;
    isHeld?: (row: Item) => boolean;
    targets?: Item[];
  }) =>
    buildContextMenuItems({
      platform: "mac",
      targets: over.targets ?? [item()],
      currentFolderId: "nd_work",
      canWriteHere: over.canWriteHere ?? true,
      leasedHere: over.leasedHere,
      isHeld: over.isHeld,
      clipboard: { mode: "copy", items: [{ id: "nd_x", etag: "e", name: "x" }], sourceFolderId: "nd_other" },
      onAction: vi.fn(),
    });

  const reason = (items: ReturnType<typeof menu>, id: string) =>
    items.find((row) => row.id === id)?.disabled;
  const ADDS = ["new-folder", "upload-files", "upload-folder", "paste", "duplicate"] as const;
  const CHANGES = ["rename", "cut", "move-to", "trash"] as const;
  const READS = ["open", "download", "copy", "copy-to", "share", "copy-link", "details"] as const;

  it("names the lease on every row that would add to the folder", () => {
    const items = menu({ leasedHere: true });
    for (const id of ADDS) expect(reason(items, id)).toBe(LEASED_HERE);
    // The rows themselves are not held: a listing whose folder is held always
    // has held rows in practice, but the two facts are decided apart.
    for (const id of CHANGES) expect(reason(items, id)).toBeUndefined();
    for (const id of READS) expect(reason(items, id)).toBeUndefined();
  });

  it("names the lease on every row that would change a held row, wherever it is listed", () => {
    // A feed lists rows away from their folders: `leasedHere` is false there and
    // the row's own facet is all there is.
    const items = menu({ leasedHere: false, isHeld: held, targets: [heldRow] });
    for (const id of CHANGES) expect(reason(items, id)).toBe(IN_LEASED_FOLDER);
    expect(reason(items, "duplicate")).toBe(IN_LEASED_FOLDER);
    for (const id of READS) expect(reason(items, id)).toBeUndefined();
    for (const id of ["new-folder", "upload-files", "upload-folder", "paste"]) {
      expect(reason(items, id)).toBeUndefined();
    }
  });

  it("one held row in a selection refuses the change for all of them", () => {
    // Rename takes a single row and says so first; the rows that take many
    // are refused as a whole, because a drag of five that moves three is a
    // surprise nobody asked for.
    const items = menu({ isHeld: held, targets: [item(), heldRow] });
    for (const id of ["cut", "move-to", "trash", "duplicate"]) {
      expect(reason(items, id)).toBe(IN_LEASED_FOLDER);
    }
    // Rename takes a single row, so on two it is not in the menu to refuse.
    expect(items.find((row) => row.id === "rename")).toBeUndefined();
  });

  it("says nothing about a lease when there is none", () => {
    const free = menu({});
    for (const id of [...ADDS, ...CHANGES, ...READS]) expect(reason(free, id)).toBeUndefined();
    const flagged = menu({ leasedHere: false, isHeld: () => false });
    for (const id of [...ADDS, ...CHANGES, ...READS]) expect(reason(flagged, id)).toBeUndefined();
  });

  it("names the missing grant first when the reader could not write here anyway", () => {
    // Two reasons, one row. The grant is the one that survives the lease ending,
    // so it is the one a person is told about.
    const items = menu({ canWriteHere: false, leasedHere: true });
    for (const id of ["new-folder", "upload-files", "upload-folder", "paste", "duplicate"]) {
      expect(reason(items, id)).toBe("You cannot add to this folder.");
    }
    const noRename = item({
      id: "nd_held",
      capabilities: { can_read: true, can_rename: false, can_write: false, can_delete: false },
    } as unknown as Partial<Item>);
    const rows = menu({ isHeld: held, targets: [noRename] });
    expect(reason(rows, "rename")).toBe("You cannot rename this item.");
    expect(reason(rows, "cut")).toBe("You cannot change this item.");
    expect(reason(rows, "trash")).toBe("You cannot delete this item.");
  });

  it("the two sentences carry no em dash and end in a full stop", () => {
    for (const line of [LEASED_HERE, IN_LEASED_FOLDER]) {
      expect(line).not.toMatch(/[—–]/);
      expect(line.endsWith(".")).toBe(true);
    }
  });
});
