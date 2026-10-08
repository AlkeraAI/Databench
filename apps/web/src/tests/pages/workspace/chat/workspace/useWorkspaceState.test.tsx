// The wiring between a chat's stored workspace and the store the page renders.
//
// Two rules live in this composition and in no single part of it, and both are
// about whose facts win. Coming back to a chat, the stored document is checked
// against the drive: a tab whose file has gone while the reader was away is not
// reopened. From then on the reader's browser is the authority — a file that
// dies while its tab is open KEEPS its tab, because saying "this file is no
// longer in the chat" and offering to restore it is the tab's job, and the
// answer to this browser's own save is not read back over what the reader did
// while that write was in flight.
//
// Everything runs through the real store, the real save mutation and the real
// file tab with only `fetch` stubbed: each part on its own was already right,
// and the defects these rules answer were defects of the seam between them.

import type { ReactNode } from "react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { act, cleanup, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatWorkspaceDoc } from "@/api/chats";
import { ApiError } from "@/api/errors";
import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import type { Item } from "@/api/files";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";

// The folder browser is stood in as a REGISTRATION: it has its own suite, and
// standing it in keeps this file about the tabs and the document behind them.
vi.mock("@/pages/workspace/chat/workspace/FilesTab", async () => {
  const { registerTabKind } = await import("@/pages/workspace/chat/workspace/tabKinds");
  registerTabKind({
    kind: "files",
    pinned: true,
    singleton: true,
    label: () => "Files",
    Component: () => <div data-testid="files-tab" />,
  });
  return {};
});

import { ChatSidePane } from "@/pages/workspace/chat/workspace/ChatSidePane";
import { useWorkspaceState } from "@/pages/workspace/chat/workspace/useWorkspaceState";
import {
  FILES_TAB_ID,
  flushWorkspace,
  forgetChatPane,
  useWorkspaceStore,
} from "@/pages/workspace/chat/workspace/workspaceStore";

const CHAT = "cht_1";
const DRIVE = "drv_1";
const ROOT = "nd_scratch";
const ROOT_PATH = "/Chats/Q3.alkerachat/scratch";
const REPORT = "nd_report";
const NOTES = "nd_notes";
const EXTRA = "nd_extra";
const REPORT_TAB = "tab-report";
const NOTES_TAB = "tab-notes";
const GRANT_URL = "http://files.localhost:8000/c/bm9uY2U.Y2xhaW0.c2ln";

function item(over: Partial<Item> & { id: string; name: string }): Item {
  return {
    driveId: DRIVE,
    kind: "file",
    nameDisplay: over.name,
    parentId: ROOT,
    pathBytes: `${ROOT_PATH}/${over.name}`,
    path: null,
    etag: "e1",
    attrs: { mtime: "2026-09-01T10:00:00Z" },
    file: { mime_type: "text/plain", size: 21, content_hash: "sha256-test", scan_state: "clean" },
    object: null,
    lease: null,
    live: null,
    stale: false,
    trashed: false,
    capabilities: { can_write: true, can_download: true },
    ...over,
  } as unknown as Item;
}

const ROOT_FOLDER = item({
  id: ROOT,
  name: "scratch",
  kind: "folder",
  parentId: "nd_chat",
  pathBytes: ROOT_PATH,
  file: null,
} as Partial<Item> & { id: string; name: string });

/** The stored document, as the reader left it. */
function layout(activeTabId: string): ChatWorkspaceDoc {
  return {
    tabs: [
      { id: FILES_TAB_ID, kind: "files", name: "Files", params: {} },
      { id: REPORT_TAB, kind: "file", node_id: REPORT, name: "q3-report.md" },
      { id: NOTES_TAB, kind: "file", node_id: NOTES, name: "notes.md" },
    ],
    active_tab_id: activeTabId,
  };
}

/** The document as the route hands it back. The answer to a write is not the
 *  bytes that were written: it is parsed by the model that stores it, so it
 *  carries the version, the bag every persisted model has, and every field the
 *  client left out spelled with its default. */
