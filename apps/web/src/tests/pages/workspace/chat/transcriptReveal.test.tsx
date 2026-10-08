// A file named in the transcript is a door onto the file itself.
//
// The agent is told to name what it produced as `[Q3 report](q3-report.html)`,
// and the reader's answer to "which file is that?" has to be the file — not a
// new browser window, not a guess. So this drives the REAL chat surface beside
// the REAL workspace pane, over a wire stub: clicking the reference walks the
// Files tab to the folder the file sits in, selects and scrolls its row, and
// opens the file in a tab of its own, with the browser left in front because
// the highlight is what was asked for.
//
// The two negatives matter as much: a reference to a file that has since been
// deleted is plain text with a note rather than a door that goes nowhere, and a
// shell that cannot look a path up — the VS Code webview, which only hands the
// path to its host — is left exactly as it was.

import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import { useRealtimeStatus } from "@/api/events/status";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { CHAT_FOLDER_COALESCE_MS } from "@/pages/workspace/chat/useChatFolderChanges";

const CHAT_ID = "cht_1";
const DRIVE = "drv_1";
/** The chat's working directory — the Files tab's root. */
const ROOT = "nd_root";
const SUB = "nd_charts";

const { ds, located } = vi.hoisted(() => {
  const located: Record<string, unknown> = {
    "q3-report.html": {
      nodeId: "nd_report",
      driveId: "drv_1",
      parentId: "nd_root",
      name: "q3-report.html",
      path: "q3-report.html",
      kind: "file",
    },
    "charts/q3.png": {
      nodeId: "nd_png",
      driveId: "drv_1",
      parentId: "nd_charts",
      name: "q3.png",
      path: "charts/q3.png",
      kind: "file",
    },
  };
  return {
    located,
    ds: {
      listChats: async () => [{ id: "cht_1", title: "Q3 review", updatedAt: "2026-09-06T12:00:00Z" }],
      listModels: async () => [],
      resolveChatDefaults: async () => ({ model: null, effort: null }),
      listCommands: async () => [],
      listContext: async () => ({ total: 0 }),
      lineageRoots: async () => ({}),
      getChatTurns: async () => [
        {
          id: "t1",
          author: "assistant",
          parts: [
            {
              id: "p1",
              kind: "text",
              text:
                "Done: [Q3 report](q3-report.html), [The plot](charts/q3.png)" +
                " and [Last quarter](old.html)",
            },
          ],
        },
      ],
      searchFiles: async () => [],
      sendUserMessage: vi.fn(),
      createChat: vi.fn(),
      getPermissionMode: async () => "read_only",
      setPermissionMode: async () => {},
      subscribePermissionMode: () => () => {},
      subscribeChat: () => () => {},
      getChatActivity: () => ({}),
      getSubagentChats: () => [],
      getSubagentLabels: () => ({}),
      chatFiles: {
        upload: vi.fn(),
        resolveUrl: vi.fn(async () => null),
        /** Present on the portal's port: it can walk the chat's folder. */
        locate: vi.fn(async (_chatId: string, path: string) => located[path] ?? null),
        openPath: vi.fn(),
      } as Record<string, unknown>,
    },
  };
});

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const host = real.createBrowserChatHost({
    account: () => ({ email: "analyst@tideline.example", webAppUrl: null }),
  });
  return {
    ...real,
    chatHost: () => host,
    chatData: () => ds,
    refetchWhileErrored: () => false as const,
    refetchWhileNoChatDefault: () => false as const,
    refetchWhileErroredOrEmpty: () => false as const,
    chatCaps: () => ({
      opencodeActive: false,
      modelCatalog: true,
      permissionModes: ["read_only", "default", "plan"],
      fixedPermissionMode: "read_only",
    }),
  };
});

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";
import { ChatSidePane } from "@/pages/workspace/chat/workspace/ChatSidePane";
import { useWorkspaceStore } from "@/pages/workspace/chat/workspace/workspaceStore";

