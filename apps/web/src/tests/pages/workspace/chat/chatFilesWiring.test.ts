// Where a file named in the transcript is opened.
//
// The chat's port knows how to find a node back from a path a message named;
// the wiring decides what happens to it. A shell that has somewhere of its own
// to put the file — a tab beside the chat — hands the wiring an opener, and
// from then on a click on a chat link lands there instead of in a new browser
// window. A shell with no opener keeps the port's own door, and a path that
// names nothing opens nothing at all.

import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { installChatRuntime, resetChatRuntime, type ChatDataSource, type ChatHost } from "@/pages/workspace/chat/data";
import type { ChatFileLocation, ChatFilesPort } from "@/pages/workspace/chat/data/chatFiles";
import { cloudChatFiles } from "@/pages/workspace/chat/data/chatFiles";
import { useChatFilesWiring } from "@/pages/workspace/chat/chatFilesWiring";
import {
  FILES_TAB_ID,
  forgetChatPane,
  useWorkspaceStore,
  workspaceOf,
} from "@/pages/workspace/chat/workspace/workspaceStore";

const CHAT = "11111111-1111-1111-1111-111111111111";
const ROOT = "root-node";
const DRIVE = "drive-1";

function found(over: Partial<ChatFileLocation> = {}): ChatFileLocation {
  return { nodeId: "n1", driveId: DRIVE, parentId: ROOT, name: "report.csv", path: "report.csv", kind: "file", ...over };
}

/** The chat's stored workspace has arrived. Until it does the strip on screen
 *  is a guess, and nothing may be written to it. */
function workspaceArrived(): void {
  useWorkspaceStore.getState().hydrate(CHAT, { tabs: [], active_tab_id: null });
}

/** Install a source that offers nothing but this chat-files port. */
function install(port: ChatFilesPort | undefined): void {
  installChatRuntime({
    source: { chatFiles: port } as unknown as ChatDataSource,
    host: {} as unknown as ChatHost,
  });
}

afterEach(() => {
  resetChatRuntime();
  forgetChatPane(CHAT);
  vi.unstubAllGlobals();
});

describe("the transcript's door onto a file", () => {
  it("hands the located node to the opener and leaves the port's own door shut", async () => {
    const openPath = vi.fn();
    const locate = vi.fn(async () => found());
    install({ upload: vi.fn(), resolveUrl: vi.fn(async () => null), locate, openPath });
    const openItem = vi.fn();

    const { result } = renderHook(() => useChatFilesWiring(CHAT, undefined, { openItem }));
    result.current.resolver?.openPath?.("report.csv");

    await vi.waitFor(() => expect(openItem).toHaveBeenCalledWith(found()));
    expect(locate).toHaveBeenCalledWith(CHAT, "report.csv");
    expect(openPath).not.toHaveBeenCalled();
  });

  it("opens no browser window when the opener is there — the real port's window seam never fires", async () => {
    // The browser port's fallback is a new tab. With a tab opener beside the
    // chat, that seam must stay untouched: this drives the REAL port so the
    // assertion is about the code that would have opened the window.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url);
        const json = (body: unknown) =>
          new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
        if (url.pathname === `/api/v1/chats/${CHAT}`) return json({ id: CHAT, title: "t", files_node_id: ROOT });
        if (url.pathname === "/api/v1/files/drives") return json({ id: DRIVE });
        if (url.pathname.endsWith("/children")) {
          return json({ value: [{ id: "n1", kind: "file", name: "report.csv", driveId: DRIVE }], nextMarker: null });
        }
        return new Response("{}", { status: 404 });
      }),
    );
    const openUrl = vi.fn();
    install(cloudChatFiles({ openUrl }));
    const openItem = vi.fn();

    const { result } = renderHook(() => useChatFilesWiring(CHAT, undefined, { openItem }));
    result.current.resolver?.openPath?.("report.csv");

    await vi.waitFor(() => expect(openItem).toHaveBeenCalledWith(found({ path: "report.csv" })));
    expect(openUrl).not.toHaveBeenCalled();
  });

  it("shows the file in the workspace's own browser when no opener is offered, rather than leaving the page", async () => {
    // The port's door is a new browser window. A shell that can look the path
    // up has the node and a file browser beside the chat, so the window is not
    // the answer to a click on a file that is right there.
    const openPath = vi.fn();
    const locate = vi.fn(async () => found());
    install({ upload: vi.fn(), resolveUrl: vi.fn(async () => null), locate, openPath });
    workspaceArrived();

    const { result } = renderHook(() => useChatFilesWiring(CHAT));
    result.current.resolver?.openPath?.("report.csv");

    await vi.waitFor(() => expect(workspaceOf(CHAT)?.revealId).toBe("n1"));
    expect(openPath).not.toHaveBeenCalled();
  });

  it("keeps the port's own door for a shell whose port cannot look a path up", async () => {
    // The VS Code webview's port opens a path through its host and has no
    // lookup of its own; an opener it cannot feed must not swallow the click.
    const openPath = vi.fn();
    install({ upload: vi.fn(), resolveUrl: vi.fn(async () => null), openPath });
    const openItem = vi.fn();

    const { result } = renderHook(() => useChatFilesWiring(CHAT, undefined, { openItem }));
    result.current.resolver?.openPath?.("report.csv");

    expect(openPath).toHaveBeenCalledWith(CHAT, "report.csv");
    expect(openItem).not.toHaveBeenCalled();
  });

  it("opens nothing for a path that names no file in the chat", async () => {
    const locate = vi.fn(async () => null);
    install({ upload: vi.fn(), resolveUrl: vi.fn(async () => null), locate, openPath: vi.fn() });
    const openItem = vi.fn();

    const { result } = renderHook(() => useChatFilesWiring(CHAT, undefined, { openItem }));
    result.current.resolver?.openPath?.("gone.png");

    await vi.waitFor(() => expect(locate).toHaveBeenCalledWith(CHAT, "gone.png"));
    expect(openItem).not.toHaveBeenCalled();
  });

  it("hands the composer the port's own transport bound, and none where the port has none", () => {
    // The browser port uploads through the Files session API, whose ceiling the
    // server publishes and refuses against in words the composer shows the
    // reader — so a number here would be a second ceiling that drifts from it.
    // A port whose transport is genuinely smaller says so and the composer
    // refuses over it without an attempt.
    install({ upload: vi.fn(), resolveUrl: vi.fn(async () => null) });
    const { result: open } = renderHook(() => useChatFilesWiring(CHAT));
    expect(open.current.uploader?.maxBytes).toBeUndefined();

    install({ upload: vi.fn(), resolveUrl: vi.fn(async () => null), maxBytes: 10 * 1024 * 1024 });
    const { result: bounded } = renderHook(() => useChatFilesWiring(CHAT));
    expect(bounded.current.uploader?.maxBytes).toBe(10 * 1024 * 1024);
  });

  it("offers no door at all for a source with no chat-files port", () => {
    install(undefined);
    const { result } = renderHook(() => useChatFilesWiring(CHAT, undefined, { openItem: vi.fn() }));
    expect(result.current.resolver).toBeNull();
    expect(result.current.uploader).toBeUndefined();
  });

  it("keeps one resolver across renders when the opener is re-declared each time", () => {
    install({ upload: vi.fn(), resolveUrl: vi.fn(async () => null), locate: vi.fn(async () => null) });
    const { result, rerender } = renderHook(() => useChatFilesWiring(CHAT, undefined, { openItem: () => {} }));
    const first = result.current.resolver;
    rerender();
    expect(result.current.resolver).toBe(first);
  });
});