function stored(doc: ChatWorkspaceDoc): ChatWorkspaceDoc {
  return {
    schema_version: "1.0.0",
    metadata: {},
    tabs: doc.tabs.map((tab) => ({
      schema_version: "1.0.0",
      metadata: {},
      id: tab.id,
      kind: tab.kind,
      node_id: tab.node_id ?? null,
      name: tab.name,
      path: tab.path ?? null,
      params: tab.params ?? {},
    })),
    active_tab_id: doc.active_tab_id ?? null,
  } as ChatWorkspaceDoc;
}

let items: Record<string, Item>;
let missing: Set<string>;
let trashEntries: { trashOpId: string; item: Item }[];
let savedLayout: ChatWorkspaceDoc;
/** Whether the route refuses to hand this reader their layout at all. */
let layoutUnreadable: boolean;
/** Every document written, in order. */
let puts: ChatWorkspaceDoc[];
/** A write the test is holding open, and the hand that lets it finish. */
let held: Promise<void> | null;
let release: () => void;

function holdTheWrite(): void {
  held = new Promise<void>((resolve) => {
    release = () => {
      held = null;
      resolve();
    };
  });
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function stubWire(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : String(input);
      const method = (init?.method ?? "GET").toUpperCase();

      if (/\/chats\/[^/]+\/workspace/.test(url)) {
        if (method !== "PUT") {
          if (layoutUnreadable) return json({ code: "server_error", message: "no" }, 500);
          return json({ state: stored(savedLayout), updated_at: null });
        }
        const doc = (JSON.parse(String(init?.body)) as { state: ChatWorkspaceDoc }).state;
        puts.push(doc);
        savedLayout = doc;
        if (held) await held;
        return json({ state: stored(doc), updated_at: "2026-09-16T10:00:00Z" });
      }
      if (url.startsWith("http://files.localhost")) {
        return new Response("the quarterly numbers", {
          status: 200,
          headers: { "content-type": "text/plain" },
        });
      }
      if (url.includes("/content-grants")) {
        return json({
          url: GRANT_URL,
          expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
          kind: "file",
          etag: "e1",
        });
      }
      if (url.includes("/trash")) return json({ entries: trashEntries, nextMarker: null });
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const id = decodeURIComponent(one[1] ?? "");
        if (missing.has(id)) return json({ code: "files.not_found", message: "no" }, 404);
        const found = items[id];
        return found ? json(found) : json({ code: "files.not_found", message: "no" }, 404);
      }
      return json({ value: [], nextMarker: null });
    }),
  );
}

function mountPane() {
  const client: QueryClient = createQueryClient({ retry: false });
  const rendered = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ChatSidePane chatId={CHAT} driveId={DRIVE} rootNodeId={ROOT} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { client, ...rendered };
}

/** The hook on a client the caller owns, so a test can leave the chat and come
 *  back to the cache the page really has. */
function mountHookOn(client: QueryClient) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(() => useWorkspaceState(CHAT, DRIVE, ROOT_PATH), { wrapper });
}

/** The hook on its own, with the client it seeds so a test can see the answer
 *  to a write land. */
function mountHook() {
  const client: QueryClient = createQueryClient({ retry: false });
  return { client, ...mountHookOn(client) };
}

const nodeFrame = (entityId: string): RealtimeEventFrame =>
  ({
    type: "file_node.changed",
    entity: "file_node",
    entity_id: entityId,
    version: 2,
    org_id: "org_1",
    drive_id: DRIVE,
    parent_id: ROOT,
  }) as RealtimeEventFrame;

async function deliver(frame: RealtimeEventFrame): Promise<void> {
  await act(async () => {
    publishFrame(frame);
    await Promise.resolve();
  });
}

/** The node ids the strip is holding, browser first. */
function openNodes(): (string | null | undefined)[] {
  return (useWorkspaceStore.getState().chats[CHAT]?.tabs ?? [])
    .filter((tab) => tab.kind === "file")
    .map((tab) => tab.node_id);
}

beforeEach(() => {
  items = {
    [ROOT]: ROOT_FOLDER,
    [REPORT]: item({ id: REPORT, name: "q3-report.md" }),
    [NOTES]: item({ id: NOTES, name: "notes.md" }),
    [EXTRA]: item({ id: EXTRA, name: "extra.md" }),
  };
  missing = new Set();
  trashEntries = [];
  savedLayout = layout(FILES_TAB_ID);
  layoutUnreadable = false;
  puts = [];
  held = null;
  release = () => undefined;
  stubWire();
  useWorkspaceStore.setState({ chats: {} });
});

