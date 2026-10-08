// Editor groups in the stored workspace: splitting, moving, closing, preview
// tabs, and the document they are written into and read back from.
//
// Driven through the store's real transport with `fetch` stubbed, so what is
// asserted about persistence is the document that actually goes over the wire
// and what the store makes of the document that comes back.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_GROUP_ID, groupOrder } from "@/pages/workspace/chat/workspace/editorLayout";
import type { WorkspaceTab } from "@/pages/workspace/chat/workspace/tabKinds";
import {
  FILES_TAB_ID,
  LAYOUT_VERSION,
  fileTargetGroup,
  flushWorkspace,
  forgetChatPane,
  previewsBesideBrowser,
  tabsOf,
  useWorkspaceStore,
  type ChatWorkspaceDoc,
} from "@/pages/workspace/chat/workspace/workspaceStore";

const CHAT = "chat-groups";

function uuid(n: number): string {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

const ref = (n: number, name = `file-${n}.md`) => ({ nodeId: uuid(n), name, path: name });

let fetchSpy: ReturnType<typeof vi.fn>;

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: () => Promise.resolve(body) } as unknown as Response;
}

function entry() {
  const current = useWorkspaceStore.getState().chats[CHAT];
  if (!current) throw new Error("no workspace for the chat");
  return current;
}

const store = () => useWorkspaceStore.getState();

function lastWrite(): ChatWorkspaceDoc {
  const call = fetchSpy.mock.calls.at(-1);
  if (!call) throw new Error("nothing was written");
  return (JSON.parse(String((call[1] as RequestInit).body)) as { state: ChatWorkspaceDoc }).state;
}

/** The nodes each group shows, in reading order. */
function shape(): (string | null | undefined)[][] {
  const current = entry();
  return groupOrder(current.layout).map((id) => tabsOf(current, id).map((tab) => tab.node_id ?? tab.id));
}

beforeEach(() => {
  vi.useFakeTimers();
  fetchSpy = vi.fn().mockImplementation(() => Promise.resolve(ok({ state: { tabs: [] }, updated_at: null })));
  vi.stubGlobal("fetch", fetchSpy);
  store().hydrate(CHAT, { tabs: [], active_tab_id: null });
});

