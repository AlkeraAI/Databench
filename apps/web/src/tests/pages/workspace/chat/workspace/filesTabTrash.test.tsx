import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRealtimeStatus } from "@/api/events/status";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import {
  tabKindFor,
  type WorkspaceCtx,
  type WorkspaceTab,
} from "@/pages/workspace/chat/workspace/tabKinds";
import { useWorkspaceStore } from "@/pages/workspace/chat/workspace/workspaceStore";

// Move to trash from the chat's Files pane.
//
// The pane's bar offered Up, New folder, Upload and the view switches, and
// nothing that deletes: a reader who does not right-click had no way to remove
// a file beside the chat, where `/files` offers it on the row. The bar now
// carries the menu's own trash row, so these cases drive the real explorer and
// assert what the wire saw and what the listing shows after its refetch.
import "@/pages/workspace/chat/workspace/FilesTab";

function FilesTabComponent(props: { tab: WorkspaceTab; ctx: WorkspaceCtx }) {
  const kind = tabKindFor("files");
  if (kind === undefined) throw new Error("the `files` tab kind was never registered");
  const Component = kind.Component;
  return <Component {...props} />;
}

const DRIVE = "drv_1";
const CHAT_ID = "cht_1";
const CHAT_NODE = "nd_chat";
const ROOT = "nd_root";
const REPORT = "nd_report";
const KEEP = "nd_keep";

const EDITOR_CAPS = {
  can_read: true,
  can_write: true,
  can_share: true,
  can_delete: true,
  can_rename: true,
  can_download: true,
};
const VIEWER_CAPS = {
  can_read: true,
  can_write: false,
  can_share: false,
  can_delete: false,
  can_rename: false,
  can_download: true,
  refusals: {
    write: "files.insufficient_role",
    delete: "files.insufficient_role",
    rename: "files.insufficient_role",
    share: "files.insufficient_role",
  },
};

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
    attrs: null,
    file: null,
    symlink: null,
    object: null,
    lease: null,
    stale: false,
    trust: null,
    locked: false,
    held: false,
    capabilities: EDITOR_CAPS,
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

/** Every request the tab issued, as method + url, in order. */
let wire: { method: string; url: string }[] = [];
let items: Record<string, Item>;
let children: Record<string, Item[]>;

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
  );
}

function stubWire(): void {
  wire = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      const method = (
        init?.method ?? (input instanceof Request ? input.method : "GET")
      ).toUpperCase();
      wire.push({ method, url });
      const listed = /\/items\/([^/?]+)\/children/.exec(url);
      if (listed) {
        const rows = children[decodeURIComponent(listed[1] ?? "")] ?? [];
        return json({ value: rows, nextMarker: null });
      }
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const id = decodeURIComponent(one[1] ?? "");
        if (method === "DELETE") {
          // The server trashes the row: every later listing of its folder
          // comes back without it.
          for (const [folder, rows] of Object.entries(children)) {
            children[folder] = rows.filter((row) => row.id !== id);
          }
          return json({ id: "op_trash", status: "completed", undoable: true });
        }
        const found = items[id];
        if (!found) return json({ code: "files.not_found", message: "no" }, 404);
        return json(found);
      }
      return json({ value: [], nextMarker: null });
    }),
  );
}

const CTX: WorkspaceCtx = { chatId: CHAT_ID, driveId: DRIVE, rootNodeId: ROOT };