describe("where a reference in the transcript points", () => {
  it("answers the node the path names, with the folder it sits in", async () => {
    install({ upload: vi.fn(), resolveUrl: vi.fn(async () => null), locate: vi.fn(async () => found()) });
    const { result } = renderHook(() => useChatFilesWiring(CHAT));

    await expect(result.current.resolver?.locate?.("report.csv")).resolves.toEqual({
      nodeId: "n1",
      parentId: ROOT,
      name: "report.csv",
      path: "report.csv",
    });
  });

  it("answers nothing for a path that walks to a folder — a folder is not a reference the transcript opens", async () => {
    install({
      upload: vi.fn(),
      resolveUrl: vi.fn(async () => null),
      locate: vi.fn(async () => found({ kind: "folder", name: "charts" })),
    });
    const { result } = renderHook(() => useChatFilesWiring(CHAT));

    await expect(result.current.resolver?.locate?.("charts")).resolves.toBeNull();
  });

  it("offers no lookup at all for a port that cannot look a path up", () => {
    install({ upload: vi.fn(), resolveUrl: vi.fn(async () => null), openPath: vi.fn() });
    const { result } = renderHook(() => useChatFilesWiring(CHAT));

    expect(result.current.resolver?.locate).toBeUndefined();
    expect(result.current.resolver?.reveal).toBeUndefined();
  });

  it("reveals through the workspace: the browser walks to the folder, selects the row and the file opens in a tab", async () => {
    install({ upload: vi.fn(), resolveUrl: vi.fn(async () => null), locate: vi.fn(async () => found()) });
    workspaceArrived();
    const { result } = renderHook(() => useChatFilesWiring(CHAT));

    result.current.resolver?.reveal?.({
      nodeId: "n1",
      parentId: ROOT,
      name: "report.csv",
      path: "report.csv",
    });

    const entry = workspaceOf(CHAT);
    expect(entry?.revealId).toBe("n1");
    expect(entry?.tabs.find((tab) => tab.id === FILES_TAB_ID)?.params).toEqual({ folderId: ROOT });
    expect(entry?.tabs.some((tab) => tab.kind === "file" && tab.node_id === "n1")).toBe(true);
    // The reader asked which file this is; the answer is the row in its folder,
    // so the browser is what they are left looking at.
    expect(entry?.activeTabId).toBe(FILES_TAB_ID);
  });

  it("sends the reader to the file's own tab when it is already open, and to the right one of several", () => {
    // The reader has the file open beside the chat. "Which file is that" is
    // already answered by the tab they opened, so the click is a jump to it —
    // not a second tab, and not the folder browser walking somewhere else.
    install({ upload: vi.fn(), resolveUrl: vi.fn(async () => null), locate: vi.fn(async () => found()) });
    workspaceArrived();
    const store = useWorkspaceStore.getState();
    store.openFileTab(CHAT, { nodeId: "n2", name: "notes.md", path: "notes.md" });
    store.openFileTab(CHAT, { nodeId: "n1", name: "report.csv", path: "report.csv" });
    store.activate(CHAT, FILES_TAB_ID);
    store.setFilesFolder(CHAT, "elsewhere");
    const before = workspaceOf(CHAT)?.tabs ?? [];

    const { result } = renderHook(() => useChatFilesWiring(CHAT));
    result.current.resolver?.reveal?.({
      nodeId: "n1",
      parentId: ROOT,
      name: "report.csv",
      path: "report.csv",
    });

    const entry = workspaceOf(CHAT);
    const wanted = entry?.tabs.find((tab) => tab.node_id === "n1");
    expect(entry?.activeTabId).toBe(wanted?.id);
    expect(entry?.activeTabId).not.toBe(entry?.tabs.find((tab) => tab.node_id === "n2")?.id);
    // No second tab for the same file, and the browser is left where it stood.
    expect(entry?.tabs).toHaveLength(before.length);
    expect(entry?.tabs.find((tab) => tab.id === FILES_TAB_ID)?.params).toEqual({ folderId: "elsewhere" });
    expect(entry?.revealId ?? null).toBeNull();
  });

  it("holds a reference clicked before the workspace's own document arrived", async () => {
    // The transcript renders as soon as it loads; the pane's document is a
    // second read, and on a narrow window the pane is not even mounted until
    // the reader switches to it. A click in that window is the reader's, and
    // applying it to a strip the first hydrate is about to replace loses it.
    install({ upload: vi.fn(), resolveUrl: vi.fn(async () => null), locate: vi.fn(async () => found()) });
    const { result } = renderHook(() => useChatFilesWiring(CHAT));

    result.current.resolver?.reveal?.({
      nodeId: "n1",
      parentId: ROOT,
      name: "report.csv",
      path: "report.csv",
    });
    expect(workspaceOf(CHAT)).toBeUndefined();

    workspaceArrived();

    const entry = workspaceOf(CHAT);
    expect(entry?.revealId).toBe("n1");
    expect(entry?.tabs.some((tab) => tab.kind === "file" && tab.node_id === "n1")).toBe(true);
    expect(entry?.activeTabId).toBe(FILES_TAB_ID);
  });

  it("hands the reference to a shell that keeps its own file browser instead", async () => {
    install({ upload: vi.fn(), resolveUrl: vi.fn(async () => null), locate: vi.fn(async () => found()) });
    const revealItem = vi.fn();
    const { result } = renderHook(() => useChatFilesWiring(CHAT, undefined, { revealItem }));

    const ref = { nodeId: "n1", parentId: ROOT, name: "report.csv", path: "report.csv" };
    result.current.resolver?.reveal?.(ref);

    expect(revealItem).toHaveBeenCalledWith(ref);
    expect(workspaceOf(CHAT)?.revealId ?? null).toBeNull();
  });
});