function item(overrides: Partial<Item> & { id: string; name: string }): Item {
  return {
    driveId: DRIVE,
    kind: "file",
    subtype: null,
    nameDisplay: overrides.name,
    pathBytes: `/Chats/c.alkerachat/scratch/${overrides.name}`,
    parentId: ROOT,
    path: null,
    etag: "e1",
    ctag: "c1",
    attrs: null,
    file: null,
    symlink: null,
    object: null,
    lease: null,
    stale: false,
    trust: null,
    locked: false,
    held: false,
    capabilities: { can_write: true, can_read: true },
    shared: false,
    trashed: false,
    ...overrides,
  } as unknown as Item;
}

const ROOT_FOLDER = item({
  id: ROOT,
  name: "scratch",
  kind: "folder",
  parentId: "nd_chat",
  pathBytes: "/Chats/c.alkerachat/scratch",
});
const CHARTS = item({ id: SUB, name: "charts", kind: "folder" });
/** The chat's own folder: `scratch` is the working directory it names. */
const CHAT_FOLDER = item({
  id: "nd_chat",
  name: "c.alkerachat",
  kind: "folder",
  parentId: "nd_chats",
  pathBytes: "/Chats/c.alkerachat",
  object: {
    type: "chat",
    id: "ob_chat",
    title: "Q3 review",
    web_url: `/chat/${CHAT_ID}`,
    metadata: { files_node_id: ROOT },
  } as Item["object"],
});
const REPORT = item({ id: "nd_report", name: "q3-report.html", file: { size: 900 } as Item["file"] });
const PLOT = item({
  id: "nd_png",
  name: "q3.png",
  parentId: SUB,
  pathBytes: "/Chats/c.alkerachat/scratch/charts/q3.png",
  file: { size: 20 } as Item["file"],
});

let items: Record<string, Item>;
let children: Record<string, Item[]>;
/** Every URL the surface asked for, in order. */
let calls: string[];

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );
}

function stubWire(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      calls.push(url);
      if (/\/chats\/[^/]+\/workspace/.test(url)) {
        return json({
          state: { tabs: [{ id: "files", kind: "files", name: "Files", params: {} }], active_tab_id: "files" },
          updated_at: null,
        });
      }
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return json({ id: DRIVE, orgId: "org_1", rootId: "nd_drive", quotaBytes: 0 });
      }
      if (/\/chats\/[^/?]+(\?|$)/.test(url)) {
        return json({ id: CHAT_ID, title: "Q3 review", files_node_id: "nd_chat" });
      }
      const listing = /\/items\/([^/?]+)\/children/.exec(url);
      if (listing) return json({ value: children[decodeURIComponent(listing[1] ?? "")] ?? [], nextMarker: null });
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const found = items[decodeURIComponent(one[1] ?? "")];
        return found ? json(found) : json({ code: "files.not_found", message: "no" }, 404);
      }
      return json({ value: [], nextMarker: null });
    }),
  );
}

/** Both halves of the chat page, as the page composes them: the transcript and
 *  the workspace pane beside it, over one cache. */
function mount(): void {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[`/chat/${CHAT_ID}`]}>
        <Routes>
          <Route
            path="/chat/:chatId"
            element={
              <>
                <ChatSurface chatId={CHAT_ID} />
                <ChatSidePane chatId={CHAT_ID} driveId={DRIVE} rootNodeId={ROOT} />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** `scrollIntoView` is not implemented in jsdom; this supplies it and records
 *  which row asked for it. */
function watchScroll(): { rows: string[]; restore: () => void } {
  const rows: string[] = [];
  const original = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = function record(this: Element) {
    rows.push(this.getAttribute("data-row-id") ?? "");
  };
  return {
    rows,
    restore: () => {
      Element.prototype.scrollIntoView = original;
    },
  };
}

function selectedIds(): string[] {
  return Array.from(document.querySelectorAll('[data-row-id][aria-selected="true"]')).map(
    (element) => element.getAttribute("data-row-id") ?? "",
  );
}

function openTabNames(): string[] {
  return Array.from(document.querySelectorAll('[role="tab"]')).map((tab) => tab.textContent ?? "");
}

let scroll: { rows: string[]; restore: () => void };
let openWindow: ReturnType<typeof vi.fn>;

beforeEach(() => {
  items = {
    [ROOT]: ROOT_FOLDER,
    [SUB]: CHARTS,
    [CHAT_FOLDER.id]: CHAT_FOLDER,
    [REPORT.id]: REPORT,
    [PLOT.id]: PLOT,
  };
  children = { [ROOT]: [REPORT, CHARTS], [SUB]: [PLOT] };
  calls = [];
  delete located["old.html"];
  stubWire();
  openWindow = vi.fn();
  vi.stubGlobal("open", openWindow);
  scroll = watchScroll();
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "connected" });
});

