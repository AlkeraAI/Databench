// Watching the agent work, from the chat's own file browser.
//
// The unit tests beside the hooks prove `useFolderLiveness` refetches the right
// query for the right frame. This file proves the SURFACE is wired to them: a
// frame published on the bus reaches the listing the reader is looking at, the
// status bar under it follows the lease as it changes rather than freezing at
// what the tab read on mount, and a row the machine is touching says so.
//
// Everything is driven through the real explorer with `fetch` stubbed at the
// wire, so what the tab learned is asserted by the requests it made and the DOM
// it painted — never by a mock reporting on itself.

import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import { useRealtimeStatus } from "@/api/events/status";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import {
  tabKindFor,
  type WorkspaceCtx,
  type WorkspaceTab,
} from "@/pages/workspace/chat/workspace/tabKinds";
import { useWorkspaceStore } from "@/pages/workspace/chat/workspace/workspaceStore";

// Imported for its registration only; the component comes from the registry.
import "@/pages/workspace/chat/workspace/FilesTab";
import { filesLiveFact } from "@/tests/fixtures/statusFacts";

const DRIVE = "drv_1";
const CHAT_ID = "cht_1";
const CHAT_NODE = "nd_chat";
/** The chat's working directory: the tab's root, and the leased node. */
const ROOT = "nd_root";
const SUB = "nd_charts";

const CTX: WorkspaceCtx = { chatId: CHAT_ID, driveId: DRIVE, rootNodeId: ROOT };

/** What a box's lease carries when the server resolved no name: the allocation
 *  uuid, as both the holder and the machine. */
const MACHINE_ID = "807bf89a-464c-4867-8b08-6e020a9bd8a3";

function item(overrides: Partial<Item> & { id: string; name: string }): Item {
  return {
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    subtype: null,
    nameDisplay: overrides.name,
    nameEncoding: "utf-8",
    nameFlags: { windows_safe: true, macos_safe: true, display_warning: false },
    pathBytes: "",
    parentId: ROOT,
    path: null,
    etag: "e1",
    ctag: "c1",
    attrs: { mtime: "2026-09-01T10:00:00Z" },
    file: null,
    symlink: null,
    object: null,
    lease: null,
    live: null,
    stale: false,
    trust: null,
    locked: false,
    held: false,
    capabilities: { can_write: true },
    shared: false,
    trashed: false,
    ...overrides,
  } as unknown as Item;
}

const CHAT_FOLDER = item({
  id: CHAT_NODE,
  name: "3952c9e2.alkerachat",
  kind: "folder",
  parentId: "nd_home",
  object: {
    id: "obj_chat_1",
    type: "chat",
    title: "Q3 review",
    web_url: "/chat/cht_1",
    metadata: { files_node_id: ROOT },
  } as Item["object"],
});

const ROOT_FOLDER = item({ id: ROOT, name: "scratch", kind: "folder", parentId: CHAT_NODE });

/** The lease the box takes while the chat is awake. It is taken over the
 *  CHAT's folder — the working directory inside it is part of what is held. */
function leased(node: Item, over: Record<string, unknown> = {}): Item {
  return {
    ...node,
    lease: {
      holder: "Dana",
      machine: "box-1",
      machine_name: "demo-box",
      live: true,
      status: filesLiveFact("live"),
      inbound: true,
      pending: 0,
      expires_at: new Date(Date.now() + 600_000).toISOString(),
      ...over,
    },
  } as unknown as Item;
}

const REPORT = item({
  id: "nd_report",
  name: "q3-report.html",
  file: { size: 900 } as Item["file"],
});
const CHARTS = item({ id: SUB, name: "charts", kind: "folder" });

/** Every URL the tab asked for, in order. */
let calls: string[] = [];
let items: Record<string, Item>;
let children: Record<string, Item[]>;

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
}

/** The tab reads the chat's folder only after the working directory's read
 *  names it as the parent, so on a real wire, and on a loaded runner, the rows
 *  paint before the tab knows which lease they live under. The first read of
 *  the chat's folder is answered late here so every case runs in that order,
 *  rather than only the runs a busy machine happens to schedule that way. */
