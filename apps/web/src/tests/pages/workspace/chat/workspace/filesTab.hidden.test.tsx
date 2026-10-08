import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearStorageMirror } from "@alkera/ui/storage";

import { useRealtimeStatus } from "@/api/events/status";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesTab } from "@/pages/workspace/chat/workspace/FilesTab";
import type { WorkspaceCtx } from "@/pages/workspace/chat/workspace/tabKinds";
import { forgetChatPane, useWorkspaceStore } from "@/pages/workspace/chat/workspace/workspaceStore";
import { SHOW_HIDDEN_STORAGE_KEY } from "@/pages/workspace/files/hiddenEntries";

// The chat's Files pane hides the same system entries the Files page does: a
// notebook's `__marimo__/` outputs folder and dot files, at any depth, until
// the viewer turns on "Show hidden files". The choice is the viewer's, so it
// carries over from the Files page and back.

const DRIVE = "drv_1";
const CHAT_ID = "cht_1";
const ROOT = "nd_root";

function item(over: Partial<Item> & { id: string; name: string }): Item {
  return {
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    nameDisplay: over.name,
    nameEncoding: "utf-8",
    pathBytes: "",
    parentId: ROOT,
    path: null,
    etag: "e1",
    ctag: "c1",
    object: null,
    lease: null,
    stale: false,
    locked: false,
    held: false,
    capabilities: { can_write: true },
    shared: false,
    trashed: false,
    ...over,
  } as unknown as Item;
}

const ROOT_FOLDER = item({ id: ROOT, name: "scratch", kind: "folder", parentId: "nd_chat" });
const SUB = item({ id: "nd_sub", name: "charts", kind: "folder" });
const ROWS = [
  item({ id: "nd_nb", name: "analysis.py" }),
  item({ id: "nd_marimo", name: "__marimo__", kind: "folder" }),
  item({ id: "nd_env", name: ".env" }),
  SUB,
];
const SUB_ROWS = [
  item({ id: "nd_png", name: "q3.png", parentId: "nd_sub" }),
  item({ id: "nd_ds", name: ".DS_Store", parentId: "nd_sub" }),
];

function stubWire(): void {
  const items: Record<string, Item> = { [ROOT]: ROOT_FOLDER, nd_sub: SUB };
  const children: Record<string, Item[]> = { [ROOT]: ROWS, nd_sub: SUB_ROWS };
  const json = (body: unknown, status = 200) =>
    Promise.resolve(
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
    );
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
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

const CTX: WorkspaceCtx = { chatId: CHAT_ID, driveId: DRIVE, rootNodeId: ROOT };

function mount(folderId?: string) {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <FilesTab
          tab={{
            id: "files",
            kind: "files",
            name: "Files",
            ...(folderId === undefined ? {} : { params: { folderId } }),
          }}
          ctx={CTX}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function paintedIds(): string[] {
  return Array.from(document.querySelectorAll("[data-row-id]")).map(
    (element) => element.getAttribute("data-row-id") ?? "",
  );
}

const toggle = () => screen.getByRole("button", { name: "Show hidden files" });

beforeEach(() => {
  window.localStorage.clear();
  clearStorageMirror();
  stubWire();
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "connected" });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  forgetChatPane(CHAT_ID);
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "idle" });
  window.localStorage.clear();
  clearStorageMirror();
});

describe("hidden entries in the chat's Files pane", () => {
  it("hides __marimo__ and dot files by default and shows them dimmed with the toggle", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(paintedIds()).toEqual(["nd_nb", "nd_sub"]));

    await user.click(toggle());

    await waitFor(() => expect(paintedIds()).toEqual(["nd_nb", "nd_marimo", "nd_env", "nd_sub"]));
    const dimmed = Array.from(document.querySelectorAll('[data-hidden-entry="true"]')).map(
      (element) => element.getAttribute("data-row-id"),
    );
    expect(dimmed.sort()).toEqual(["nd_env", "nd_marimo"]);
  });

  it("hides a dot file inside a subfolder", async () => {
    mount("nd_sub");
    await waitFor(() => expect(paintedIds()).toEqual(["nd_png"]));
  });

  it("opens with the choice the viewer made elsewhere", async () => {
    window.localStorage.setItem(SHOW_HIDDEN_STORAGE_KEY, "true");
    mount();
    await waitFor(() => expect(paintedIds()).toHaveLength(4));
    expect(toggle()).toHaveAttribute("aria-pressed", "true");
  });

  it("renders and toggles when storage refuses every read and write", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(paintedIds()).toEqual(["nd_nb", "nd_sub"]));
    await user.click(toggle());
    await waitFor(() => expect(paintedIds()).toHaveLength(4));
  });
});
