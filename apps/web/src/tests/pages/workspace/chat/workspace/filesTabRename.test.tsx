import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

// Renaming from the chat's Files tab, driven through the real explorer.
//
// The pane mounts the Files page's action layer, whose Rename row is enabled by
// a per-row capability the server answers without regard to any lease. So the
// row is offered whether or not the surface can act on it, and the two things
// this file pins are the two halves of that: a rename asked for here reaches
// the wire, and a rename the surface cannot make is refused on the row rather
// than closing the menu and doing nothing.
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
const ROWS: Item[] = [
  item({ id: REPORT, name: "q3-report.html", file: { size: 900 } as Item["file"] }),
];

/** A lease a machine holds and does not admit inbound writes through — the one
 *  state in which this pane is read-only whatever the reader's own rung says. */
const MACHINE_LEASE = {
  holder: "Dana",
  machine: "box-1",
  live: true,
  inbound: false,
} as unknown as Item["lease"];

interface Call {
  method: string;
  url: string;
  body: unknown;
}

let wire: Call[] = [];
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
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      const method = (
        init?.method ?? (input instanceof Request ? input.method : "GET")
      ).toUpperCase();
      // The typed client sends a `Request`; a plain `init.body` is the other
      // shape a caller can take, and both are read so the assertion is about
      // what was sent rather than about which overload sent it.
      const sent =
        input instanceof Request
          ? await input.clone().text()
          : typeof init?.body === "string"
            ? init.body
            : "";
      const raw = sent === "" ? null : (JSON.parse(sent) as { name?: string });
      wire.push({ method, url, body: raw });
      const children_ = /\/items\/([^/?]+)\/children/.exec(url);
      if (children_) {
        const rows = children[decodeURIComponent(children_[1] ?? "")] ?? [];
        return json({ value: rows, nextMarker: null });
      }
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const id = decodeURIComponent(one[1] ?? "");
        const found = items[id];
        if (!found) return json({ code: "files.not_found", message: "no" }, 404);
        if (method === "PATCH") {
          const name = raw?.name ?? found.name;
          return json({ ...found, name, nameDisplay: name, etag: "e2" });
        }
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

/** The tab, mounted, with the row menu open on the one file in the listing. */
async function openRowMenu(): Promise<HTMLElement> {
  mount();
  await screen.findByText("q3-report.html");
  const row = rowFor(REPORT);
  fireEvent.click(row);
  await waitFor(() => expect(row).toHaveAttribute("aria-selected", "true"));
  fireEvent.contextMenu(row);
  return await screen.findByRole("menu");
}

function menuRow(menu: HTMLElement, action: string): HTMLElement {
  const found = menu.querySelector(`[data-item="${action}"]`);
  if (!(found instanceof HTMLElement)) throw new Error(`no \`${action}\` row in the menu`);
  return found;
}

beforeEach(() => {
  items = {
    [CHAT_NODE]: CHAT_FOLDER,
    [ROOT]: ROOT_FOLDER,
    ...Object.fromEntries(ROWS.map((row) => [row.id, row])),
  };
  children = { [ROOT]: ROWS };
  stubWire();
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "connected" });
});

afterEach(() => {
  vi.unstubAllGlobals();
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "idle" });
});

describe("renaming a file from the chat's Files tab", () => {
  it("opens the editor on the row and sends the new name when it is committed", async () => {
    const menu = await openRowMenu();
    fireEvent.click(menuRow(menu, "rename"));

    const field = await screen.findByRole("textbox", { name: "New name" });
    expect(field).toHaveValue("q3-report.html");

    fireEvent.change(field, { target: { value: "q3-final.html" } });
    fireEvent.keyDown(field, { key: "Enter" });

    await waitFor(() => {
      const patch = wire.find(
        (call) => call.method === "PATCH" && call.url.includes(`/items/${REPORT}`),
      );
      expect(patch?.body).toEqual({ name: "q3-final.html" });
    });
    // The editor closes on the answer; the row is back to being a row.
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "New name" })).not.toBeInTheDocument(),
    );
  });

  it("abandons the rename on Escape without sending anything", async () => {
    const menu = await openRowMenu();
    fireEvent.click(menuRow(menu, "rename"));

    const field = await screen.findByRole("textbox", { name: "New name" });
    fireEvent.change(field, { target: { value: "not-this.html" } });
    fireEvent.keyDown(field, { key: "Escape" });

    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "New name" })).not.toBeInTheDocument(),
    );
    expect(wire.some((call) => call.method === "PATCH")).toBe(false);
  });

  it("refuses the row, with the reason, while a machine holds the folder", async () => {
    // The row's own `can_rename` is a write answer the server computes without
    // regard to the lease, so the menu has to be told about the lease here or
    // it offers a rename the surface cannot make.
    items = {
      ...items,
      [ROOT]: { ...ROOT_FOLDER, lease: MACHINE_LEASE } as Item,
    };
    const menu = await openRowMenu();
    const rename = menuRow(menu, "rename");

    expect(rename).toHaveAttribute("aria-disabled", "true");
    expect(rename.getAttribute("title") ?? "").not.toBe("");

    fireEvent.click(rename);
    expect(screen.queryByRole("textbox", { name: "New name" })).not.toBeInTheDocument();
  });
});
