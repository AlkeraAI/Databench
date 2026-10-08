import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MAX_WORKSPACE_TABS, type WorkspaceTab } from "@/pages/workspace/chat/workspace/tabKinds";
import {
  FILES_TAB_ID,
  SAVE_DEBOUNCE_MS,
  TRIM_NOTICE,
  flushWorkspace,
  forgetChatPane,
  useWorkspaceStore,
  type ChatWorkspaceDoc,
} from "@/pages/workspace/chat/workspace/workspaceStore";

// The tabs a reader leaves open beside a chat, and what the browser does with
// them when it comes back to find the chat has moved on.
//
// Everything here is driven through the store's real transport with `fetch`
// stubbed, because the interesting behaviour IS the wire: which document is
// written, how many times, whether the write survives the page going away, and
// what the client does when the server says the document is too big. A save
// asserted against a substituted saver would prove none of that.

const CHAT = "chat-4b2";
const ROOT = "/home/dana/Chats/Q3.alkerachat/scratch";

let fetchSpy: ReturnType<typeof vi.fn>;

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: () => Promise.resolve(body) } as unknown as Response;
}

function refused(status: number, body: unknown): Response {
  return { ok: false, status, json: () => Promise.resolve(body) } as unknown as Response;
}

/** The chat's own saved-workspace answer, echoed back the way the route does. */
function saved(state: ChatWorkspaceDoc): Response {
  return ok({ state, updated_at: "2026-09-16T10:00:00Z" });
}

