// A file open beside the conversation while the machine is still writing it.
//
// The tab already re-reads its node on the frames that name it. What this file
// pins is what the reader is TOLD about that: a preview drawn from bytes the
// machine has already moved past is a lie the reader acts on, so the tab says
// what is being done to the file, and stops saying it the moment the machine is
// done. The claim is asserted through the real tab with `fetch` stubbed at the
// wire — the state comes back from the server on a frame, not from a mock.

import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import {
  tabKindFor,
  type WorkspaceCtx,
  type WorkspaceTab,
} from "@/pages/workspace/chat/workspace/tabKinds";
import {
  FILES_TAB_ID,
  forgetChatPane,
  useWorkspaceStore,
  type ChatWorkspaceEntry,
} from "@/pages/workspace/chat/workspace/workspaceStore";

// Imported for its registration only; the component comes from the registry.
import "@/pages/workspace/chat/workspace/FileTab";

const DRIVE = "drv_1";
const CHAT_ID = "cht_1";
const ROOT = "nd_root";
const NODE = "nd_report";
const TAB_ID = "tab-report";
const GRANT_URL = "http://files.localhost:8000/c/bm9uY2U.Y2xhaW0.c2ln";

const CTX: WorkspaceCtx = { chatId: CHAT_ID, driveId: DRIVE, rootNodeId: ROOT };

function item(overrides: Partial<Item> & { id: string; name: string }): Item {
  return {
    driveId: DRIVE,
    kind: "file",
    nameDisplay: overrides.name,
    pathBytes: "",
    parentId: ROOT,
    path: null,
    etag: "e1",
    attrs: { mtime: "2026-09-01T10:00:00Z" },
    file: null,
    object: null,
    lease: null,
    live: null,
    stale: false,
    trashed: false,
    capabilities: { can_write: true, can_download: true },
    ...overrides,
  } as unknown as Item;
}

const ROOT_FOLDER = item({
  id: ROOT,
  name: "scratch",
  kind: "folder",
  parentId: "nd_chat",
  pathBytes: "/home/dana/Chats/Q3.alkerachat/scratch",
});

function textFile(over: Partial<Item> = {}): Item {
  return item({
    id: NODE,
    name: "notes.txt",
    pathBytes: "/home/dana/Chats/Q3.alkerachat/scratch/notes.txt",
    file: { mime_type: "text/plain", size: 2048, content_hash: "sha256-test", scan_state: "clean" },
    ...over,
  } as Partial<Item> & { id: string; name: string });
}

let calls: string[] = [];
let items: Record<string, Item>;
let served: string;
/** What the grant route answers instead of a grant, while the drive is still
 *  bringing the bytes from the machine. */
let grantRefusal: { body: unknown; retryAfter?: string } | null = null;
/** What the grant route says beside the URL about where the bytes stand. */
let grantState: Record<string, unknown> = {};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function stubWire(): void {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      calls.push(url);
      if (url.startsWith("http://files.localhost")) {
        return Promise.resolve(
          new Response(served, { status: 200, headers: { "content-type": "text/plain" } }),
        );
      }
      if (url.includes("/content-grants") && grantRefusal !== null) {
        const headers: Record<string, string> = { "content-type": "application/json" };
        if (grantRefusal.retryAfter) headers["retry-after"] = grantRefusal.retryAfter;
        return Promise.resolve(
          new Response(JSON.stringify(grantRefusal.body), { status: 409, headers }),
        );
      }
      if (url.includes("/content-grants")) {
        return Promise.resolve(
          json({
            url: GRANT_URL,
            expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
            kind: "file",
            etag: "e1",
            ...grantState,
          }),
        );
      }
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const found = items[decodeURIComponent(one[1] ?? "")];
        if (!found) return Promise.resolve(json({ code: "files.not_found" }, 404));
        return Promise.resolve(json(found));
      }
      return Promise.resolve(json({ value: [], nextMarker: null }));
    }),
  );
}

function seedStore(): void {
  useWorkspaceStore.getState().hydrate(CHAT_ID, {
    tabs: [
      { id: FILES_TAB_ID, kind: "files", name: "Files", params: {} },
      { id: TAB_ID, kind: "file", node_id: NODE, name: "notes.txt", params: {} },
    ],
    active_tab_id: TAB_ID,
  });
}

