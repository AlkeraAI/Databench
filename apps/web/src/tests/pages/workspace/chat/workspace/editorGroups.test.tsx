// @vitest-environment jsdom
//
// The editor groups as a reader drives them: dragging a tab to another group or
// onto a group's edge, the group's own split buttons, the dividers, and the
// keyboard. The pane is mounted for real over a stored document read through a
// stubbed `fetch`; the two tab kinds are stood in as registrations, because
// what is pinned here is the arrangement, not what a tab draws.

import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, createEvent, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { flushSync } from "react-dom";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";

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
  return { toggledView: () => null };
});

import { ChatSidePane } from "@/pages/workspace/chat/workspace/ChatSidePane";
import { TAB_DRAG_TYPE, dropZoneAt } from "@/pages/workspace/chat/workspace/EditorGroups";
import { groupOrder } from "@/pages/workspace/chat/workspace/editorLayout";
import {
  forgetChatPane,
  tabsOf,
  useWorkspaceStore,
  type ChatWorkspaceDoc,
} from "@/pages/workspace/chat/workspace/workspaceStore";

const CHAT_ID = "cht_groups";
const DRIVE = "drv_1";
const ROOT = "nd_scratch";
const A = "00000000-0000-4000-8000-00000000000a";
const B = "00000000-0000-4000-8000-00000000000b";
const C = "00000000-0000-4000-8000-00000000000c";

function file(id: string, name: string): Item {
  return {
    id,
    driveId: DRIVE,
    kind: "file",
    name,
    nameDisplay: name,
    parentId: ROOT,
    pathBytes: `/Chats/c.alkerachat/scratch/${name}`,
    etag: "e1",
    ctag: "c1",
    file: { mime_type: "text/plain", size: 10, content_hash: "h", scan_state: "clean" },
    capabilities: { can_read: true, can_write: true },
    trashed: false,
  } as unknown as Item;
}

const STORED: ChatWorkspaceDoc = {
  tabs: [
    { id: "files", kind: "files", name: "Files", params: {}, group: "g-left" },
    { id: "t-a", kind: "file", node_id: A, name: "a.md", group: "g-left" },
    { id: "t-b", kind: "file", node_id: B, name: "b.md", group: "g-left" },
    { id: "t-c", kind: "file", node_id: C, name: "c.md", group: "g-right" },
  ],
  active_tab_id: "t-a",
  layout: {
    v: 1,
    root: { split: "row", children: [{ g: "g-left" }, { g: "g-right" }], sizes: [0.5, 0.5] },
    active_group: "g-left",
    active: { "g-left": "t-a", "g-right": "t-c" },
  },
};

let stored: ChatWorkspaceDoc;

function stubWire(): void {
  const items: Record<string, Item> = { [A]: file(A, "a.md"), [B]: file(B, "b.md"), [C]: file(C, "c.md") };
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : String(input);
      const json = (body: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }));
      if (/\/chats\/[^/]+\/workspace/.test(url)) {
        if (init?.method === "PUT") return json({ state: JSON.parse(String(init.body)).state, updated_at: "now" });
        return json({ state: stored, updated_at: null });
      }
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const found = items[decodeURIComponent(one[1] ?? "")];
        return found ? json(found) : json({ code: "files.not_found" }, 404);
      }
      return json({ value: [], nextMarker: null });
    }),
  );
}