const CHAT_FOLDER_LAG_MS = 200;
let chatFolderAnswered = false;
/** Set by a case that decides itself when the chat's folder is answered. */
let chatFolderGate: Promise<void> | null = null;

function stubWire(): void {
  calls = [];
  chatFolderAnswered = false;
  chatFolderGate = null;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      calls.push(url);
      const listing = /\/items\/([^/?]+)\/children/.exec(url);
      if (listing) {
        const rows = children[decodeURIComponent(listing[1] ?? "")] ?? [];
        return json({ value: rows, nextMarker: null });
      }
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const found = items[decodeURIComponent(one[1] ?? "")];
        if (!found) return json({ code: "files.not_found", message: "no" }, 404);
        if (found.id === CHAT_NODE && !chatFolderAnswered) {
          chatFolderAnswered = true;
          const late =
            chatFolderGate ??
            new Promise<void>((resolve) => setTimeout(resolve, CHAT_FOLDER_LAG_MS));
          // Answered with what the drive holds when the answer goes out.
          return late.then(() => json(items[CHAT_NODE]));
        }
        return json(found);
      }
      return json({ value: [], nextMarker: null });
    }),
  );
}

function mount(tab: Partial<WorkspaceTab> = {}, ctx: WorkspaceCtx = CTX) {
  const kind = tabKindFor("files");
  if (kind === undefined) throw new Error("the `files` tab kind was never registered");
  const Component = kind.Component;
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Component tab={{ id: "files", kind: "files", name: "Files", ...tab }} ctx={ctx} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const nodeFrame = (parentId: string | undefined, entityId = "nd_new"): RealtimeEventFrame =>
  ({
    type: "file_node.changed",
    entity: "file_node",
    entity_id: entityId,
    version: 2,
    org_id: "org_1",
    drive_id: DRIVE,
    ...(parentId === undefined ? {} : { parent_id: parentId }),
  }) as RealtimeEventFrame;

const leaseFrame = (leaseNodeId: string): RealtimeEventFrame =>
  ({
    type: "file_lease.changed",
    entity: "file_lease",
    entity_id: leaseNodeId,
    version: 3,
    org_id: "org_1",
    drive_id: DRIVE,
    lease_node_id: leaseNodeId,
  }) as RealtimeEventFrame;

/** Wait until the tab has read the chat's folder above its working directory.
 *  The box's lease is taken on that folder, so until the read lands the tab
 *  cannot tell that a lease frame naming it is its own. The trail names the
 *  conversation off the same read. */
async function chatFolderRead(): Promise<void> {
  await screen.findByText("Q3 review");
}

/** Publish a frame and let the hook's coalescing window close. */
async function deliver(frame: RealtimeEventFrame): Promise<void> {
  await act(async () => {
    publishFrame(frame);
    await new Promise((resolve) => setTimeout(resolve, 300));
  });
}

function requestsFor(fragment: string): number {
  return calls.filter((url) => url.includes(fragment)).length;
}

function rowNames(): string[] {
  return Array.from(document.querySelectorAll("[data-row-id]")).map(
    (element) => element.textContent ?? "",
  );
}

function chipOn(rowId: string): string | null {
  const row = document.querySelector(`[data-row-id="${rowId}"]`);
  return row?.querySelector(".alk-files-live-chip")?.textContent ?? null;
}

/** The bar at the foot of the tab. Always there: what is happening to the
 *  folder is a permanent fact, and "nothing" is one of its states. */
function statusBar(): HTMLElement {
  const bar = document.querySelector<HTMLElement>(".alk-ws-status");
  if (bar === null) throw new Error("the tab rendered no status bar");
  return bar;
}

function barState(): string | null {
  return statusBar().getAttribute("data-state");
}

function barChips(): string[] {
  return Array.from(statusBar().querySelectorAll(".alk-ws-status__chips .alk-pill")).map(
    (chip) => chip.textContent ?? "",
  );
}

beforeEach(() => {
  items = { [CHAT_NODE]: CHAT_FOLDER, [ROOT]: ROOT_FOLDER, [REPORT.id]: REPORT, [SUB]: CHARTS };
  children = { [ROOT]: [REPORT, CHARTS], [SUB]: [] };
  stubWire();
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "connected" });
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetFrameBus();
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "idle" });
});

