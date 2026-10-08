// A file both sides changed, in the chat's Files tab: the drive kept both
// versions, so the tab lists the conflicted copy as a file with a chip on its
// row and says nothing else about it — no count in the status bar, even while
// the drive holds open conflict records under the chat's lease. Driven through
// the real registered tab with `fetch` stubbed at the wire.

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRealtimeStatus } from "@/api/events/status";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { tabKindFor, type WorkspaceCtx } from "@/pages/workspace/chat/workspace/tabKinds";
import { useWorkspaceStore } from "@/pages/workspace/chat/workspace/workspaceStore";

import "@/pages/workspace/chat/workspace/FilesTab";

const DRIVE = "drv_1";
const CHAT_NODE = "nd_chat";
const ROOT = "nd_root";
const CTX: WorkspaceCtx = { chatId: "cht_1", driveId: DRIVE, rootNodeId: ROOT };
const COPY_NAME = "notes (conflicted copy from Dana, 2026-09-24 03.25 UTC).md";

function item(over: Record<string, unknown> & { id: string; name: string }): Item {
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
    attrs: { mtime: "2026-09-01T10:00:00Z" },
    lease: null,
    live: null,
    stale: false,
    locked: false,
    held: false,
    capabilities: { can_write: true },
    shared: false,
    trashed: false,
    ...over,
  } as unknown as Item;
}

/** The lease facet every node under the chat's folder carries. */
const HELD = {
  holder: "Dana",
  machine: "box-1",
  machine_name: "demo-box",
  node_id: CHAT_NODE,
};

const CHAT_FOLDER = item({
  id: CHAT_NODE,
  name: "3952c9e2.alkerachat",
  kind: "folder",
  parentId: "nd_home",
  object: {
    id: "obj_1",
    type: "chat",
    title: "Q3 review",
    web_url: "/chat/cht_1",
    metadata: { files_node_id: ROOT },
  },
});
const ROOT_FOLDER = item({ id: ROOT, name: "scratch", kind: "folder", parentId: CHAT_NODE });
const NOTES = item({ id: "nd_notes", name: "notes.md", lease: HELD });
const COPY = item({ id: "nd_copy", name: COPY_NAME, lease: HELD, conflictOf: "nd_notes" });
const DEEP = item({ id: "nd_deep", name: "deep.csv", parentId: "nd_sub", lease: HELD });
/** A file in some other folder, held by some other chat's machine. */
const ELSEWHERE = item({
  id: "nd_else",
  name: "else.md",
  parentId: "nd_other",
  lease: { ...HELD, node_id: "nd_other_chat" },
});

function conflict(id: string, nodeId: string) {
  return {
    id,
    nodeId,
    baseVersionId: null,
    theirsVersionId: "v_t",
    mineVersionId: "v_m",
    state: "auto",
    copyNodeId: null,
    arrivedFrom: "holder",
    who: null,
  };
}

let open: unknown[] = [];
const ITEMS: Record<string, Item> = {
  [CHAT_NODE]: CHAT_FOLDER,
  [ROOT]: ROOT_FOLDER,
  [NOTES.id]: NOTES,
  [COPY.id]: COPY,
  [DEEP.id]: DEEP,
  [ELSEWHERE.id]: ELSEWHERE,
};

function stubWire(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const json = (body: unknown, status = 200) =>
        Promise.resolve(
          new Response(JSON.stringify(body), {
            status,
            headers: { "content-type": "application/json" },
          }),
        );
      if (url.includes("/conflicts")) return json({ value: open });
      if (/\/items\/nd_root\/children/.test(url))
        return json({ value: [NOTES, COPY], nextMarker: null });
      if (url.includes("/children")) return json({ value: [], nextMarker: null });
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const found = ITEMS[decodeURIComponent(one[1] ?? "")];
        return found ? json(found) : json({ code: "files.not_found", message: "no" }, 404);
      }
      return json({ value: [], nextMarker: null });
    }),
  );
}

function mount() {
  const kind = tabKindFor("files");
  if (kind === undefined) throw new Error("the `files` tab kind was never registered");
  const Component = kind.Component;
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <Component tab={{ id: "files", kind: "files", name: "Files" }} ctx={CTX} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function barChips(): string[] {
  const bar = document.querySelector<HTMLElement>(".alk-ws-status");
  if (bar === null) throw new Error("the tab rendered no status bar");
  return Array.from(bar.querySelectorAll(".alk-ws-status__chips .alk-pill")).map(
    (chip) => chip.textContent ?? "",
  );
}

beforeEach(() => {
  open = [];
  stubWire();
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "connected" });
});

afterEach(() => {
  vi.unstubAllGlobals();
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "idle" });
});

describe("conflicts in the chat's Files tab", () => {
  it("says nothing about conflicts under the chat's lease beyond the files themselves", async () => {
    open = [conflict("cf_1", NOTES.id), conflict("cf_2", DEEP.id), conflict("cf_3", ELSEWHERE.id)];
    mount();

    await screen.findByText("notes.md");
    await screen.findByText(COPY_NAME);
    // Long enough for any read the tab starts beside the listing to land.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(barChips().some((chip) => /conflict/.test(chip))).toBe(false);
    for (const label of ["Keep this", "Keep the other", "Keep both"]) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    }
  });

  it("lists the conflicted copy under its own name, with no chip on either row", async () => {
    mount();

    await screen.findByText(COPY_NAME);
    const row = document.querySelector(`[data-row-id="${COPY.id}"]`);
    const plain = document.querySelector(`[data-row-id="${NOTES.id}"]`);
    // The name is the whole word on it; a chip only repeated it and, in the
    // narrow name cell of a side pane, cut both down to a stub.
    expect(row?.querySelector(".alk-files-live-chip")).toBeNull();
    expect(plain?.querySelector(".alk-files-live-chip")).toBeNull();
    expect(plain?.textContent).not.toContain("conflicted copy");
  });
});
