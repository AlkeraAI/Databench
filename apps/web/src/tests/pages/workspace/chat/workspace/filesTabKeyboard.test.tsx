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

// The keyboard the chat's Files tab actually binds, on a Mac.
//
// The tab mounts the Files page's action layer, and that layer reads ONE
// platform for both halves of the contract: which keys fire which action, and
// which glyphs the row menu prints beside them. A pane that does not say which
// platform it is on gets the PC table — where Backspace alone is the delete —
// so these cases are driven through the real explorer with a Mac navigator and
// assert what the wire saw, not what a handler was told.
import "@/pages/workspace/chat/workspace/FilesTab";

function filesKind() {
  const kind = tabKindFor("files");
  if (kind === undefined) throw new Error("the `files` tab kind was never registered");
  return kind;
}
function FilesTabComponent(props: { tab: WorkspaceTab; ctx: WorkspaceCtx }) {
  const Component = filesKind().Component;
  return <Component {...props} />;
}

const DRIVE = "drv_1";
const CHAT_ID = "cht_1";
const CHAT_NODE = "nd_chat";
const ROOT = "nd_root";
const REPORT = "nd_report";

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
    capabilities: {
      can_read: true,
      can_write: true,
      can_share: true,
      can_delete: true,
      can_rename: true,
      can_download: true,
    },
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

const ROWS: Item[] = [item({ id: REPORT, name: "q3-report.html", file: { size: 900 } as Item["file"] })];

/** Every request the tab issued, as method + url. */
let wire: { method: string; url: string }[] = [];
let items: Record<string, Item>;
let children: Record<string, Item[]>;

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(status === 204 ? null : JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    }),
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
      const children_ = /\/items\/([^/?]+)\/children/.exec(url);
      if (children_) {
        const rows = children[decodeURIComponent(children_[1] ?? "")] ?? [];
        return json({ value: rows, nextMarker: null });
      }
      if (/\/items\/[^/?]+\/copy/.test(url)) return json({ id: "op_1", status: "queued" });
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const id = decodeURIComponent(one[1] ?? "");
        if (method === "DELETE") return json({ id: "op_2", status: "completed" });
        const found = items[id];
        if (!found) return json({ code: "files.not_found", message: "no" }, 404);
        if (method === "PATCH") return json(found);
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

/** The row's `role="row"` element, which is where selection and the keyboard live. */
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

/** Let every queued write reach the wire, so "nothing was sent" is a verdict
 *  about a settled pane rather than about a request still in flight. A trash
 *  the keyboard fires goes out on the turn after the keystroke. */
async function settle(): Promise<void> {
  await act(async () => {
    for (let turn = 0; turn < 5; turn += 1) await new Promise((done) => setTimeout(done, 0));
  });
}

/** A Mac, as every signal `detectPlatform` consults reports one. */
function pretendMac(): void {
  const withData = navigator as Navigator & { userAgentData?: { platform?: string } };
  for (const [key, value] of [
    ["platform", "MacIntel"],
    ["userAgent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"],
  ] as const) {
    Object.defineProperty(navigator, key, { value, configurable: true });
  }
  Object.defineProperty(withData, "userAgentData", {
    value: { platform: "macOS" },
    configurable: true,
  });
}

const NAVIGATOR_BEFORE = {
  platform: Object.getOwnPropertyDescriptor(navigator, "platform"),
  userAgent: Object.getOwnPropertyDescriptor(navigator, "userAgent"),
};

beforeEach(() => {
  items = {
    [CHAT_NODE]: CHAT_FOLDER,
    [ROOT]: ROOT_FOLDER,
    ...Object.fromEntries(ROWS.map((row) => [row.id, row])),
  };
  children = { [ROOT]: ROWS };
  stubWire();
  pretendMac();
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "connected" });
});

afterEach(() => {
  vi.unstubAllGlobals();
  for (const [key, descriptor] of Object.entries(NAVIGATOR_BEFORE)) {
    if (descriptor) Object.defineProperty(navigator, key, descriptor);
    else Reflect.deleteProperty(navigator, key);
  }
  Reflect.deleteProperty(navigator as object, "userAgentData");
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "idle" });
});

describe("the chat pane's keyboard is the reader's own platform", () => {
  it("does not trash a selected file on a bare Backspace", async () => {
    mount();
    const row = await selectReport();

    // On macOS the delete is Cmd+Backspace. Bare Backspace is the PC binding,
    // and a pane that fell back to it deletes a file with no modifier and no
    // confirmation.
    fireEvent.keyDown(row, { key: "Backspace" });
    await settle();

    expect(wire.filter((call) => call.method === "DELETE")).toEqual([]);
    expect(screen.getByText("q3-report.html")).toBeInTheDocument();
  });

  it("trashes on Cmd+Backspace, which is what a Mac binds the delete to", async () => {
    mount();
    const row = await selectReport();

    fireEvent.keyDown(row, { key: "Backspace", metaKey: true });

    await waitFor(() =>
      expect(
        wire.some(
          (call) => call.method === "DELETE" && call.url.includes(`/items/${REPORT}`),
        ),
      ).toBe(true),
    );
  });

  it("reaches the copy handler on Cmd+C, so Cmd+V pastes what was copied", async () => {
    mount();
    const row = await selectReport();

    // Cmd is the Mac's accelerator; the PC table rejects it outright, so on a
    // pane bound to that table the clipboard never fills and the paste that
    // follows has nothing to write.
    fireEvent.keyDown(row, { key: "c", metaKey: true });
    fireEvent.keyDown(row, { key: "v", metaKey: true });

    await waitFor(() =>
      expect(wire.some((call) => call.method === "POST" && call.url.includes("/copy"))).toBe(true),
    );
  });

  it("prints the Mac's glyphs in the row menu, not Ctrl", async () => {
    mount();
    const row = await selectReport();

    fireEvent.contextMenu(row);
    const menu = await screen.findByRole("menu");
    const caption = (action: string): string =>
      within(menu).getByText((_text, node) => node?.getAttribute("data-item") === action)
        .textContent ?? "";

    expect(caption("copy")).toContain("⌘C");
    expect(caption("trash")).toContain("⌘⌫");
    expect(menu.textContent).not.toContain("Ctrl");
  });
});
