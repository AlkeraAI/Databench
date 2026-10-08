// @vitest-environment jsdom
//
// A file the machine rewrites while the reader is looking at something else.
//
// Only the tab in front is mounted — a workspace can hold dozens, and a
// background tab holding a frame, a blob URL and a live grant for a file nobody
// is reading would cost the reader memory and the drive requests for nothing.
// The consequence is that a background tab has no hooks of its own, so the
// thing that notices its file moved has to be the pane around it.
//
// What is pinned here is exactly that: the strip marks a background tab whose
// node the drive says changed, marks nothing for the tab already in front (a
// change the reader is watching happen needs no announcement), and marks
// nothing for a node no tab is holding or for a frame that is not about a
// node's bytes at all.

import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";

// The two tab kinds, stood in as REGISTRATIONS: the pane learns what a tab is
// from the modules it imports, and the panels themselves have their own suites.
// Standing them in is what makes this file about the pane and nothing else.
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
vi.mock("@/pages/workspace/chat/workspace/FileTab", async () => {
  const { registerTabKind } = await import("@/pages/workspace/chat/workspace/tabKinds");
  registerTabKind({
    kind: "file",
    label: (tab: { name: string }) => tab.name,
    Component: ({ tab }: { tab: { name: string } }) => <div data-testid="file-tab">{tab.name}</div>,
  });
  return {};
});

import { ChatSidePane } from "@/pages/workspace/chat/workspace/ChatSidePane";
import { useWorkspaceStore } from "@/pages/workspace/chat/workspace/workspaceStore";

const CHAT_ID = "cht_1";
const DRIVE = "drv_1";
const ROOT = "nd_scratch";
const REPORT = "nd_report";
const NOTES = "nd_notes";
const STRANGER = "nd_stranger";

function item(over: Partial<Item> & { id: string; name: string }): Item {
  return {
    driveId: DRIVE,
    kind: "file",
    nameDisplay: over.name,
    parentId: ROOT,
    pathBytes: `/Chats/c.alkerachat/scratch/${over.name}`,
    path: null,
    etag: "e1",
    ctag: "c1",
    attrs: { mtime: "2026-09-01T10:00:00Z" },
    file: { mime_type: "text/plain", size: 10, content_hash: "sha256-test", scan_state: "clean" },
    object: null,
    lease: null,
    live: null,
    stale: false,
    trashed: false,
    capabilities: { can_read: true, can_write: true },
    ...over,
  } as unknown as Item;
}

const ROOT_FOLDER = item({
  id: ROOT,
  name: "scratch",
  kind: "folder",
  parentId: "nd_chat",
  pathBytes: "/Chats/c.alkerachat/scratch",
  file: null,
} as Partial<Item> & { id: string; name: string });

let items: Record<string, Item>;

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );
}

function stubWire(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (/\/chats\/[^/]+\/workspace/.test(url)) {
        return json({
          state: {
            tabs: [
              { id: "files", kind: "files", name: "Files", params: {} },
              { id: "tab-report", kind: "file", node_id: REPORT, name: "q3-report.md" },
              { id: "tab-notes", kind: "file", node_id: NOTES, name: "notes.md" },
            ],
            active_tab_id: active,
          },
          updated_at: null,
        });
      }
      if (/\/items\/[^/?]+\/children/.test(url)) return json({ value: [], nextMarker: null });
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const found = items[decodeURIComponent(one[1] ?? "")];
        return found ? json(found) : json({ code: "files.not_found", message: "no" }, 404);
      }
      return json({ value: [], nextMarker: null });
    }),
  );
}

/** Which tab the reader left in front. */
let active: string;

function mount() {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <ChatSidePane chatId={CHAT_ID} driveId={DRIVE} rootNodeId={ROOT} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The machine saved the file: the one node frame that is about its bytes. */
const nodeFrame = (entityId: string, reason: string | null = "live_saved"): RealtimeEventFrame =>
  ({
    type: "file_node.changed",
    entity: "file_node",
    entity_id: entityId,
    version: 2,
    org_id: "org_1",
    drive_id: DRIVE,
    parent_id: ROOT,
    ...(reason === null ? {} : { reason }),
  }) as RealtimeEventFrame;

const leaseFrame = (nodeId: string): RealtimeEventFrame =>
  ({
    type: "file_lease.changed",
    entity: "file_lease",
    entity_id: nodeId,
    version: 3,
    org_id: "org_1",
    drive_id: DRIVE,
    lease_node_id: nodeId,
  }) as RealtimeEventFrame;

async function deliver(frame: RealtimeEventFrame): Promise<void> {
  await act(async () => {
    publishFrame(frame);
    await Promise.resolve();
  });
}

/** The tabs the strip is currently marking as changed. */
function markedUpdated(): string[] {
  return Array.from(document.querySelectorAll('[role="tab"]'))
    .filter((tab) => tab.querySelector('[aria-label="updated"]') !== null)
    .map((tab) => tab.textContent ?? "");
}

beforeEach(() => {
  active = "files";
  items = {
    [ROOT]: ROOT_FOLDER,
    [REPORT]: item({ id: REPORT, name: "q3-report.md" }),
    [NOTES]: item({ id: NOTES, name: "notes.md" }),
  };
  stubWire();
  useWorkspaceStore.setState({ chats: {} });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  resetFrameBus();
  useWorkspaceStore.setState({ chats: {} });
});

describe("a file tab the reader is not looking at", () => {
  it("is marked when the drive says its file changed", async () => {
    mount();
    await screen.findByRole("tab", { name: /q3-report\.md/ });
    expect(markedUpdated()).toEqual([]);

    await deliver(nodeFrame(REPORT));

    await waitFor(() => expect(markedUpdated()).toEqual(["q3-report.md"]));
  });

  it("marks only the tab whose file it was", async () => {
    mount();
    await screen.findByRole("tab", { name: /notes\.md/ });

    await deliver(nodeFrame(NOTES));

    await waitFor(() => expect(markedUpdated()).toEqual(["notes.md"]));
  });
});

describe("what the strip must not mark", () => {
  it("leaves the tab in front alone — the reader is watching it happen", async () => {
    active = "tab-report";
    mount();
    await screen.findByTestId("file-tab");

    await deliver(nodeFrame(REPORT));

    // Give the marker every chance to land before ruling it out.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(markedUpdated()).toEqual([]);
  });

  it("leaves a tab alone when its node moved but its bytes did not", async () => {
    // A share, a rename, a move or a trash changes the node and sends a frame
    // with no reason; a dot for it read as an edit that never happened.
    mount();
    await screen.findByRole("tab", { name: /q3-report\.md/ });

    await deliver(nodeFrame(REPORT, null));

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(markedUpdated()).toEqual([]);
  });

  it("says nothing about a node no tab is holding", async () => {
    mount();
    await screen.findByRole("tab", { name: /q3-report\.md/ });

    await deliver(nodeFrame(STRANGER));

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(markedUpdated()).toEqual([]);
  });

  it("ignores a lease frame naming the same node — the bytes did not move", async () => {
    mount();
    await screen.findByRole("tab", { name: /q3-report\.md/ });

    await deliver(leaseFrame(REPORT));

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(markedUpdated()).toEqual([]);
  });
});