afterEach(() => {
  cleanup();
  forgetChatPane(CHAT);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetFrameBus();
  useWorkspaceStore.setState({ chats: {} });
});

describe("the first thing a reopened chat draws", () => {
  it("is nothing at all until the chat's own document has been read", async () => {
    savedLayout = layout(REPORT_TAB);

    mountPane();

    // The read is still out, and what the store holds meanwhile is the
    // workspace every chat starts from: the folder browser and nothing else.
    // Drawing it would show a reader coming back a workspace with none of
    // their tabs in it and take it back a moment later — and anything sampling
    // the strip on its first paint would read that guess as the truth.
    expect(screen.queryAllByRole("tab")).toEqual([]);

    // When the strip does arrive it is the one the reader left, in front.
    expect(
      await screen.findByRole("tab", { name: /q3-report\.md/, selected: true }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Files" })).toBeInTheDocument();
  });

  it("is the folder browser anyway when the document cannot be read", async () => {
    layoutUnreadable = true;

    mountPane();

    // Waiting for a document that is never coming would cost the reader the
    // file browser too. A refused read is an answer: there is nothing stored to
    // restore, so the workspace every chat starts from is the honest one.
    expect(await screen.findByRole("tab", { name: "Files" })).toBeInTheDocument();
  });
});

describe("coming back to a chat", () => {
  it("does not reopen a tab whose file has gone, and writes the shorter document", async () => {
    missing.add(REPORT);

    mountPane();

    await screen.findByRole("tab", { name: /notes\.md/ });
    await waitFor(() => expect(openNodes()).toEqual([NOTES]));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]?.tabs.map((tab) => tab.node_id)).toEqual([null, NOTES]);
  });
});