afterEach(() => {
  cleanup();
  scroll.restore();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  resetFrameBus();
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "idle" });
});

/** One node in the chat's working directory changed, as the stream delivers it. */
const nodeFrame = (parentId: string): RealtimeEventFrame =>
  ({
    type: "file_node.changed",
    entity: "file_node",
    entity_id: "nd_old",
    version: 1,
    org_id: "org_1",
    drive_id: DRIVE,
    parent_id: parentId,
  }) as RealtimeEventFrame;

/** The frame a MACHINE's work in its own folder always raises. The folder is
 *  mounted to the box under a lease, and the live plane announces itself as one
 *  frame for the whole leased subtree (`emit_lease_changed` in
 *  `alkera_core/files/lease_live.py`). Landing NEW bytes also raises a node
 *  frame carrying the parent; a re-save of identical bytes, a rename, a new
 *  folder and a delete do not, so this is the only signal that is always
 *  there. */
const leaseFrame = (leasedNodeId: string): RealtimeEventFrame =>
  ({
    type: "file_lease.changed",
    entity: "file_lease",
    entity_id: leasedNodeId,
    version: 1,
    org_id: "org_1",
    drive_id: DRIVE,
    lease_node_id: leasedNodeId,
    live_seq: 7,
  }) as RealtimeEventFrame;

/** The assistant's block, read once it has stopped moving — the same wait for
 *  the reader who watched (whose re-ask is behind the folder's coalesce window)
 *  and the reader who reloaded (whose first lookup is a round trip). Waiting on
 *  stillness rather than on a word keeps the wait from deciding the answer. */
async function settledBlock(): Promise<string> {
  const read = (): string =>
    screen.getByText(/^Done: /).closest(".chat-block")?.textContent ?? "";
  // Stillness has to outlast the folder's own coalesce window, or the wait ends
  // inside it and calls the block settled before the re-ask has even started.
  const enough = Math.ceil((CHAT_FOLDER_COALESCE_MS * 2) / 25);
  let last = "";
  let still = 0;
  for (let tick = 0; tick < 120 && still < enough; tick += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 25));
    });
    const now = read();
    still = now === last ? still + 1 : 0;
    last = now;
  }
  return last;
}

async function deliver(frame: RealtimeEventFrame): Promise<void> {
  await act(async () => {
    publishFrame(frame);
    await Promise.resolve();
  });
}

/** The surface can only answer a frame once it has read which folder the chat
 *  keeps its files in. */
async function watchingTheFolder(): Promise<void> {
  await waitFor(() => expect(calls.some((url) => url.includes("/items/nd_chat"))).toBe(true));
}