describe("the agent writing into the folder on screen", () => {
  it("paints a file the machine has just written, with no navigation", async () => {
    mount();
    await screen.findByText("q3-report.html");

    // The agent writes a chart into the directory the reader is looking at, and
    // the server says so.
    const chart = item({ id: "nd_png", name: "q3.png", file: { size: 20 } as Item["file"] });
    children = { ...children, [ROOT]: [REPORT, CHARTS, chart] };
    items = { ...items, [chart.id]: chart };
    await deliver(nodeFrame(ROOT, chart.id));

    await waitFor(() => expect(rowNames().join(" ")).toContain("q3.png"));
  });

  it("ignores a write into a folder nobody here has open", async () => {
    mount();
    await screen.findByText("q3-report.html");
    const before = requestsFor(`/items/${ROOT}/children`);

    // Deep inside the chat, under a folder this tab is not showing: refreshing
    // the listing on screen would buy nothing and cost a request per save.
    await deliver(nodeFrame(SUB, "nd_deep"));

    expect(requestsFor(`/items/${ROOT}/children`)).toBe(before);
  });
});

describe("the status bar under the listing", () => {
  it("follows the lease from a saved copy to live when the machine takes the folder", async () => {
    mount();
    await screen.findByText("q3-report.html");
    // Nothing is happening to the folder yet: the bar is still there, saying
    // what the reader is looking at and when it was written.
    await chatFolderRead();
    await waitFor(() => expect(statusBar()).toHaveTextContent(/^Saved copySaved .+ ago$/));
    expect(barState()).toBe("persisted");

    // The machine acquires the chat's folder and starts pushing.
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER, { landing_count: 2 }) };
    await deliver(leaseFrame(CHAT_NODE));

    await waitFor(() => expect(barState()).toBe("landing"));
    expect(statusBar()).toHaveTextContent(/^Livedemo-box/);
    expect(barChips()).toEqual(["2 files syncing"]);
    // And the timestamp goes with it: this is no longer a saved copy.
    expect(statusBar()).not.toHaveTextContent(/Saved/);
  });

  it("leaves the bar alone for a lease on somebody else's folder", async () => {
    mount();
    await screen.findByText("q3-report.html");
    await waitFor(() => expect(barState()).toBe("persisted"));
    await chatFolderRead();

    // The same server frame, about a folder that is not this chat's.
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER) };
    await deliver(leaseFrame("nd_someone_else"));

    expect(barState()).toBe("persisted");
    expect(statusBar()).toHaveTextContent(/Saved .+ ago$/);
  });

  it("reads the lease from the chat's folder, not the working directory inside it", async () => {
    // What the box actually holds is the chat's folder; the directory the tab
    // is rooted at is one node inside that hold. A tab that asked the working
    // directory would be asking the node the lease is not on.
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER) };
    mount();
    await screen.findByText("q3-report.html");

    await waitFor(() => expect(barState()).toBe("live"));
    expect(statusBar()).toHaveTextContent("demo-box");
  });

  it("reads its own folder's lease for a chat that has no working directory", async () => {
    // A chat old enough to predate the working directory IS its folder, so that
    // is both the root and the leased node.
    const oldChat = item({
      ...CHAT_FOLDER,
      object: { ...CHAT_FOLDER.object, metadata: {} } as Item["object"],
    });
    children = { ...children, [CHAT_NODE]: [REPORT] };
    items = { ...items, [CHAT_NODE]: leased(oldChat) };
    mount({}, { chatId: CHAT_ID, driveId: DRIVE, rootNodeId: CHAT_NODE });
    await screen.findByText("q3-report.html");

    await waitFor(() => expect(barState()).toBe("live"));
  });

  it("stops claiming live once the event stream stops delivering", async () => {
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER, { landing_count: 2 }) };
    mount();
    await waitFor(() => expect(barState()).toBe("landing"));

    // With no stream the browser cannot learn about a save, so the lease's own
    // word for it is not enough to keep the promise, and neither is any count
    // it took from an earlier frame.
    await act(async () => {
      useRealtimeStatus.getState().setSse("down");
      await Promise.resolve();
    });

    expect(barState()).toBe("stream-down");
    expect(statusBar()).toHaveTextContent("Stream down");
    expect(barChips()).toEqual([]);
  });

  it("names the machine and counts what is moving, with no id anywhere on it", async () => {
    // The shape the reader complained about: the facet carries the allocation
    // id as both the holder and the machine, and the bar shows neither.
    items = {
      ...items,
      [CHAT_NODE]: leased(CHAT_FOLDER, {
        holder: MACHINE_ID,
        machine: MACHINE_ID,
        landing_count: 2,
      }),
    };
    children = {
      ...children,
      [ROOT]: [
        { ...REPORT, live: { state: "on_box" } } as Item,
        { ...CHARTS, live: { state: "deferred" } } as Item,
      ],
    };
    mount();
    await waitFor(() => expect(barState()).toBe("landing"));

    expect(statusBar()).toHaveTextContent("demo-box");
    expect(barChips()).toEqual(["2 files syncing", "2 files on the machine"]);
    expect(statusBar().textContent ?? "").not.toMatch(
      /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i,
    );
  });

  it("goes from files landing to live as the landing count drains", async () => {
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER, { landing_count: 17 }) };
    mount();
    await waitFor(() => expect(barState()).toBe("landing"));
    expect(barChips()).toEqual(["17 files syncing"]);

    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER, { landing_count: 0 }) };
    await deliver(leaseFrame(CHAT_NODE));

    await waitFor(() => expect(barState()).toBe("live"));
    expect(statusBar()).toHaveTextContent(/^Livedemo-box$/);
  });

  it("says the machine is offline when the server stops serving it live", async () => {
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER, { landing_count: 3 }) };
    mount();
    await waitFor(() => expect(barState()).toBe("landing"));

    items = {
      ...items,
      [CHAT_NODE]: leased(CHAT_FOLDER, {
        served: "offline",
        status: filesLiveFact("paused_silent"),
        landing_count: 3,
        last_sync_at: "2026-09-23T12:04:00Z",
      }),
    };
    await deliver(leaseFrame(CHAT_NODE));

    await waitFor(() => expect(barState()).toBe("offline"));
    expect(statusBar()).toHaveTextContent(/^Offlinedemo-boxSince .+ · showing last sync$/);
    expect(barChips()).toEqual([]);
  });

  it("says nothing about a count of zero", async () => {
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER) };
    mount();
    await waitFor(() => expect(barState()).toBe("live"));

    expect(barChips()).toEqual([]);
    expect(statusBar()).toHaveTextContent("Live");
  });

  it("is under the listing, not over it", async () => {
    // The point of the bar: a permanent fact belongs at the foot of the tab,
    // where it costs the rows nothing.
    mount();
    await screen.findByText("q3-report.html");
    const table = document.querySelector(".alk-files-grid");
    if (table === null) throw new Error("the tab rendered no listing");
    expect(table.compareDocumentPosition(statusBar())).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });
});