function mount() {
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <FilesTabComponent tab={{ id: "files", kind: "files", name: "Files" }} ctx={CTX} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function rowFor(id: string): HTMLElement {
  const row = document.querySelector(`[data-row-id="${id}"]`);
  if (!(row instanceof HTMLElement)) throw new Error(`no row painted for ${id}`);
  return row;
}

async function selectReport(): Promise<HTMLElement> {
  await screen.findByText("q3-report.html");
  const row = rowFor(REPORT);
  fireEvent.click(row);
  await waitFor(() => expect(row).toHaveAttribute("aria-selected", "true"));
  return row;
}

function trashButton(): HTMLElement | null {
  return screen.queryByRole("button", { name: "Move to trash" });
}

/** Let every queued write reach the wire, so "nothing was sent" is a verdict
 *  about a settled pane rather than about a request still in flight. */
async function settle(): Promise<void> {
  await act(async () => {
    for (let turn = 0; turn < 5; turn += 1) await new Promise((done) => setTimeout(done, 0));
  });
}

function deletes(): { method: string; url: string }[] {
  return wire.filter((call) => call.method === "DELETE");
}

/** The listing's rows as the wire gave them to the pane, for one reader. */
function seed(caps: typeof EDITOR_CAPS | typeof VIEWER_CAPS): void {
  const report = item({
    id: REPORT,
    name: "q3-report.html",
    file: { size: 900 } as Item["file"],
    capabilities: caps as Item["capabilities"],
  });
  const keep = item({
    id: KEEP,
    name: "notes.md",
    file: { size: 10 } as Item["file"],
    capabilities: caps as Item["capabilities"],
  });
  const root = item({
    id: ROOT,
    name: "scratch",
    kind: "folder",
    parentId: CHAT_NODE,
    capabilities: caps as Item["capabilities"],
  });
  items = { [CHAT_NODE]: CHAT_FOLDER, [ROOT]: root, [REPORT]: report, [KEEP]: keep };
  children = { [ROOT]: [report, keep] };
}

beforeEach(() => {
  seed(EDITOR_CAPS);
  stubWire();
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "connected" });
});

afterEach(() => {
  vi.unstubAllGlobals();
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "idle" });
});

describe("Move to trash in the chat's Files pane", () => {
  it("is on the bar once a row is selected, and not before", async () => {
    mount();
    await screen.findByText("q3-report.html");
    expect(trashButton()).toBeNull();

    await selectReport();

    expect(trashButton()).toBeEnabled();
  });

  it("sends the same DELETE /files sends, and the row is gone once the folder is read again", async () => {
    mount();
    await selectReport();

    fireEvent.click(trashButton()!);

    await waitFor(() => expect(deletes()).toHaveLength(1));
    const sent = new URL(deletes()[0]!.url, "http://x");
    expect(sent.pathname).toBe(`/api/v1/files/drives/${DRIVE}/items/${REPORT}`);
    expect(sent.searchParams.get("permanent")).toBe("false");

    // The listing is read again after the write, and that read no longer has it.
    const at = wire.findIndex((call) => call.method === "DELETE");
    await waitFor(() =>
      expect(
        wire
          .slice(at + 1)
          .some((call) => call.method === "GET" && call.url.includes(`/items/${ROOT}/children`)),
      ).toBe(true),
    );
    await waitFor(() => expect(screen.queryByText("q3-report.html")).not.toBeInTheDocument());
    await settle();
    expect(screen.queryByText("q3-report.html")).not.toBeInTheDocument();
    expect(screen.getByText("notes.md")).toBeInTheDocument();
  });

  it("runs the row menu's Move to trash to the same request", async () => {
    mount();
    const row = await selectReport();

    fireEvent.contextMenu(row);
    const menu = await screen.findByRole("menu");
    fireEvent.click(within(menu).getByRole("menuitem", { name: /Move to trash/ }));

    await waitFor(() => expect(deletes()).toHaveLength(1));
    expect(new URL(deletes()[0]!.url, "http://x").pathname).toBe(
      `/api/v1/files/drives/${DRIVE}/items/${REPORT}`,
    );
    await waitFor(() => expect(screen.queryByText("q3-report.html")).not.toBeInTheDocument());
  });

  it("is refused to a viewer, who can trash nothing from the bar, the menu or the keyboard", async () => {
    seed(VIEWER_CAPS);
    mount();
    const row = await selectReport();

    const button = trashButton();
    expect(button).not.toBeNull();
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "You can view this, not trash it.");
    fireEvent.click(button!);

    fireEvent.contextMenu(row);
    const menu = await screen.findByRole("menu");
    const menuTrash = within(menu).getByRole("menuitem", { name: /Move to trash/ });
    expect(menuTrash).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(menuTrash);

    fireEvent.keyDown(row, { key: "Delete" });
    fireEvent.keyDown(row, { key: "Backspace", metaKey: true });
    fireEvent.keyDown(row, { key: "Backspace", ctrlKey: true });
    await settle();

    expect(deletes()).toEqual([]);
    expect(screen.getByText("q3-report.html")).toBeInTheDocument();
  });
});