/** The document as the route hands it back: it is parsed by the model that
 *  stores it, so the answer to a write is not the bytes that were written — it
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

function writes(): { url: string; init: RequestInit; doc: ChatWorkspaceDoc }[] {
  return fetchSpy.mock.calls.map((call) => {
    const init = call[1] as RequestInit;
    return {
      url: String(call[0]),
      init,
      doc: (JSON.parse(String(init.body)) as { state: ChatWorkspaceDoc }).state,
    };
  });
}

function entry() {
  return useWorkspaceStore.getState().chats[CHAT];
}

function tabIds(): string[] {
  return entry().tabs.map((tab) => tab.id);
}

function nodeIds(): (string | null | undefined)[] {
  return entry().tabs.filter((tab) => tab.kind === "file").map((tab) => tab.node_id);
}

function fileTab(nodeId: string, name: string): WorkspaceTab {
  return { id: `tab-${nodeId}`, kind: "file", node_id: nodeId, name, path: name };
}

function uuid(n: number): string {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

beforeEach(() => {
  vi.useFakeTimers();
  fetchSpy = vi.fn().mockImplementation(() => Promise.resolve(saved({ tabs: [], active_tab_id: null })));
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  forgetChatPane(CHAT);
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("re-opening a chat", () => {
  it("draws the tabs that were stored, with the one that was in front active", () => {
    useWorkspaceStore.getState().hydrate(CHAT, {
      tabs: [fileTab(uuid(1), "revenue.csv"), fileTab(uuid(2), "q3.html")],
      active_tab_id: `tab-${uuid(2)}`,
    });

    // The folder browser is part of the workspace, not a preference, so it is
    // there whether or not the stored document mentioned it.
    expect(tabIds()).toEqual([FILES_TAB_ID, `tab-${uuid(1)}`, `tab-${uuid(2)}`]);
    expect(entry().activeTabId).toBe(`tab-${uuid(2)}`);
    // Reading the server's own document is not a change to save.
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS * 2);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("keeps a tab this build has never heard of rather than dropping it", () => {
    useWorkspaceStore.getState().hydrate(CHAT, {
      tabs: [{ id: "t-notebook", kind: "notebook", name: "model.ipynb" }],
      active_tab_id: "t-notebook",
    });

    expect(tabIds()).toContain("t-notebook");
  });

  it("falls back to the folder browser when the stored active tab is gone", () => {
    useWorkspaceStore.getState().hydrate(CHAT, {
      tabs: [fileTab(uuid(1), "revenue.csv")],
      active_tab_id: "tab-that-was-closed-elsewhere",
    });

    expect(entry().activeTabId).toBe(FILES_TAB_ID);
  });
});

describe("a stored tab whose file has moved on", () => {
  beforeEach(() => {
    useWorkspaceStore.getState().hydrate(CHAT, {
      tabs: [
        fileTab(uuid(1), "revenue.csv"),
        fileTab(uuid(2), "drafts"),
        fileTab(uuid(3), "elsewhere.md"),
        fileTab(uuid(4), "q3.html"),
      ],
      active_tab_id: `tab-${uuid(4)}`,
    });
  });

  it("drops the node the server will not hand back, and writes the shorter document", async () => {
    useWorkspaceStore.getState().prune(CHAT, {
      rootPathBytes: ROOT,
      nodes: {
        [uuid(1)]: { present: false },
        [uuid(4)]: { present: true, kind: "file", pathBytes: `${ROOT}/q3.html` },
      },
    });

    expect(nodeIds()).toEqual([uuid(2), uuid(3), uuid(4)]);

    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS);
    await vi.runAllTimersAsync();
    const [write] = writes();
    expect(write.url).toContain(`/api/v1/chats/${CHAT}/workspace`);
    expect(write.init.method).toBe("PUT");
    expect(write.doc.tabs.map((tab) => tab.node_id)).toEqual([undefined, uuid(2), uuid(3), uuid(4)]);
  });

  it("drops a node that turned out to be a folder", () => {
    useWorkspaceStore.getState().prune(CHAT, {
      rootPathBytes: ROOT,
      nodes: { [uuid(2)]: { present: true, kind: "folder", pathBytes: `${ROOT}/drafts` } },
    });

    expect(nodeIds()).not.toContain(uuid(2));
  });

  it("drops a node that is no longer inside the chat's own folder", () => {
    useWorkspaceStore.getState().prune(CHAT, {
      rootPathBytes: ROOT,
      nodes: {
        [uuid(3)]: { present: true, kind: "file", pathBytes: "/home/dana/elsewhere.md" },
        [uuid(4)]: { present: true, kind: "file", pathBytes: `${ROOT}/q3.html` },
      },
    });

    expect(nodeIds()).not.toContain(uuid(3));
    expect(nodeIds()).toContain(uuid(4));
  });

  it("is not fooled by a sibling folder whose name starts with the chat's", () => {
    useWorkspaceStore.getState().prune(CHAT, {
      rootPathBytes: ROOT,
      nodes: { [uuid(1)]: { present: true, kind: "file", pathBytes: `${ROOT}-archive/revenue.csv` } },
    });

    expect(nodeIds()).not.toContain(uuid(1));
  });

  it("leaves a tab alone while the node's facts are still unknown", () => {
    useWorkspaceStore.getState().prune(CHAT, { rootPathBytes: ROOT, nodes: {} });

    expect(nodeIds()).toEqual([uuid(1), uuid(2), uuid(3), uuid(4)]);
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS * 2);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("moves the reader to the folder browser when the tab in front is the one that died", () => {
    useWorkspaceStore.getState().prune(CHAT, {
      rootPathBytes: ROOT,
      nodes: { [uuid(4)]: { present: false } },
    });

    expect(entry().activeTabId).toBe(FILES_TAB_ID);
  });

  it("keeps a tab whose file went while the reader had it open", () => {
    const store = useWorkspaceStore.getState();
    // Coming back to the chat, the file was there.
    store.prune(CHAT, {
      rootPathBytes: ROOT,
      nodes: { [uuid(1)]: { present: true, kind: "file", pathBytes: `${ROOT}/revenue.csv` } },
    });

    // And then somebody trashed it while the tab was open.
    store.prune(CHAT, { rootPathBytes: ROOT, nodes: { [uuid(1)]: { present: false } } });

    // Saying so is the tab's job, and a tab that is not there cannot say it —
    // nor offer to restore the file.
    expect(nodeIds()).toContain(uuid(1));
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS * 2);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("waits for the chat's own folder before deciding a file has left it", () => {
    const store = useWorkspaceStore.getState();
    const moved = { present: true, kind: "file", pathBytes: "/home/dana/elsewhere.md" } as const;
    // The node answered before the chat's folder did. Its path cannot be
    // measured against a root nobody has read yet, so nothing is settled.
    store.prune(CHAT, { rootPathBytes: undefined, nodes: { [uuid(3)]: moved } });
    expect(nodeIds()).toContain(uuid(3));

    store.prune(CHAT, { rootPathBytes: ROOT, nodes: { [uuid(3)]: moved } });

    expect(nodeIds()).not.toContain(uuid(3));
  });
});

describe("opening a file", () => {
  beforeEach(() => {
    useWorkspaceStore.getState().hydrate(CHAT, { tabs: [], active_tab_id: null });
  });

  it("brings the tab already showing that node to the front instead of opening a second", () => {
    const store = useWorkspaceStore.getState();
    store.openFileTab(CHAT, { nodeId: uuid(1), name: "revenue.csv", path: "revenue.csv" });
    const opened = entry().activeTabId;
    store.activate(CHAT, FILES_TAB_ID);

    store.openFileTab(CHAT, { nodeId: uuid(1), name: "revenue.csv", path: "revenue.csv" });

    expect(entry().tabs.filter((tab) => tab.node_id === uuid(1))).toHaveLength(1);
    expect(entry().activeTabId).toBe(opened);
  });

  it("closes the oldest tab the reader is not looking at when the strip is full", () => {
    const store = useWorkspaceStore.getState();
    for (let n = 1; n <= MAX_WORKSPACE_TABS + 2; n += 1) {
      store.openFileTab(CHAT, { nodeId: uuid(n), name: `f${n}.txt` });
    }

    expect(entry().tabs).toHaveLength(MAX_WORKSPACE_TABS);
    expect(entry().tabs[0]?.id).toBe(FILES_TAB_ID);
    expect(nodeIds()).not.toContain(uuid(1));
    expect(nodeIds()).toContain(uuid(MAX_WORKSPACE_TABS + 2));
  });

  it("refuses to close the folder browser", () => {
    useWorkspaceStore.getState().closeTab(CHAT, FILES_TAB_ID);

    expect(tabIds()).toContain(FILES_TAB_ID);
  });
});

describe("writing the workspace back", () => {
  beforeEach(() => {
    useWorkspaceStore.getState().hydrate(CHAT, { tabs: [], active_tab_id: null });
  });

  it("writes once for a burst of changes, carrying all of them", async () => {
    const store = useWorkspaceStore.getState();
    store.openFileTab(CHAT, { nodeId: uuid(1), name: "a.txt" });
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS - 100);
    store.openFileTab(CHAT, { nodeId: uuid(2), name: "b.txt" });
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS - 100);
    store.openFileTab(CHAT, { nodeId: uuid(3), name: "c.txt" });

    await vi.runAllTimersAsync();

    const all = writes();
    expect(all).toHaveLength(1);
    expect(all[0]?.doc.tabs.map((tab) => tab.node_id)).toEqual([undefined, uuid(1), uuid(2), uuid(3)]);
    expect(all[0]?.doc.active_tab_id).toBe(entry().activeTabId);
  });

  it("records which folder the browser is showing", async () => {
    useWorkspaceStore.getState().setFilesFolder(CHAT, uuid(9));

    await vi.runAllTimersAsync();

    const [write] = writes();
    expect(write.doc.tabs.find((tab) => tab.id === FILES_TAB_ID)?.params).toEqual({ folderId: uuid(9) });
  });

  it("gets the pending write out before the page goes away, and lets it outlive the page", async () => {
    useWorkspaceStore.getState().openFileTab(CHAT, { nodeId: uuid(1), name: "a.txt" });
    expect(fetchSpy).not.toHaveBeenCalled();

    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    await Promise.resolve();

    const [write] = writes();
    expect(write.init.keepalive).toBe(true);
    expect(write.doc.tabs.map((tab) => tab.node_id)).toEqual([undefined, uuid(1)]);

    // The debounce timer must not fire a second, identical write afterwards.
    await vi.runAllTimersAsync();
    expect(writes()).toHaveLength(1);
  });

  it("writes nothing on the way out when nothing changed", async () => {
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.runAllTimersAsync();

    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("flushes on the way out of a chat", async () => {
    useWorkspaceStore.getState().openFileTab(CHAT, { nodeId: uuid(1), name: "a.txt" });

    await flushWorkspace(CHAT, { keepalive: true });

    expect(writes()).toHaveLength(1);
    expect(writes()[0]?.init.keepalive).toBe(true);
  });

  it("forgets a chat that no longer exists without writing to it", async () => {
    useWorkspaceStore.getState().openFileTab(CHAT, { nodeId: uuid(1), name: "a.txt" });

    forgetChatPane(CHAT);
    await vi.runAllTimersAsync();

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(useWorkspaceStore.getState().chats[CHAT]).toBeUndefined();
  });
});

describe("a workspace the server says is too big", () => {
  const TOO_BIG = { detail: { code: "workspace_state_too_large", message: "Close some tabs" } };

  function openMany(count: number): void {
    const store = useWorkspaceStore.getState();
    store.hydrate(CHAT, { tabs: [], active_tab_id: null });
    for (let n = 1; n <= count; n += 1) {
      store.openFileTab(CHAT, { nodeId: uuid(n), name: `f${n}.txt` });
    }
  }

  it("closes the oldest tabs the reader is not in and writes again, once", async () => {
    fetchSpy
      .mockImplementationOnce(() => Promise.resolve(refused(422, TOO_BIG)))
      .mockImplementation(() => Promise.resolve(saved({ tabs: [], active_tab_id: null })));
    openMany(6);

    await vi.runAllTimersAsync();

    const all = writes();
    expect(all).toHaveLength(2);
    expect(all[0]?.doc.tabs).toHaveLength(7);
    // The oldest tab the reader is not looking at goes; the browser and the tab
    // in front stay.
    expect(all[1]?.doc.tabs.map((tab) => tab.id)).toEqual(
      all[0]?.doc.tabs.filter((tab) => tab.node_id !== uuid(1)).map((tab) => tab.id),
    );
    expect(all[1]?.doc.tabs[0]?.id).toBe(FILES_TAB_ID);
    expect(all[1]?.doc.active_tab_id).toBe(all[0]?.doc.active_tab_id);
    expect(nodeIds()).not.toContain(uuid(1));
    expect(entry().notice).toBe(TRIM_NOTICE);
  });

  it("stops after the one retry rather than closing tabs until the strip is empty", async () => {
    fetchSpy.mockImplementation(() => Promise.resolve(refused(422, TOO_BIG)));
    openMany(6);

    await vi.runAllTimersAsync();

    expect(writes()).toHaveLength(2);
    expect(nodeIds()).toHaveLength(5);
  });

  it("does not write the shorter document to a chat deleted while the refusal was in flight", async () => {
    let refuse = (): void => undefined;
    fetchSpy
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            refuse = () => resolve(refused(422, TOO_BIG));
          }),
      )
      .mockImplementation(() => Promise.resolve(saved({ tabs: [], active_tab_id: null })));
    openMany(6);

    // The write is out and the server has not answered yet.
    await vi.runAllTimersAsync();
    expect(writes()).toHaveLength(1);

    // The chat is deleted while it is in flight, and only then does "too big"
    // come back. Closing tabs and writing again would be a PUT to a chat the
    // server no longer has.
    forgetChatPane(CHAT);
    refuse();
    await vi.runAllTimersAsync();

    expect(writes()).toHaveLength(1);
  });

  it("keeps the tabs on screen when the write fails for any other reason", async () => {
    fetchSpy.mockImplementation(() => Promise.resolve(refused(503, { detail: "unavailable" })));
    openMany(3);

    await vi.runAllTimersAsync();

    expect(writes()).toHaveLength(1);
    expect(nodeIds()).toEqual([uuid(1), uuid(2), uuid(3)]);
    expect(entry().notice).toBeNull();
  });
});

describe("what the reader has already seen", () => {
  beforeEach(() => {
    useWorkspaceStore.getState().hydrate(CHAT, {
      tabs: [fileTab(uuid(1), "a.txt"), fileTab(uuid(2), "b.txt")],
      active_tab_id: `tab-${uuid(2)}`,
    });
  });

  it("marks a tab the reader is not in when its file changes, and clears it on arrival", () => {
    const store = useWorkspaceStore.getState();
    store.markUpdated(CHAT, uuid(1));
    expect(entry().updated).toContain(`tab-${uuid(1)}`);

    store.activate(CHAT, `tab-${uuid(1)}`);
    expect(entry().updated).not.toContain(`tab-${uuid(1)}`);
  });

  it("does not mark the tab the reader is already looking at", () => {
    useWorkspaceStore.getState().markUpdated(CHAT, uuid(2));

    expect(entry().updated).toEqual([]);
  });

  it("marks a file that died while its tab was open, and keeps the tab", () => {
    useWorkspaceStore.getState().markGone(CHAT, uuid(1));

    expect(entry().gone).toContain(`tab-${uuid(1)}`);
    expect(nodeIds()).toContain(uuid(1));
  });

  it("takes the marker off again when the node answers for itself", () => {
    const store = useWorkspaceStore.getState();
    store.markGone(CHAT, uuid(1));

    // The reader put the file back from the tab's own Restore button.
    store.markPresent(CHAT, uuid(1));

    expect(entry().gone).toEqual([]);
    // A marker is what this browser noticed, not part of the layout.
    vi.advanceTimersByTime(SAVE_DEBOUNCE_MS * 2);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("the answer to this browser's own save", () => {
  beforeEach(() => {
    fetchSpy.mockImplementation((_url: string, init: RequestInit) =>
      Promise.resolve(
        saved(stored((JSON.parse(String(init.body)) as { state: ChatWorkspaceDoc }).state)),
      ),
    );
    useWorkspaceStore.getState().hydrate(CHAT, { tabs: [], active_tab_id: null });
  });

  it("is not read back over what the reader did while the write was in flight", async () => {
    const store = useWorkspaceStore.getState();
    store.openFileTab(CHAT, { nodeId: uuid(1), name: "a.txt" });
    const first = entry().activeTabId;
    await vi.runAllTimersAsync();
    const [write] = writes();

    // The write is gone; the reader opens a second file and leaves the first,
    // whose bytes then change behind them.
    store.openFileTab(CHAT, { nodeId: uuid(2), name: "b.txt" });
    store.activate(CHAT, FILES_TAB_ID);
    store.markUpdated(CHAT, uuid(1));

    // Only now does the answer to the first write arrive, carrying the document
    // as it was BEFORE any of that.
    store.hydrate(CHAT, stored(write?.doc as ChatWorkspaceDoc));

    expect(nodeIds()).toEqual([uuid(1), uuid(2)]);
    expect(entry().updated).toEqual([first]);
    // And the next write carries both tabs rather than the shorter document.
    store.setFilesFolder(CHAT, uuid(9));
    await vi.runAllTimersAsync();
    expect(
      writes()
        .at(-1)
        ?.doc.tabs.map((tab) => tab.node_id),
    ).toEqual([undefined, uuid(1), uuid(2)]);
  });

  it("does not take back the tab opened during the second write when the first is answered", async () => {
    // Two saves out at once is what a person clicking faster than a round trip
    // produces, and the FIRST write's answer carries the document from before
    // the second tab was opened.
    const outstanding: { doc: ChatWorkspaceDoc; answer: () => void }[] = [];
    fetchSpy.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise<Response>((resolve) => {
          const doc = (JSON.parse(String(init.body)) as { state: ChatWorkspaceDoc }).state;
          outstanding.push({ doc, answer: () => resolve(saved(stored(doc))) });
        }),
    );
    const store = useWorkspaceStore.getState();

    store.openFileTab(CHAT, { nodeId: uuid(1), name: "a.txt" });
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS);
    store.openFileTab(CHAT, { nodeId: uuid(2), name: "b.txt" });
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS);
    expect(outstanding).toHaveLength(2);

    // The answers come back the other way round, and each seeds the layout the
    // page reads.
    outstanding[0]?.answer();
    await vi.advanceTimersByTimeAsync(0);
    store.hydrate(CHAT, stored(outstanding[0]?.doc as ChatWorkspaceDoc));
    expect(nodeIds()).toEqual([uuid(1), uuid(2)]);

    outstanding[1]?.answer();
    await vi.advanceTimersByTimeAsync(0);
    store.hydrate(CHAT, stored(outstanding[1]?.doc as ChatWorkspaceDoc));
    expect(nodeIds()).toEqual([uuid(1), uuid(2)]);

    // And the browser is not left writing the older document back over the
    // newer one for the life of the page.
    store.setFilesFolder(CHAT, uuid(9));
    await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS);
    expect(outstanding.at(-1)?.doc.tabs.map((tab) => tab.node_id)).toEqual([
      undefined,
      uuid(1),
      uuid(2),
    ]);
  });

  it("keeps what only this browser knows when another client's layout arrives", async () => {
    const store = useWorkspaceStore.getState();
    store.openFileTab(CHAT, { nodeId: uuid(1), name: "a.txt" });
    const first = entry().activeTabId as string;
    store.activate(CHAT, FILES_TAB_ID);
    store.markUpdated(CHAT, uuid(1));
    await vi.runAllTimersAsync();

    // A document this browser never wrote: the reader's other laptop opened a
    // third file. The layout is theirs; what changed while this reader was
    // looking elsewhere is still this browser's to say.
    store.hydrate(CHAT, {
      tabs: [
        { id: FILES_TAB_ID, kind: "files", name: "Files", params: {} },
        { id: first, kind: "file", node_id: uuid(1), name: "a.txt" },
        fileTab(uuid(3), "c.txt"),
      ],
      active_tab_id: FILES_TAB_ID,
    });

    expect(nodeIds()).toEqual([uuid(1), uuid(3)]);
    expect(entry().updated).toEqual([first]);
  });
});

describe("a reference in the transcript", () => {
  it("waits for the stored document, then opens the file on top of it", async () => {
    const store = useWorkspaceStore.getState();
    // The transcript is drawn as soon as it loads; the workspace's own document
    // is a second read, and a click in between must not be lost.
    store.reveal(CHAT, { nodeId: uuid(7), parentId: uuid(8), name: "q3-report.html" });
    expect(useWorkspaceStore.getState().chats[CHAT]).toBeUndefined();

    store.hydrate(CHAT, {
      tabs: [fileTab(uuid(1), "a.txt")],
      active_tab_id: `tab-${uuid(1)}`,
    });

    expect(nodeIds()).toEqual([uuid(1), uuid(7)]);
    expect(entry().revealId).toBe(uuid(7));
    expect(entry().tabs.find((tab) => tab.id === FILES_TAB_ID)?.params).toEqual({
      folderId: uuid(8),
    });
    // And it is written, rather than living in this browser until the next
    // change takes it away again.
    await vi.runAllTimersAsync();
    expect(writes().at(-1)?.doc.tabs.map((tab) => tab.node_id)).toEqual([
      undefined,
      uuid(1),
      uuid(7),
    ]);
  });


  it("points the browser at the file's folder, selects it, and opens it", () => {
    useWorkspaceStore.getState().hydrate(CHAT, { tabs: [], active_tab_id: null });

    useWorkspaceStore.getState().reveal(CHAT, {
      nodeId: uuid(7),
      parentId: uuid(8),
      name: "q3-report.html",
      path: "q3-report.html",
    });

    const files = entry().tabs.find((tab) => tab.id === FILES_TAB_ID);
    expect(files?.params).toEqual({ folderId: uuid(8) });
    expect(entry().revealId).toBe(uuid(7));
    expect(nodeIds()).toContain(uuid(7));
    expect(entry().tabs.find((tab) => tab.id === entry().activeTabId)?.node_id).toBe(uuid(7));
  });
});