describe("a row the machine is touching", () => {
  it("carries the chip for what is being done to it, and nothing for a row at rest", async () => {
    children = {
      ...children,
      [ROOT]: [{ ...REPORT, live: { state: "writing" } } as Item, CHARTS],
    };
    mount();
    await screen.findByText("q3-report.html");

    await waitFor(() => expect(chipOn(REPORT.id)).toBe("writing…"));
    expect(chipOn(SUB)).toBeNull();
  });

  it("names the machine only on a row whose bytes it will not send", async () => {
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER) };
    const lease = (items[CHAT_NODE] as Item).lease;
    children = {
      ...children,
      [ROOT]: [
        { ...REPORT, lease, live: { state: "writing", content: "unsynced" } } as Item,
        CHARTS,
      ],
    };
    mount();
    await screen.findByText("q3-report.html");

    await waitFor(() => expect(chipOn(REPORT.id)).toBe("left on demo-box, not saved"));
    expect(chipOn(SUB)).toBeNull();
  });

  it.each(["behind", "unlanded"])(
    "a %s row carries no word: awake it is served from the machine, asleep the drive is current",
    async (content) => {
      items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER) };
      const lease = (items[CHAT_NODE] as Item).lease;
      const stale = {
        ...CHARTS,
        lease,
        live: {
          state: "writing",
          content,
          holder_mtime: new Date(Date.now() - 90_000).toISOString(),
        },
      } as Item;
      children = {
        ...children,
        [ROOT]: [{ ...REPORT, lease, live: { state: "writing", content } } as Item, stale],
      };
      mount();
      await screen.findByText("q3-report.html");
      await screen.findByText(CHARTS.name);

      expect(chipOn(REPORT.id)).toBeNull();
      expect(chipOn(SUB)).toBeNull();
    },
  );

  it("lists a file with no bytes on the drive at the size on the machine", async () => {
    // Rows appear before bytes: the drive knows the name and the machine's
    // stat, and an empty size would read as an empty file.
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER) };
    const lease = (items[CHAT_NODE] as Item).lease;
    const unlanded = item({
      id: "nd_readme",
      name: "README.md",
      file: { size: 0, content_hash: "" } as unknown as Item["file"],
      lease,
      live: {
        state: "writing",
        content: "unlanded",
        holder_size: 4_096_000,
        holder_mtime: "2026-09-23T10:15:00Z",
      } as Item["live"],
    });
    children = { ...children, [ROOT]: [unlanded] };
    mount();
    await screen.findByText("README.md");

    const row = document.querySelector(`[data-row-id="${unlanded.id}"]`);
    await waitFor(() => expect(row?.textContent ?? "").toContain("4.1 MB"));
    // The size is the row's word; where the bytes stand is not.
    expect(chipOn(unlanded.id)).toBeNull();
  });

  it("names the size of a file the machine is holding back", async () => {
    children = {
      ...children,
      [ROOT]: [{ ...REPORT, live: { state: "on_box", box_size: 2_000_000 } } as Item, CHARTS],
    };
    mount();
    await screen.findByText("q3-report.html");

    await waitFor(() => expect(chipOn(REPORT.id)).toContain("on the machine"));
    expect(chipOn(REPORT.id)).toContain("2");
  });

  it("drops the chip once the machine is done with the row", async () => {
    children = {
      ...children,
      [ROOT]: [{ ...REPORT, live: { state: "uploading" } } as Item, CHARTS],
    };
    mount();
    await waitFor(() => expect(chipOn(REPORT.id)).toBe("uploading…"));

    children = { ...children, [ROOT]: [REPORT, CHARTS] };
    await deliver(nodeFrame(ROOT, REPORT.id));

    await waitFor(() => expect(chipOn(REPORT.id)).toBeNull());
  });
});