function mount(tab: Partial<WorkspaceTab> = {}) {
  const kind = tabKindFor("file");
  if (kind === undefined) throw new Error("the `file` tab kind was never registered");
  const Component = kind.Component;
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Component
          tab={{ id: TAB_ID, kind: "file", node_id: NODE, name: "notes.txt", ...tab }}
          ctx={CTX}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const nodeFrame = (entityId: string): RealtimeEventFrame =>
  ({
    type: "file_node.changed",
    entity: "file_node",
    entity_id: entityId,
    version: 2,
    org_id: "org_1",
    drive_id: DRIVE,
  }) as RealtimeEventFrame;

/** The file as the server now has it, then the frame that says so. */
async function fileChanged(next: Partial<Item>): Promise<void> {
  items = { ...items, [NODE]: { ...(items[NODE] as Item), ...next } as Item };
  await act(async () => {
    publishFrame(nodeFrame(NODE));
    await Promise.resolve();
  });
}

function chip(): string | null {
  return document.querySelector(".alk-files-live-chip")?.textContent ?? null;
}

beforeEach(() => {
  items = { [ROOT]: ROOT_FOLDER, [NODE]: textFile() };
  served = "the quarterly numbers";
  grantRefusal = null;
  grantState = {};
  stubWire();
  seedStore();
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetFrameBus();
  forgetChatPane(CHAT_ID);
  useWorkspaceStore.setState({ chats: {} });
});

describe("a file the machine is still working on", () => {
  it("says nothing about the machine for a file at rest", async () => {
    mount();
    await screen.findByText("the quarterly numbers");
    expect(chip()).toBeNull();
  });

  it("says the machine is writing it, and stops saying so when it finishes", async () => {
    items = { ...items, [NODE]: textFile({ live: { state: "writing" } } as Partial<Item>) };
    mount();
    await screen.findByText("the quarterly numbers");
    await waitFor(() => expect(chip()).toBe("writing…"));

    // The write lands: the file is the drive's again, and the tab must stop
    // implying its bytes are moving.
    await fileChanged({ live: null, etag: "e2" });
    await waitFor(() => expect(chip()).toBeNull());
  });

  it("names the size of a file the machine is holding on the box", async () => {
    items = {
      ...items,
      [NODE]: textFile({ live: { state: "on_box", box_size: 2_000_000 } } as Partial<Item>),
    };
    mount();
    await screen.findByText("the quarterly numbers");

    // The bytes on screen are the drive's copy; the machine has a newer one it
    // has not sent. Saying how big it is is what makes that legible.
    await waitFor(() => expect(chip()).toContain("on the machine"));
    expect(chip()).toContain("2");
  });

  it("shows nothing for a state this build has no sentence for", async () => {
    // Deliberately a state this build's types do not know: it is what a newer
    // server would put on the wire, so it is built the way the wire delivers it.
    items = {
      ...items,
      [NODE]: { ...textFile(), live: { state: "teleporting" } } as unknown as Item,
    };
    mount();
    await screen.findByText("the quarterly numbers");
    // A newer server's spelling must never reach a reader raw.
    expect(chip()).toBeNull();
    expect(screen.queryByText(/teleporting/)).toBeNull();
  });

  it("redraws the preview and the chip together when the machine rewrites the file", async () => {
    items = { ...items, [NODE]: textFile({ live: { state: "writing" } } as Partial<Item>) };
    mount();
    await screen.findByText("the quarterly numbers");

    served = "the revised numbers";
    await fileChanged({ etag: "e2", live: { state: "uploading" } } as Partial<Item>);

    // The moved etag buys the new bytes; the same read is what moves the chip.
    await screen.findByText("the revised numbers");
    await waitFor(() => expect(chip()).toBe("uploading…"));
    expect(calls.filter((url) => url.includes("/content-grants")).length).toBe(2);
  });
});

// The lease over an open file is on the chat's folder, and the plane's word
// about the file — writing, uploading, left on the machine, settled — is
// announced by THAT node's frame, not the file's own. A tab that only listened
// for its own node kept the old bytes and the old chip until the next save
// happened to land beside it.
const CHAT_NODE = "nd_chat";
const CHAT_FOLDER = item({
  id: CHAT_NODE,
  name: "Q3.alkerachat",
  kind: "folder",
  parentId: "nd_home",
  pathBytes: "/home/dana/Chats/Q3.alkerachat",
  object: {
    id: "obj_chat_1",
    type: "chat",
    title: "Q3 review",
    web_url: "/chat/cht_1",
    metadata: { files_node_id: ROOT },
  } as Item["object"],
});

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

describe("what marks a tab as updated", () => {
  const marked = (): string[] => useWorkspaceStore.getState().chats[CHAT_ID]?.updated ?? [];

  it("a node change that leaves the bytes alone does not mark the tab", async () => {
    // A share, a rename, a move or a trash moves the etag and not the content
    // tag; a dot for it read as an edit that never happened.
    items = { ...items, [NODE]: textFile({ etag: "e1", ctag: "c1" } as Partial<Item>) };
    mount();
    await screen.findByText("the quarterly numbers");
    // The reader moves to another tab: the file tab stays mounted and keeps
    // watching its node, and a change is now something to mark.
    act(() => {
      useWorkspaceStore.setState((state) => {
        const entry = state.chats[CHAT_ID] as ChatWorkspaceEntry;
        return {
          chats: {
            ...state.chats,
            [CHAT_ID]: {
              ...entry,
              groups: entry.groups.map((group) => ({ ...group, activeTabId: FILES_TAB_ID })),
              activeTabId: FILES_TAB_ID,
            },
          },
        };
      });
    });

    await fileChanged({ etag: "e2", ctag: "c1" } as Partial<Item>);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(marked()).toEqual([]);

    await fileChanged({ etag: "e3", ctag: "c2" } as Partial<Item>);
    await waitFor(() => expect(marked()).toContain(TAB_ID));
  });
});

describe("a file open while the machine holds the folder", () => {
  it("re-reads the file on the folder's lease frame and shows the agent's new bytes", async () => {
    items = { ...items, [CHAT_NODE]: CHAT_FOLDER };
    mount();
    await screen.findByText("the quarterly numbers");

    // The agent rewrote the file; the plane reports it left on the machine
    // (past the live cap) with the bytes the drive holds moved on. The only
    // frame is the lease's — nothing named the file itself.
    served = "the revised numbers";
    items = {
      ...items,
      [NODE]: textFile({
        etag: "e2",
        live: { state: "on_box", box_size: 40_000_000 },
      } as Partial<Item>),
    };
    await act(async () => {
      publishFrame(leaseFrame(CHAT_NODE));
      await Promise.resolve();
    });

    await screen.findByText("the revised numbers");
    await waitFor(() => expect(chip()).toContain("on the machine"));
    expect(calls.filter((url) => url.includes("/content-grants")).length).toBe(2);
  });
});

// Rows are listed before their bytes, so a reader can open a file the drive
// has only heard of. The tab says where the bytes are coming from, and draws
// them once they land — on the save frame, with no reload.
describe("a file whose bytes have not landed", () => {
  const holding = {
    holder: "807bf89a-464c-4867-8b08-6e020a9bd8a3",
    machine: "807bf89a-464c-4867-8b08-6e020a9bd8a3",
    machine_name: "demo-box",
    live: true,
    served: "live",
    expires_at: "2100-01-01T00:00:00Z",
  };

  it("says it is fetching from the machine, then shows the bytes when they land", async () => {
    items = {
      ...items,
      [CHAT_NODE]: CHAT_FOLDER,
      [NODE]: textFile({
        lease: holding,
        live: { state: "writing", content: "unlanded", holder_size: 21 },
        file: { mime_type: "text/plain", size: 0, content_hash: "", scan_state: "clean" },
      } as unknown as Partial<Item>),
    };
    // The drive asked the machine and is still waiting on the upload.
    grantRefusal = {
      body: {
        code: "files.live_pending",
        message: "not written back yet",
        detail: { holder: "demo-box", outcome: "accepted", landing: null },
      },
    };
    mount();

    await screen.findByText("Fetching from demo-box…");
    // Opening the file is what asks for it — once, and the content origin is
    // not read for bytes that are not there.
    await waitFor(() =>
      expect(calls.filter((url) => url.includes("/content-grants"))).toHaveLength(1),
    );
    expect(calls.filter((url) => url.startsWith("http://files.localhost"))).toHaveLength(0);

    grantRefusal = null;
    await fileChanged({
      etag: "e2",
      live: null,
      file: {
        mime_type: "text/plain",
        size: 21,
        content_hash: "sha256-landed",
        scan_state: "clean",
      },
    } as unknown as Partial<Item>);

    await screen.findByText("the quarterly numbers");
    expect(screen.queryByText("Fetching from demo-box…")).toBeNull();
  });

  it("says the copy is older than the machine's, until the newer bytes land", async () => {
    items = {
      ...items,
      [CHAT_NODE]: CHAT_FOLDER,
      [NODE]: textFile({
        lease: holding,
        live: { state: "writing", content: "behind", holder_size: 21 },
      } as unknown as Partial<Item>),
    };
    grantState = { contentState: "behind", asOf: null };
    mount();

    await screen.findByText("the quarterly numbers");
    const line = await screen.findByRole("note");
    expect(line).toHaveTextContent("Showing an older copy; demo-box has a newer one");

    grantState = { contentState: "on_drive", asOf: null };
    served = "the revised numbers";
    await fileChanged({ etag: "e2", live: null } as unknown as Partial<Item>);

    await screen.findByText("the revised numbers");
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("says the machine is offline when the drive could not reach it", async () => {
    items = {
      ...items,
      [CHAT_NODE]: CHAT_FOLDER,
      [NODE]: textFile({
        lease: { ...holding, served: "offline" },
        live: { state: "writing", content: "unlanded", holder_size: 21 },
        file: { mime_type: "text/plain", size: 0, content_hash: "", scan_state: "clean" },
      } as unknown as Partial<Item>),
    };
    grantRefusal = {
      body: {
        code: "files.live_pending",
        message: "not written back yet",
        detail: { holder: "demo-box", outcome: "offline", landing: null },
      },
    };
    mount();

    await screen.findByText("Demo-box is offline · showing nothing yet");
    expect(screen.queryByText("Fetching from demo-box…")).toBeNull();
  });
});