describe("a file named in the transcript", () => {
  it("says which file it is before it is clicked", async () => {
    mount();
    const link = await screen.findByRole("button", { name: "Q3 report" });
    await waitFor(() =>
      expect(link.getAttribute("title")).toBe("q3-report.html (in the chat's folder)"),
    );
  });

  it("selects and scrolls its row in the Files tab, opens it in a tab, and opens no window", async () => {
    mount();
    const link = await screen.findByRole("button", { name: "Q3 report" });
    await waitFor(() => expect(link.getAttribute("title")).toContain("q3-report.html"));

    fireEvent.click(link);

    await waitFor(() => expect(selectedIds()).toEqual(["nd_report"]));
    expect(scroll.rows).toContain("nd_report");
    // The file is open in a tab of its own, and the browser is what the reader
    // is left looking at — the highlight is the answer to the click.
    await waitFor(() => expect(openTabNames()).toContain("q3-report.html"));
    expect(useWorkspaceStore.getState().chats[CHAT_ID]?.activeTabId).toBe("files");
    expect(openWindow).not.toHaveBeenCalled();
    expect(ds.chatFiles.openPath).not.toHaveBeenCalled();
  });

  it("goes back to the tab the file is already open in, rather than opening a second one", async () => {
    mount();
    const plot = await screen.findByRole("button", { name: "The plot" });
    await waitFor(() => expect(plot.getAttribute("title")).toContain("q3.png"));

    // The reader opens the plot, then the report: two tabs, and the browser
    // standing in the report's folder.
    fireEvent.click(plot);
    await waitFor(() => expect(openTabNames()).toContain("q3.png"));
    const report = await screen.findByRole("button", { name: "Q3 report" });
    fireEvent.click(report);
    await waitFor(() => expect(openTabNames()).toContain("q3-report.html"));
    const strip = useWorkspaceStore.getState().chats[CHAT_ID];
    const plotTab = strip?.tabs.find((tab) => tab.node_id === "nd_png");
    const tabCount = strip?.tabs.length ?? 0;

    // Clicking the plot again is a jump to its tab.
    fireEvent.click(plot);

    await waitFor(() =>
      expect(useWorkspaceStore.getState().chats[CHAT_ID]?.activeTabId).toBe(plotTab?.id),
    );
    const after = useWorkspaceStore.getState().chats[CHAT_ID];
    expect(after?.tabs).toHaveLength(tabCount);
    // The browser did not walk back into the subfolder, and no window opened.
    expect(after?.tabs.find((tab) => tab.id === "files")?.params).toEqual({ folderId: ROOT });
    expect(openWindow).not.toHaveBeenCalled();
    expect(ds.chatFiles.openPath).not.toHaveBeenCalled();
  });

  it("walks the browser into the subfolder a nested file sits in", async () => {
    mount();
    const link = await screen.findByRole("button", { name: "The plot" });
    await waitFor(() => expect(link.getAttribute("title")).toBe("q3.png (in charts)"));

    fireEvent.click(link);

    await waitFor(() => expect(selectedIds()).toEqual(["nd_png"]));
    expect(scroll.rows).toContain("nd_png");
    // The browser is standing in the subfolder, not the root it started in.
    expect(
      useWorkspaceStore.getState().chats[CHAT_ID]?.tabs.find((tab) => tab.id === "files")?.params,
    ).toEqual({ folderId: SUB });
  });
});

describe("a file the transcript names that has never been there", () => {
  it("renders as plain text saying it is not in the chat, and opens nothing", async () => {
    mount();
    await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
    const gone = screen.getByText("Last quarter");
    expect(gone.tagName).toBe("SPAN");
    expect(screen.queryByRole("button", { name: "Last quarter" })).toBeNull();

    fireEvent.click(gone);

    expect(selectedIds()).toEqual([]);
    expect(openWindow).not.toHaveBeenCalled();
    expect(ds.chatFiles.openPath).not.toHaveBeenCalled();
  });
});

describe("a file the transcript named that has since been deleted", () => {
  it("says it is not in the chat any more, while one that never came is not in the chat", async () => {
    const report = located["q3-report.html"];
    mount();
    const link = await screen.findByRole("button", { name: "Q3 report" });
    await waitFor(() =>
      expect(link.getAttribute("title")).toBe("q3-report.html (in the chat's folder)"),
    );
    await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
    await watchingTheFolder();

    delete located["q3-report.html"];
    try {
      await deliver(nodeFrame(ROOT));
      await waitFor(() => expect(screen.getByText("not in the chat any more")).toBeTruthy());
      expect(screen.queryByRole("button", { name: "Q3 report" })).toBeNull();
      expect(screen.getByText("Q3 report").tagName).toBe("SPAN");
      // The one that never came is still told apart from the one deleted.
      expect(screen.getAllByText("not in the chat")).toHaveLength(1);
    } finally {
      located["q3-report.html"] = report;
    }
  });
});

describe("a shell whose port cannot look a path up", () => {
  it("keeps today's button and its own door", async () => {
    // The VS Code webview's port: it hands the path to its host. Nothing about
    // the reference may change for it.
    const locate = ds.chatFiles["locate"];
    delete ds.chatFiles["locate"];
    try {
      mount();
      const link = await screen.findByRole("button", { name: "Q3 report" });
      expect(link.getAttribute("title")).toBe("q3-report.html");
      fireEvent.click(link);
      expect(ds.chatFiles.openPath).toHaveBeenCalledWith(CHAT_ID, "q3-report.html");
      expect(screen.queryByText("not in the chat")).toBeNull();
      expect(selectedIds()).toEqual([]);
    } finally {
      ds.chatFiles["locate"] = locate;
    }
  });
});