afterEach(() => {
  forgetChatPane(CHAT);
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("splitting", () => {
  it("shows the tab in front a second time in a new group to its right, and puts the reader there", () => {
    store().openFileTab(CHAT, ref(1));
    store().splitTab(CHAT, "right");

    expect(shape()).toEqual([[FILES_TAB_ID, uuid(1)], [uuid(1)]]);
    const [left, right] = groupOrder(entry().layout);
    expect(entry().activeGroupId).toBe(right);
    // Two tabs on one file, never the same tab twice.
    const ids = entry().tabs.filter((tab) => tab.node_id === uuid(1)).map((tab) => tab.id);
    expect(new Set(ids).size).toBe(2);
    expect(left).toBe(DEFAULT_GROUP_ID);
  });

  it("splits down into a column", () => {
    store().openFileTab(CHAT, ref(1));
    store().splitTab(CHAT, "down");
    expect((entry().layout as { axis?: string }).axis).toBe("column");
  });

  it("never copies the folder browser", () => {
    store().splitTab(CHAT, "right", FILES_TAB_ID);
    expect(entry().groups).toHaveLength(1);
  });

  it("stops at four groups", () => {
    store().openFileTab(CHAT, ref(1));
    for (let index = 0; index < 6; index += 1) store().splitTab(CHAT, "right");
    expect(entry().groups).toHaveLength(4);
  });
});

describe("opening a preview to the side", () => {
  it("makes the group to the right when there is none, showing the file as its preview", () => {
    store().openFileTab(CHAT, ref(1));
    store().openToSide(CHAT, ref(1), { view: "preview" });

    expect(shape()).toEqual([[FILES_TAB_ID, uuid(1)], [uuid(1)]]);
    const right = groupOrder(entry().layout)[1] as string;
    expect(tabsOf(entry(), right)[0]?.view).toBe("preview");
  });

  it("reuses the group to the right, and the preview tab already in it", () => {
    store().openFileTab(CHAT, ref(1));
    store().openToSide(CHAT, ref(1), { view: "preview", fromGroup: DEFAULT_GROUP_ID });
    store().openToSide(CHAT, ref(1), { view: "preview", fromGroup: DEFAULT_GROUP_ID });
    store().openToSide(CHAT, ref(2), { view: "preview", fromGroup: DEFAULT_GROUP_ID });

    expect(entry().groups).toHaveLength(2);
    expect(shape()[1]).toEqual([uuid(1), uuid(2)]);
  });
});

describe("closing", () => {
  it("collapses a group with its last tab, and puts the reader in the group that takes its space", () => {
    store().openFileTab(CHAT, ref(1));
    store().splitTab(CHAT, "right");
    const right = entry().activeTabId as string;

    store().closeTab(CHAT, right);

    expect(entry().groups).toHaveLength(1);
    expect(entry().layout).toEqual({ kind: "group", id: DEFAULT_GROUP_ID });
    expect(entry().activeGroupId).toBe(DEFAULT_GROUP_ID);
  });

  it("leaves the reader on the right-hand neighbour within the group", () => {
    store().openFileTab(CHAT, ref(1));
    store().openFileTab(CHAT, ref(2));
    store().openFileTab(CHAT, ref(3));
    const second = entry().tabs.find((tab) => tab.node_id === uuid(2))?.id as string;
    store().activate(CHAT, second);
    store().closeTab(CHAT, second);
    expect(entry().tabs.find((tab) => tab.id === entry().activeTabId)?.node_id).toBe(uuid(3));
  });
});

describe("moving a tab", () => {
  function twoGroups(): { left: string; right: string } {
    store().openFileTab(CHAT, ref(1));
    store().openFileTab(CHAT, ref(2));
    store().openToSide(CHAT, ref(3), { fromGroup: DEFAULT_GROUP_ID });
    const [left, right] = groupOrder(entry().layout) as [string, string];
    return { left, right };
  }

  it("into another group's strip, at the place it was dropped", () => {
    const { right } = twoGroups();
    const tab = entry().tabs.find((candidate) => candidate.node_id === uuid(1)) as WorkspaceTab;

    store().moveTab(CHAT, tab.id, { group: right, index: 0 });

    expect(shape()).toEqual([[FILES_TAB_ID, uuid(2)], [uuid(1), uuid(3)]]);
    expect(entry().activeTabId).toBe(tab.id);
    expect(entry().activeGroupId).toBe(right);
  });

  it("along its own strip, never ahead of the pinned browser", () => {
    twoGroups();
    const tab = entry().tabs.find((candidate) => candidate.node_id === uuid(2)) as WorkspaceTab;
    store().moveTab(CHAT, tab.id, { group: DEFAULT_GROUP_ID, index: 0 });
    expect(shape()[0]).toEqual([FILES_TAB_ID, uuid(2), uuid(1)]);
  });

  it("onto a group's edge, which splits it", () => {
    const { right } = twoGroups();
    const tab = entry().tabs.find((candidate) => candidate.node_id === uuid(1)) as WorkspaceTab;

    store().moveTab(CHAT, tab.id, { group: right, split: "down" });

    expect(shape()).toEqual([[FILES_TAB_ID, uuid(2)], [uuid(3)], [uuid(1)]]);
    const layout = entry().layout;
    expect(layout.kind === "split" && layout.children[1]?.kind === "split" && layout.children[1].axis).toBe("column");
  });

  it("out of a group it was alone in, which closes that group", () => {
    const { left, right } = twoGroups();
    const only = tabsOf(entry(), right)[0] as WorkspaceTab;
    store().moveTab(CHAT, only.id, { group: left });
    expect(entry().groups.map((group) => group.id)).toEqual([left]);
  });

  it("onto a group already showing that file in that view, which keeps the tab already there", () => {
    const { right } = twoGroups();
    store().openFileTab(CHAT, ref(1), { group: right });
    const leftCopy = tabsOf(entry(), DEFAULT_GROUP_ID).find((tab) => tab.node_id === uuid(1)) as WorkspaceTab;

    store().moveTab(CHAT, leftCopy.id, { group: right });

    expect(entry().tabs.filter((tab) => tab.node_id === uuid(1))).toHaveLength(1);
    expect(tabsOf(entry(), right).map((tab) => tab.node_id)).toEqual([uuid(3), uuid(1)]);
  });

  it("is refused when a split would make a fifth group", () => {
    store().openFileTab(CHAT, ref(1));
    store().openFileTab(CHAT, ref(2));
    for (let index = 0; index < 3; index += 1) store().splitTab(CHAT, "right", entry().tabs.find((tab) => tab.node_id === uuid(1))?.id);
    expect(entry().groups).toHaveLength(4);
    const loose = tabsOf(entry(), DEFAULT_GROUP_ID).find((tab) => tab.node_id === uuid(2)) as WorkspaceTab;
    store().moveTab(CHAT, loose.id, { group: DEFAULT_GROUP_ID, split: "down" });
    expect(entry().groups).toHaveLength(4);
  });
});

describe("preview tabs", () => {
  it("are replaced by the next one opened the same way, in place", () => {
    store().openFileTab(CHAT, ref(1));
    store().openFileTab(CHAT, ref(2), { transient: true });
    store().openFileTab(CHAT, ref(3), { transient: true });

    expect(shape()).toEqual([[FILES_TAB_ID, uuid(1), uuid(3)]]);
    expect(entry().tabs.find((tab) => tab.node_id === uuid(3))?.transient).toBe(true);
  });

  it("are kept by a pin, after which the next preview opens beside them", () => {
    store().openFileTab(CHAT, ref(2), { transient: true });
    store().pinTab(CHAT, entry().activeTabId as string);
    store().openFileTab(CHAT, ref(3), { transient: true });
    expect(shape()).toEqual([[FILES_TAB_ID, uuid(2), uuid(3)]]);
  });

  it("are kept by opening the same file for keeps", () => {
    store().openFileTab(CHAT, ref(2), { transient: true });
    store().openFileTab(CHAT, ref(2));
    const tab = entry().tabs.find((candidate) => candidate.node_id === uuid(2));
    expect(tab?.transient).toBe(false);
    expect(entry().tabs.filter((candidate) => candidate.node_id === uuid(2))).toHaveLength(1);
  });

  it("are never replaced across groups", () => {
    store().openFileTab(CHAT, ref(1));
    store().openToSide(CHAT, ref(9), {});
    const right = entry().activeGroupId;
    store().openFileTab(CHAT, ref(2), { transient: true, group: DEFAULT_GROUP_ID });
    store().openFileTab(CHAT, ref(3), { transient: true, group: right });
    expect(entry().tabs.filter((tab) => tab.transient).map((tab) => tab.node_id).sort()).toEqual([uuid(2), uuid(3)]);
  });
});

describe("where a file from the browser opens", () => {
  it("is the browser's own group when it is the only one, so a click only selects", () => {
    expect(previewsBesideBrowser(entry())).toBe(false);
    expect(fileTargetGroup(entry())).toBe(DEFAULT_GROUP_ID);
  });

  it("is the group beside the browser once there is one, so a click previews there", () => {
    store().openFileTab(CHAT, ref(1));
    store().splitTab(CHAT, "right");
    // The reader goes back to the browser in the first group.
    store().activate(CHAT, FILES_TAB_ID);

    expect(previewsBesideBrowser(entry())).toBe(true);
    const right = groupOrder(entry().layout)[1];
    expect(fileTargetGroup(entry())).toBe(right);
    store().openFileTab(CHAT, ref(2), { transient: true });
    expect(tabsOf(entry(), right as string).map((tab) => tab.node_id)).toContain(uuid(2));
    // The browser is still in front of its own group.
    expect(entry().groups.find((group) => group.id === DEFAULT_GROUP_ID)?.activeTabId).toBe(FILES_TAB_ID);
  });
});

describe("the stored layout", () => {
  function arrange(): void {
    store().openFileTab(CHAT, ref(1));
    store().openFileTab(CHAT, ref(2));
    store().openToSide(CHAT, ref(1), { view: "preview", fromGroup: DEFAULT_GROUP_ID });
    store().splitTab(CHAT, "down");
    store().resizeSplit(CHAT, [], [0.7, 0.3]);
    store().openFileTab(CHAT, ref(3), { transient: true });
  }

  it("round-trips: what is written reads back as the same workspace", async () => {
    arrange();
    const before = entry();
    await vi.runAllTimersAsync();
    const doc = lastWrite();

    expect(doc.layout?.v).toBe(LAYOUT_VERSION);
    // Every tab says which group it is in, so a reader that loses the layout
    // can still put each tab back where it was.
    expect(doc.tabs.every((tab) => typeof tab.group === "string")).toBe(true);
    // An older build reads only this: the tab in front of the active group.
    expect(doc.active_tab_id).toBe(before.activeTabId);

    forgetChatPane(CHAT);
    store().hydrate(CHAT, JSON.parse(JSON.stringify(doc)) as ChatWorkspaceDoc);
    const after = entry();
    expect(after.layout).toEqual(before.layout);
    expect(after.groups).toEqual(before.groups);
    expect(after.activeGroupId).toBe(before.activeGroupId);
    expect(after.tabs.map(({ id, group, view, transient, node_id }) => ({ id, group, view, transient: transient === true, node_id }))).toEqual(
      before.tabs.map(({ id, group, view, transient, node_id }) => ({ id, group, view, transient: transient === true, node_id })),
    );
  });

  it("is written when only the arrangement changed", async () => {
    arrange();
    await vi.runAllTimersAsync();
    const writes = fetchSpy.mock.calls.length;
    store().resizeSplit(CHAT, [], [0.4, 0.6]);
    await vi.runAllTimersAsync();
    expect(fetchSpy.mock.calls.length).toBe(writes + 1);
    expect(lastWrite().layout?.root).toMatchObject({ sizes: [0.4, 0.6] });
  });

  it("loads a document written before groups existed into one group", () => {
    forgetChatPane(CHAT);
    store().hydrate(CHAT, {
      tabs: [
        { id: "files", kind: "files", name: "Files", params: { folderId: uuid(9) } },
        { id: "t1", kind: "file", node_id: uuid(1), name: "a.md" },
        { id: "t2", kind: "file", node_id: uuid(2), name: "b.md" },
      ],
      active_tab_id: "t2",
    });
    expect(entry().groups).toEqual([{ id: DEFAULT_GROUP_ID, activeTabId: "t2" }]);
    expect(shape()).toEqual([[FILES_TAB_ID, uuid(1), uuid(2)]]);
    expect(entry().tabs[0]?.params).toEqual({ folderId: uuid(9) });
  });

  it("puts each tab back in its group when an older build wrote the document without the layout", async () => {
    arrange();
    await vi.runAllTimersAsync();
    const doc = lastWrite();
    // An older build keeps every field of a tab and drops the layout beside them.
    const older: ChatWorkspaceDoc = { tabs: doc.tabs, active_tab_id: doc.active_tab_id };
    const groupsBefore = entry().groups.length;

    forgetChatPane(CHAT);
    store().hydrate(CHAT, older);

    expect(entry().groups).toHaveLength(groupsBefore);
    for (const tab of doc.tabs) {
      expect(entry().tabs.find((candidate) => candidate.id === tab.id)?.group).toBe(tab.group);
    }
  });

  it("puts a tab an older build opened into the group the reader was working in", () => {
    forgetChatPane(CHAT);
    store().hydrate(CHAT, {
      tabs: [
        { id: "files", kind: "files", name: "Files", group: "g-a" },
        { id: "t1", kind: "file", node_id: uuid(1), name: "a.md", group: "g-a" },
        { id: "t2", kind: "file", node_id: uuid(2), name: "b.md", group: "g-b" },
        { id: "t3", kind: "file", node_id: uuid(3), name: "c.md" },
      ],
      active_tab_id: "t3",
      layout: {
        v: LAYOUT_VERSION,
        root: { split: "row", children: [{ g: "g-a" }, { g: "g-b" }], sizes: [0.5, 0.5] },
        active_group: "g-b",
        active: { "g-a": "t1", "g-b": "t2" },
      },
    });
    expect(entry().tabs.find((tab) => tab.id === "t3")?.group).toBe("g-b");
    expect(entry().activeTabId).toBe("t3");
  });

  it("reads a layout of a version it does not know as absent, and lays the tabs' groups out in a row", () => {
    forgetChatPane(CHAT);
    store().hydrate(CHAT, {
      tabs: [
        { id: "files", kind: "files", name: "Files", group: "g-a" },
        { id: "t1", kind: "file", node_id: uuid(1), name: "a.md", group: "g-b" },
      ],
      active_tab_id: "t1",
      layout: { v: 99, root: { g: "g-zzz" }, active_group: "g-zzz" } as unknown as ChatWorkspaceDoc["layout"],
    });
    expect(groupOrder(entry().layout)).toEqual(["g-a", "g-b"]);
    expect((entry().layout as { axis?: string }).axis).toBe("row");
  });

  it("keeps a field a newer build left on a tab", async () => {
    forgetChatPane(CHAT);
    store().hydrate(CHAT, {
      tabs: [{ id: "t1", kind: "file", node_id: uuid(1), name: "a.md", pinnedBy: "newer" } as unknown as ChatWorkspaceDoc["tabs"][number]],
      active_tab_id: "t1",
    });
    store().openFileTab(CHAT, ref(2));
    await flushWorkspace(CHAT);
    expect((lastWrite().tabs.find((tab) => tab.id === "t1") as unknown as Record<string, unknown>).pinnedBy).toBe("newer");
  });
});