describe("a workspace chat's references", () => {
  it("are looked up in the workspace's shared files folder, where the agent wrote them", async () => {
    // A workspace chat's own folder holds nothing the agent wrote: its working
    // directory is the workspace's shared `files/` tree. Looking a link up under
    // the chat's folder said "not in the chat" for a file Files was showing.
    const SHARED = "node-ws-files";
    const seen: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url);
        const json = (body: unknown) =>
          new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
        if (url.pathname === `/api/v1/chats/${CHAT}`) {
          return json({
            id: CHAT,
            title: "t",
            files_node_id: ROOT,
            workspace_layout: "native",
            workspace_files_node_id: SHARED,
            working_node_id: SHARED,
          });
        }
        if (url.pathname === "/api/v1/files/drives") return json({ id: DRIVE });
        if (url.pathname.endsWith("/children")) {
          seen.push(url.pathname);
          const inShared = url.pathname.includes(`/items/${SHARED}/`);
          return json({
            value: inShared ? [{ id: "n9", kind: "file", name: "geo-dashboard.html", driveId: DRIVE }] : [],
            nextMarker: null,
          });
        }
        return new Response("{}", { status: 404 });
      }),
    );
    install(cloudChatFiles({ openUrl: vi.fn() }));
    const { result } = renderHook(() => useChatFilesWiring(CHAT));

    await expect(result.current.resolver?.locate?.("geo-dashboard.html")).resolves.toMatchObject({
      nodeId: "n9",
      parentId: SHARED,
      name: "geo-dashboard.html",
    });
  });
});