function mount() {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <ChatSidePane chatId={CHAT_ID} driveId={DRIVE} rootNodeId={ROOT} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const entry = () => {
  const current = useWorkspaceStore.getState().chats[CHAT_ID];
  if (!current) throw new Error("no workspace");
  return current;
};

/** A drag's data, as the platform carries it between the events of one drag. */
function transfer(): DataTransfer {
  const data = new Map<string, string>();
  return {
    get types() {
      return [...data.keys()];
    },
    setData: (type: string, value: string) => void data.set(type, value),
    getData: (type: string) => data.get(type) ?? "",
    dropEffect: "none",
    effectAllowed: "all",
  } as unknown as DataTransfer;
}

function group(n: number): HTMLElement {
  return screen.getByRole("region", { name: `Editor group ${n}` });
}

function tab(name: string): HTMLElement {
  return screen.getByRole("tab", { name: new RegExp(`^${name}`) });
}

beforeEach(() => {
  stored = STORED;
  stubWire();
});

afterEach(() => {
  cleanup();
  forgetChatPane(CHAT_ID);
  vi.unstubAllGlobals();
});

describe("the groups as stored", () => {
  it("draws each group with its own strip and the tab in front of it", async () => {
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    expect(within(group(1)).getAllByRole("tab").map((node) => node.textContent)).toEqual(["Files", "a.md", "b.md"]);
    expect(within(group(2)).getAllByRole("tab").map((node) => node.textContent)).toEqual(["c.md"]);
    // Both groups show their front tab at once.
    expect(screen.getAllByTestId("file-tab").map((node) => node.textContent)).toEqual(["a.md", "c.md"]);
  });
});

describe("dragging a tab", () => {
  it("to another group's strip moves it there, at the place it was dropped", async () => {
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    const data = transfer();
    fireEvent.dragStart(tab("b.md"), { dataTransfer: data });
    fireEvent.dragOver(tab("c.md"), { dataTransfer: data });
    fireEvent.drop(tab("c.md"), { dataTransfer: data });

    await waitFor(() => expect(tabsOf(entry(), "g-right").map((t) => t.node_id)).toEqual([B, C]));
    expect(tabsOf(entry(), "g-left").map((t) => t.id)).toEqual(["files", "t-a"]);
    expect(within(group(2)).getAllByRole("tab").map((node) => node.textContent)).toEqual(["b.md", "c.md"]);
  });

  it("onto the middle of another group's panel moves it into that group", async () => {
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    const data = transfer();
    fireEvent.dragStart(tab("a.md"), { dataTransfer: data });
    const surface = await screen.findByTestId("drop-g-right");
    fireEvent.dragOver(surface, { dataTransfer: data });
    fireEvent.drop(surface, { dataTransfer: data });

    await waitFor(() => expect(tabsOf(entry(), "g-right").map((t) => t.node_id)).toEqual([C, A]));
    // The drop surfaces go once the drag is over.
    await waitFor(() => expect(screen.queryByTestId("drop-g-right")).toBeNull());
  });

  it("lands even though the browser runs pending work between one listener and the next", async () => {
    // A real drop is dispatched by the browser, which flushes queued work after
    // every listener; jsdom's dispatch does not. A capture listener that flushes
    // React stands in for that checkpoint: anything that tore the drop surfaces
    // down on the way into the event would take the target away here.
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    const data = transfer();
    fireEvent.dragStart(tab("a.md"), { dataTransfer: data });
    const surface = await screen.findByTestId("drop-g-right");
    const checkpoint = (): void => flushSync(() => {});
    document.addEventListener("drop", checkpoint, true);
    try {
      fireEvent.dragOver(surface, { dataTransfer: data });
      fireEvent.drop(surface, { dataTransfer: data });
    } finally {
      document.removeEventListener("drop", checkpoint, true);
    }
    await waitFor(() => expect(tabsOf(entry(), "g-right").map((t) => t.node_id)).toEqual([C, A]));
  });

  it("onto a group's edge splits that group and puts the tab on that side", async () => {
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    const data = transfer();
    fireEvent.dragStart(tab("b.md"), { dataTransfer: data });
    const surface = await screen.findByTestId("drop-g-right");
    surface.getBoundingClientRect = () => ({ left: 0, top: 0, width: 400, height: 400, right: 400, bottom: 400, x: 0, y: 0, toJSON: () => ({}) });
    // jsdom has no DragEvent, so the pointer's place is put on the event by hand.
    for (const type of ["dragOver", "drop"] as const) {
      const event = createEvent[type](surface, { dataTransfer: data });
      Object.defineProperty(event, "clientX", { value: 200 });
      Object.defineProperty(event, "clientY", { value: 390 });
      fireEvent(surface, event);
    }

    await waitFor(() => expect(entry().groups).toHaveLength(3));
    const layout = entry().layout;
    expect(layout.kind === "split" && layout.children[1]?.kind === "split" && layout.children[1].axis).toBe("column");
    expect(groupOrder(layout).map((id) => tabsOf(entry(), id).map((t) => t.node_id ?? t.id))).toEqual([
      ["files", A],
      [C],
      [B],
    ]);
  });

  it("ignores a drag that is not a workspace tab, like a file from the desktop", async () => {
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    const before = JSON.stringify(entry().tabs);
    const data = transfer();
    data.setData("Files", "report.pdf");
    fireEvent.dragOver(tab("c.md"), { dataTransfer: data });
    fireEvent.drop(tab("c.md"), { dataTransfer: data });
    expect(JSON.stringify(entry().tabs)).toBe(before);
  });

  it("from another chat's workspace is not taken", async () => {
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    const data = transfer();
    data.setData(TAB_DRAG_TYPE, JSON.stringify({ chat: "some-other-chat", tab: "t-a" }));
    fireEvent.drop(tab("c.md"), { dataTransfer: data });
    expect(tabsOf(entry(), "g-right").map((t) => t.id)).toEqual(["t-c"]);
  });
});

describe("where a drop on a panel lands", () => {
  it.each([
    [200, 200, "center"],
    [10, 200, "left"],
    [390, 200, "right"],
    [200, 10, "up"],
    [200, 390, "down"],
    [5, 20, "left"],
  ] as const)("(%i, %i) in a 400×400 panel: %s", (x, y, zone) => {
    expect(dropZoneAt(x, y, 400, 400)).toBe(zone);
  });

  it("is the middle in a panel that has not been laid out", () => {
    expect(dropZoneAt(0, 0, 0, 0)).toBe("center");
  });
});

describe("a group's own controls", () => {
  it("split the tab in front of that group to the right", async () => {
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    fireEvent.click(within(group(2)).getByRole("button", { name: "Split right" }));
    await waitFor(() => expect(entry().groups).toHaveLength(3));
    expect(groupOrder(entry().layout).map((id) => tabsOf(entry(), id).map((t) => t.node_id ?? t.id))).toEqual([
      ["files", A, B],
      [C],
      [C],
    ]);
  });

  it("cannot split the folder browser", async () => {
    stored = { ...STORED, active_tab_id: "files", layout: { ...STORED.layout!, active: { "g-left": "files", "g-right": "t-c" } } };
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    expect(within(group(1)).getByRole("button", { name: "Split right" })).toBeDisabled();
  });

  it("move the tab in front to the next group from the menu", async () => {
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    fireEvent.click(within(group(1)).getByRole("button", { name: "More group actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Move to next group" }));
    await waitFor(() => expect(tabsOf(entry(), "g-right").map((t) => t.node_id)).toEqual([C, A]));
  });
});

describe("the divider between groups", () => {
  it("is a separator the arrow keys move", async () => {
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    const divider = screen.getByRole("separator", { name: "Resize editor groups" });
    expect(divider).toHaveAttribute("aria-valuenow", "50");
    fireEvent.keyDown(divider, { key: "ArrowRight" });
    await waitFor(() => expect(divider).toHaveAttribute("aria-valuenow", "55"));
    expect((entry().layout as { sizes?: number[] }).sizes?.[0]).toBeCloseTo(0.55);
    fireEvent.keyDown(divider, { key: "ArrowLeft" });
    fireEvent.keyDown(divider, { key: "ArrowLeft" });
    await waitFor(() => expect(divider).toHaveAttribute("aria-valuenow", "45"));
  });
});

describe("the keyboard", () => {
  // jsdom's navigator is not a Mac's, so the accelerator is Ctrl.
  it("Ctrl+\\ splits the tab in front", async () => {
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    fireEvent.keyDown(tab("a.md"), { key: "\\", ctrlKey: true });
    await waitFor(() => expect(entry().groups).toHaveLength(3));
  });

  it("Ctrl+W closes the tab in front, and never the folder browser", async () => {
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    fireEvent.keyDown(tab("a.md"), { key: "w", ctrlKey: true });
    await waitFor(() => expect(entry().tabs.some((t) => t.id === "t-a")).toBe(false));
    act(() => useWorkspaceStore.getState().activate(CHAT_ID, "files"));
    fireEvent.keyDown(tab("Files"), { key: "w", ctrlKey: true });
    expect(entry().tabs.some((t) => t.id === "files")).toBe(true);
  });

  it("Ctrl+2 puts the reader in the second group and the keyboard on its tab", async () => {
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    fireEvent.keyDown(tab("a.md"), { key: "2", ctrlKey: true });
    await waitFor(() => expect(entry().activeGroupId).toBe("g-right"));
    await waitFor(() => expect(document.activeElement).toBe(tab("c.md")));
  });

  it("closing a group's last tab collapses the group", async () => {
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    fireEvent.click(within(group(2)).getByRole("button", { name: "Close c.md" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Editor group 2" })).toBeNull());
    expect(screen.queryByRole("separator")).toBeNull();
    expect(screen.getByRole("region", { name: "Editor" })).toBeInTheDocument();
  });
});

describe("a preview tab", () => {
  it("is drawn in italics, and kept by a double click", async () => {
    stored = {
      ...STORED,
      tabs: STORED.tabs.map((t) => (t.id === "t-b" ? { ...t, transient: true } : t)),
    };
    mount();
    await screen.findByRole("region", { name: "Editor group 2" });
    expect(tab("b.md")).toHaveClass("alk-tabstrip__tab--transient");
    fireEvent.doubleClick(tab("b.md"));
    await waitFor(() => expect(tab("b.md")).not.toHaveClass("alk-tabstrip__tab--transient"));
    expect(entry().tabs.find((t) => t.id === "t-b")?.transient).toBe(false);
  });
});