describe("a file that goes while its tab is open", () => {
  it("keeps its tab, says so, and offers to put it back", async () => {
    savedLayout = layout(REPORT_TAB);
    mountPane();
    await screen.findByText("the quarterly numbers");

    // Somebody trashes it from another window while the reader is reading it.
    const trashed = item({ id: REPORT, name: "q3-report.md", trashed: true });
    items = { ...items, [REPORT]: trashed };
    trashEntries = [{ trashOpId: "op_7", item: trashed }];
    await deliver(nodeFrame(REPORT));

    expect(await screen.findByText("This file is no longer in the chat")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Restore" })).toBeInTheDocument();
    // The tab is still in the strip — a tab that is gone can say nothing, and
    // there would be nothing left to click Restore in.
    expect(screen.getByRole("tab", { name: /q3-report\.md/ })).toBeInTheDocument();
    expect(openNodes()).toEqual([REPORT, NOTES]);
  });

  it("stops saying so once the file is put back", async () => {
    savedLayout = layout(REPORT_TAB);
    mountPane();
    await screen.findByText("the quarterly numbers");

    const trashed = item({ id: REPORT, name: "q3-report.md", trashed: true });
    items = { ...items, [REPORT]: trashed };
    trashEntries = [{ trashOpId: "op_7", item: trashed }];
    await deliver(nodeFrame(REPORT));
    await waitFor(() =>
      expect(
        within(screen.getByRole("tab", { name: /q3-report\.md/ })).getByLabelText(
          "no longer available",
        ),
      ).toBeInTheDocument(),
    );

    // Restore puts it back — the drive hands the node out again.
    items = { ...items, [REPORT]: item({ id: REPORT, name: "q3-report.md" }) };
    trashEntries = [];
    await deliver(nodeFrame(REPORT));

    await waitFor(() =>
      expect(
        within(screen.getByRole("tab", { name: /q3-report\.md/ })).queryByLabelText(
          "no longer available",
        ),
      ).toBeNull(),
    );
    expect(await screen.findByText("the quarterly numbers")).toBeInTheDocument();
  });

  it("keeps a background tab whose file the drive stops handing back", async () => {
    const { client } = mountPane();
    await screen.findByRole("tab", { name: /q3-report\.md/ });
    // The tab was checked against the drive on the way in, and the file was
    // there.
    await waitFor(() => expect(client.getQueryData(keys.files.item(REPORT))).toBeDefined());

    // The reader is on the folder browser; the file behind one of the tabs
    // behind it is deleted, and the drive is re-read.
    missing.add(REPORT);
    await act(async () => {
      await client.invalidateQueries({ queryKey: keys.files.item(REPORT) });
    });

    await waitFor(() =>
      expect(client.getQueryState(keys.files.item(REPORT))?.error).toBeInstanceOf(ApiError),
    );
    // The tab stays, so going to it says what happened rather than leaving the
    // reader with a strip that quietly lost one — and nothing was written
    // either: the layout is still the one the reader stored. Everything the
    // refusal set off has run by the time React's own queue is empty, so this
    // fails for the mechanism and never for the machine.
    await act(async () => {});
    expect(openNodes()).toEqual([REPORT, NOTES]);
    expect(puts).toEqual([]);
  });
});

describe("the answer to this browser's own save", () => {
  it("leaves a marker raised while the write was in flight", async () => {
    const { client, result } = mountHook();
    await waitFor(() => expect(result.current.ready).toBe(true));
    const store = useWorkspaceStore.getState();

    // A change goes out, and while it is in flight the file behind a tab the
    // reader is not in changes.
    holdTheWrite();
    let written!: Promise<void>;
    act(() => {
      store.setFilesFolder(CHAT, "nd_charts");
      written = flushWorkspace(CHAT);
    });
    await waitFor(() => expect(puts).toHaveLength(1));
    act(() => store.markUpdated(CHAT, REPORT));

    await act(async () => {
      release();
      await written;
    });

    // The answer landed and seeded the layout...
    await waitFor(() =>
      expect(client.getQueryData(keys.chatWorkspace.one(CHAT))).toMatchObject({
        updated_at: "2026-09-16T10:00:00Z",
      }),
    );
    // ...and what only this browser knows survived it.
    expect(result.current.entry.updated).toEqual([REPORT_TAB]);
  });

  it("seeds the layout with the write the chat made on the way out", async () => {
    // The pane is unmounted by ordinary gestures — the Chat/Files toggle under
    // 900 px, or leaving the page — and the write that leaving triggers is the
    // chat's LAST word about its layout. A write that seeds nothing leaves the
    // older document in the cache to be read back over the strip on the way in.
    const client: QueryClient = createQueryClient({ retry: false });
    const first = mountHookOn(client);
    await waitFor(() => expect(first.result.current.ready).toBe(true));

    act(() => useWorkspaceStore.getState().openFileTab(CHAT, { nodeId: EXTRA, name: "extra.md" }));
    first.unmount();

    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]?.tabs.map((tab) => tab.node_id)).toEqual([null, REPORT, NOTES, EXTRA]);
    await waitFor(() =>
      expect(client.getQueryData(keys.chatWorkspace.one(CHAT))).toMatchObject({
        updated_at: "2026-09-16T10:00:00Z",
      }),
    );

    const second = mountHookOn(client);
    await waitFor(() => expect(second.result.current.ready).toBe(true));

    expect(openNodes()).toEqual([REPORT, NOTES, EXTRA]);
  });

  it("does not take back a tab the reader opened while the write was in flight", async () => {
    const { client, result } = mountHook();
    await waitFor(() => expect(result.current.ready).toBe(true));
    const store = useWorkspaceStore.getState();

    holdTheWrite();
    let written!: Promise<void>;
    act(() => {
      store.activate(CHAT, NOTES_TAB);
      written = flushWorkspace(CHAT);
    });
    await waitFor(() => expect(puts).toHaveLength(1));
    act(() => store.openFileTab(CHAT, { nodeId: EXTRA, name: "extra.md" }));

    await act(async () => {
      release();
      await written;
    });
    await waitFor(() =>
      expect(client.getQueryData(keys.chatWorkspace.one(CHAT))).toMatchObject({
        updated_at: "2026-09-16T10:00:00Z",
      }),
    );

    expect(openNodes()).toEqual([REPORT, NOTES, EXTRA]);
    // And the write that follows carries the tab, rather than closing it on the
    // server as well.
    await act(async () => {
      await flushWorkspace(CHAT);
    });
    expect(puts.at(-1)?.tabs.map((tab) => tab.node_id)).toEqual([null, REPORT, NOTES, EXTRA]);
  });
});