// A leased folder is a live view for the whole lease, not only while a turn
// is writing: the listing fills from nothing as the machine's files land, a
// row's chip follows the plane's word about it even when no save landed
// beside it, and the saved copy is what is shown once the folder is back.
describe("a folder the machine holds", () => {
  it("fills from an empty listing as the machine's files land, under a live notice", async () => {
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER) };
    children = { ...children, [ROOT]: [] };
    mount();
    await waitFor(() => expect(barState()).toBe("live"));
    expect(rowNames()).toEqual([]);

    const chart = item({
      id: "nd_png",
      name: "q3.png",
      file: { size: 20 } as Item["file"],
      live: { state: "uploading" } as Item["live"],
    });
    children = { ...children, [ROOT]: [chart] };
    items = { ...items, [chart.id]: chart };
    await deliver(nodeFrame(ROOT, chart.id));

    await waitFor(() => expect(rowNames().join(" ")).toContain("q3.png"));
    expect(chipOn(chart.id)).toBe("uploading…");
  });

  it("moves a row's chip on the lease frame alone, with no save beside it", async () => {
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER) };
    children = {
      ...children,
      [ROOT]: [{ ...REPORT, live: { state: "uploading" } } as Item, CHARTS],
    };
    mount();
    await waitFor(() => expect(chipOn(REPORT.id)).toBe("uploading…"));
    // The rows paint off the working directory's listing; which lease they
    // live under is one read further, off the chat's folder above it. A frame
    // published before that read lands names a lease the tab does not know yet.
    await waitFor(() => expect(barState()).toBe("live"));

    // The file grew past the live cap on the box: the plane says so and
    // nothing else changes on the drive, so the only frame is the lease's.
    children = {
      ...children,
      [ROOT]: [{ ...REPORT, live: { state: "on_box", box_size: 40_000_000 } } as Item, CHARTS],
    };
    await deliver(leaseFrame(CHAT_NODE));

    await waitFor(() => expect(chipOn(REPORT.id)).toContain("on the machine"));
  });

  it("applies a lease frame that lands before the tab knows its lease, once it does", async () => {
    let answerChatFolder = (): void => undefined;
    chatFolderGate = new Promise<void>((resolve) => {
      answerChatFolder = resolve;
    });
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER) };
    children = {
      ...children,
      [ROOT]: [{ ...REPORT, live: { state: "uploading" } } as Item, CHARTS],
    };
    mount();
    await waitFor(() => expect(chipOn(REPORT.id)).toBe("uploading…"));
    // The chat's folder has been asked for and not answered: the tab cannot
    // yet tell whose lease this listing lives under.
    expect(requestsFor(`/items/${CHAT_NODE}`)).toBeGreaterThan(0);
    expect(barState()).not.toBe("live");

    children = {
      ...children,
      [ROOT]: [{ ...REPORT, live: { state: "on_box", box_size: 40_000_000 } } as Item, CHARTS],
    };
    await deliver(leaseFrame(CHAT_NODE));
    expect(chipOn(REPORT.id)).toBe("uploading…");

    await act(async () => {
      answerChatFolder();
      await Promise.resolve();
    });

    await waitFor(() => expect(chipOn(REPORT.id)).toContain("on the machine"));
    expect(barState()).toBe("live");
  });

  it("drops a held lease frame that turns out to be about another folder", async () => {
    let answerChatFolder = (): void => undefined;
    chatFolderGate = new Promise<void>((resolve) => {
      answerChatFolder = resolve;
    });
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER) };
    mount();
    await screen.findByText("q3-report.html");

    await deliver(leaseFrame("nd_someone_else"));
    await act(async () => {
      answerChatFolder();
      await Promise.resolve();
    });
    await waitFor(() => expect(barState()).toBe("live"));
    const listed = requestsFor(`/items/${ROOT}/children`);

    // Past the coalescing window: a replay would have re-read the listing.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 300));
    });
    expect(requestsFor(`/items/${ROOT}/children`)).toBe(listed);
  });

  it("shows the saved listing once the machine hands the folder back", async () => {
    items = { ...items, [CHAT_NODE]: leased(CHAT_FOLDER) };
    children = {
      ...children,
      [ROOT]: [{ ...REPORT, live: { state: "uploading" } } as Item, CHARTS],
    };
    mount();
    await waitFor(() => expect(chipOn(REPORT.id)).toBe("uploading…"));
    await waitFor(() => expect(barState()).toBe("live"));

    // The hand-back: the lease is gone from the chat's folder, and the
    // plane's rows with it.
    items = { ...items, [CHAT_NODE]: CHAT_FOLDER };
    children = { ...children, [ROOT]: [REPORT, CHARTS] };
    await deliver(leaseFrame(CHAT_NODE));

    await waitFor(() => expect(chipOn(REPORT.id)).toBeNull());
    await waitFor(() => expect(barState()).toBe("persisted"));
    expect(statusBar()).toHaveTextContent(/Saved .+ ago$/);
  });
});