describe("a reference the machine has not written the file for yet", () => {
  it("stops saying the file is not in the chat once the drive says it is there", async () => {
    mount();
    await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "Last quarter" })).toBeNull();
    await watchingTheFolder();

    // The turn lands. Each path is looked up once per transcript, so without
    // something dropping that answer the reader is told the file is gone for
    // as long as the chat stays open.
    located["old.html"] = {
      nodeId: "nd_old",
      driveId: DRIVE,
      parentId: ROOT,
      name: "old.html",
      path: "old.html",
      kind: "file",
    };
    await deliver(nodeFrame(ROOT));

    const link = await screen.findByRole("button", { name: "Last quarter" });
    await waitFor(() =>
      expect(link.getAttribute("title")).toBe("old.html (in the chat's folder)"),
    );
    expect(screen.queryByText("not in the chat")).toBeNull();
  });

  it("asks again for nothing when the change was elsewhere in the drive", async () => {
    mount();
    await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
    await watchingTheFolder();
    const lookups = ds.chatFiles.locate as unknown as { mock: { calls: unknown[] } };
    const asked = lookups.mock.calls.length;

    // Another reader's folder, in the same drive: nothing the transcript names
    // can have appeared there.
    await deliver(nodeFrame("nd_somebody_elses_folder"));

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(lookups.mock.calls.length).toBe(asked);
    expect(screen.getByText("not in the chat")).toBeTruthy();
  });

  // The file the agent writes lands in the chat's working directory, which is
  // LEASED to its box, and a great many of the things the box does there raise
  // only the lease's own frame: a re-save of identical bytes, a file that
  // reaches its name by rename, a folder appearing, a delete, and every live
  // state in between. A transcript listening for node frames alone never learns
  // those happened, and goes on saying a file the drive now holds "has not
  // arrived" until the reader reloads.
  //
  // Both spellings of the leased node are driven, because the holder leases the
  // chat's FOLDER and steps down to the working directory inside it: which of
  // the two the frame names is the server's business, and neither may be the
  // one the reader's answer depends on.
  for (const [where, leased] of [
    ["the chat's folder", "nd_chat"],
    ["the working directory", ROOT],
  ] as const) {
    it(`stops saying the file is not in the chat when a lease on ${where} says it moved`, async () => {
      mount();
      await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
      await watchingTheFolder();

      located["old.html"] = {
        nodeId: "nd_old",
        driveId: DRIVE,
        parentId: ROOT,
        name: "old.html",
        path: "old.html",
        kind: "file",
      };
      await deliver(leaseFrame(leased));

      const link = await screen.findByRole("button", { name: "Last quarter" });
      await waitFor(() =>
        expect(link.getAttribute("title")).toBe("old.html (in the chat's folder)"),
      );
      expect(screen.queryByText("not in the chat")).toBeNull();
    });
  }

  it("reads the same to the reader who watched as to the reader who reloads", async () => {
    mount();
    await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
    await watchingTheFolder();

    located["old.html"] = {
      nodeId: "nd_old",
      driveId: DRIVE,
      parentId: ROOT,
      name: "old.html",
      path: "old.html",
      kind: "file",
    };
    await deliver(leaseFrame(ROOT));
    const watched = await settledBlock();

    // The same chat, opened fresh against the same drive — a reload.
    cleanup();
    mount();
    const reloaded = await settledBlock();

    expect(watched).toBe(reloaded);
    expect(watched).not.toContain("not in the chat");
  });

  it("asks again for nothing when somebody else's lease moved", async () => {
    mount();
    await waitFor(() => expect(screen.getByText("not in the chat")).toBeTruthy());
    await watchingTheFolder();
    const lookups = ds.chatFiles.locate as unknown as { mock: { calls: unknown[] } };
    const asked = lookups.mock.calls.length;

    await deliver(leaseFrame("nd_somebody_elses_lease"));

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(lookups.mock.calls.length).toBe(asked);
    expect(screen.getByText("not in the chat")).toBeTruthy();
  });
});
