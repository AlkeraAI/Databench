// @vitest-environment jsdom
//
// The views of a file across editor groups, through the real file tab.
//
// The pane is mounted over a stored document of two groups, the live channel
// runs against the stand-in server holding a real Loro document, and the drive
// is stubbed at `fetch`. Pinned: one file split into two groups is ONE live
// document (an edit in one editor is in the other, over one subscription); a
// Markdown preview follows its source as it is typed; a file opens in its
// preview when it has one and in the editor when it has not; the view toggle
// and its shortcut switch a tab's view, the choice is kept while the tab is
// open and forgotten once it is closed; typing into a preview tab
// keeps it; and a page or a drawing is only ever rendered in a sandboxed frame
// loaded from the content origin; its markup never enters this document.

import { EditorView } from "@codemirror/view";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetFrameBus } from "@/api/events/frameBus";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { closeAllLiveFiles } from "@/api/realtime/crdt/liveFile";

import { LiveServer, loroNode, settle, type FakeSocket } from "../../../../api/realtime/crdt/liveServer";

vi.mock("@/api/realtime/crdt/loro", async (original) => ({
  ...(await original<typeof import("@/api/realtime/crdt/loro")>()),
  loadLoro: () => Promise.resolve(loroNode),
}));

// The folder browser is not what this file is about.
vi.mock("@/pages/workspace/chat/workspace/FilesTab", async () => {
  const { registerTabKind } = await import("@/pages/workspace/chat/workspace/tabKinds");
  registerTabKind({ kind: "files", pinned: true, label: () => "Files", Component: () => <div /> });
  return {};
});

import { ChatSidePane } from "@/pages/workspace/chat/workspace/ChatSidePane";
import {
  forgetChatPane,
  useWorkspaceStore,
  type ChatWorkspaceDoc,
} from "@/pages/workspace/chat/workspace/workspaceStore";

const CHAT_ID = "cht_live_groups";
const DRIVE = "drv_1";
const ROOT = "nd_scratch";
const NODE = "0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f";
const CHANNEL = `doc:file:${NODE}`;
const CONTENT_ORIGIN = "http://files.localhost:8000";

function file(name: string, mime: string): Item {
  return {
    id: NODE,
    driveId: DRIVE,
    kind: "file",
    name,
    nameDisplay: name,
    parentId: ROOT,
    pathBytes: `/Chats/c.alkerachat/scratch/${name}`,
    etag: "e1",
    ctag: "c1",
    attrs: { mtime: "2026-09-01T10:00:00Z" },
    file: { mime_type: mime, size: 64, content_hash: "h", scan_state: "clean" },
    lease: null,
    live: null,
    capabilities: { can_read: true, can_write: true, can_download: true },
    trashed: false,
  } as unknown as Item;
}

let item: Item;
let stored: ChatWorkspaceDoc;
let calls: { url: string; body: unknown }[];
let driveBytes: string;

function stubWire(): void {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : String(input);
      const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : null;
      calls.push({ url, body });
      const json = (value: unknown, status = 200) =>
        Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } }));
      // The reader is known before any file is held live.
      if (url.includes("/auth/me")) return json({ id: "usr_ana", org_team_id: "org_a", email: "ana@acme.test" });
      if (url.startsWith(CONTENT_ORIGIN)) {
        return Promise.resolve(new Response(driveBytes, { status: 200, headers: { "content-type": item.file?.mime_type ?? "text/plain" } }));
      }
      if (/\/chats\/[^/]+\/workspace/.test(url)) {
        if (init?.method === "PUT") return json({ state: (body as { state: unknown }).state, updated_at: "now" });
        return json({ state: stored, updated_at: null });
      }
      if (url.includes("/content-grants")) {
        const kind = (body as { kind?: string } | null)?.kind ?? "file";
        const path = kind === "page" ? `/c/p/tok/${item.name}` : "/c/single";
        return json({ url: `${CONTENT_ORIGIN}${path}`, expiresAt: new Date(Date.now() + 600_000).toISOString(), kind, etag: "e1" });
      }
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const id = decodeURIComponent(one[1] ?? "");
        return id === NODE ? json(item) : json({ id, kind: "folder", name: "scratch", driveId: DRIVE, pathBytes: "/Chats/c.alkerachat/scratch" });
      }
      return json({ value: [], nextMarker: null });
    }),
  );
}

/** Two groups side by side, each showing the file in the view named. */
function sideBySide(left: string | undefined, right: string | undefined, name: string): ChatWorkspaceDoc {
  return {
    tabs: [
      { id: "files", kind: "files", name: "Files", group: "g-l" },
      { id: "t-l", kind: "file", node_id: NODE, name, group: "g-l", ...(left ? { view: left } : {}) },
      { id: "t-r", kind: "file", node_id: NODE, name, group: "g-r", ...(right ? { view: right } : {}) },
    ],
    active_tab_id: "t-l",
    layout: {
      v: 1,
      root: { split: "row", children: [{ g: "g-l" }, { g: "g-r" }], sizes: [0.5, 0.5] },
      active_group: "g-l",
      active: { "g-l": "t-l", "g-r": "t-r" },
    },
  };
}

/** One group showing the file. */
function alone(view: string | undefined, name: string, transient = false): ChatWorkspaceDoc {
  return {
    tabs: [
      { id: "files", kind: "files", name: "Files" },
      { id: "t-one", kind: "file", node_id: NODE, name, ...(view ? { view } : {}), ...(transient ? { transient } : {}) },
    ],
    active_tab_id: "t-one",
  };
}

function mount(socket: FakeSocket | undefined) {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <ChatSidePane chatId={CHAT_ID} driveId={DRIVE} rootNodeId={ROOT} liveSocket={socket} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function pump(socket: FakeSocket): Promise<void> {
  for (let i = 0; i < 40; i += 1) {
    await act(async () => {
      await socket.deliver();
      await settle();
    });
  }
}

function editors(): EditorView[] {
  return Array.from(document.querySelectorAll<HTMLElement>(".cm-editor")).map((node) => {
    const view = EditorView.findFromDOM(node);
    if (!view) throw new Error("an editor without a view");
    return view;
  });
}

function type(view: EditorView, at: number, text: string): void {
  act(() => view.dispatch({ changes: { from: at, insert: text } }));
}

const entry = () => useWorkspaceStore.getState().chats[CHAT_ID];
const mints = (kind: string) =>
  calls.filter((call) => call.url.includes("/content-grants") && (call.body as { kind?: string } | null)?.kind === kind).length;

beforeEach(() => {
  driveBytes = "the drive's copy";
  stubWire();
});

afterEach(() => {
  cleanup();
  closeAllLiveFiles();
  forgetChatPane(CHAT_ID);
  resetFrameBus();
  vi.unstubAllGlobals();
});

describe("one file split into two groups", () => {
  it("is one live document: an edit in one editor is in the other, over one subscription", async () => {
    item = file("greet.py", "text/plain");
    stored = sideBySide(undefined, undefined, "greet.py");
    const server = new LiveServer();
    server.seed(CHANNEL, "x = 1\n");
    const socket = server.socket("ana");
    const subscribe = vi.spyOn(socket, "subscribe");
    mount(socket);
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(editors()).toHaveLength(2));
    const [left, right] = editors() as [EditorView, EditorView];

    type(left, 0, "y = 2\n");
    await pump(socket);

    expect(right.state.doc.toString()).toBe("y = 2\nx = 1\n");
    // And the other way round, and the server has the one document both made.
    type(right, right.state.doc.length, "z = 3\n");
    await pump(socket);
    expect(left.state.doc.toString()).toBe("y = 2\nx = 1\nz = 3\n");
    expect(server.text(CHANNEL)).toBe("y = 2\nx = 1\nz = 3\n");
    expect(subscribe.mock.calls.filter(([channel]) => channel === CHANNEL)).toHaveLength(1);
  });

  it("draws a Markdown preview beside its source that follows it as it is typed", async () => {
    item = file("notes.md", "text/plain");
    stored = sideBySide("edit", "preview", "notes.md");
    const server = new LiveServer();
    server.seed(CHANNEL, "plain words\n");
    const socket = server.socket("ana");
    mount(socket);
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(editors()).toHaveLength(1));
    const preview = within(screen.getByRole("region", { name: "Editor group 2" }));
    await waitFor(() => expect(preview.getByText("plain words")).toBeTruthy());
    // The strip says which tab is the rendering.
    expect(preview.getByRole("tab", { name: /^notes\.md/ })).toBeTruthy();

    type(editors()[0] as EditorView, 0, "# A heading\n\n");
    await pump(socket);

    await waitFor(() => expect(preview.getByRole("heading", { name: "A heading" })).toBeTruthy());
    // Drawn from the document, not bought from the drive.
    expect(mints("file")).toBe(0);
  });
});

describe("opening a file", () => {
  it("shows a note rendered from its live text, its source one toggle away", async () => {
    item = file("notes.md", "text/plain");
    stored = alone(undefined, "notes.md");
    const server = new LiveServer();
    server.seed(CHANNEL, "# Title\n");
    const socket = server.socket("ana");
    mount(socket);
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Title" })).toBeTruthy());
    expect(editors()).toHaveLength(0);
    expect(screen.getByRole("radio", { name: "Preview" }).getAttribute("aria-checked")).toBe("true");
    expect(mints("file")).toBe(0);
  });

  it("shows a script in the editor", async () => {
    item = file("greet.py", "text/plain");
    stored = alone(undefined, "greet.py");
    const server = new LiveServer();
    server.seed(CHANNEL, "x = 1\n");
    const socket = server.socket("ana");
    mount(socket);
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(editors()[0]?.state.doc.toString()).toBe("x = 1\n"));
  });
});

describe("the view toggle", () => {
  it("switches the tab between its rendering and its source, and keeps the choice", async () => {
    item = file("notes.md", "text/plain");
    stored = alone(undefined, "notes.md");
    const server = new LiveServer();
    server.seed(CHANNEL, "# Title\n");
    const socket = server.socket("ana");
    mount(socket);
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Title" })).toBeTruthy());

    fireEvent.click(screen.getByRole("radio", { name: "Edit" }));
    await pump(socket);
    await waitFor(() => expect(editors()).toHaveLength(1));
    expect(entry()?.tabs.find((tab) => tab.id === "t-one")?.view).toBe("edit");

    // The shortcut switches it back.
    fireEvent.keyDown(screen.getByRole("radio", { name: "Edit" }), { key: "V", ctrlKey: true, shiftKey: true });
    await pump(socket);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Title" })).toBeTruthy());
    expect(editors()).toHaveLength(0);
    expect(entry()?.tabs.find((tab) => tab.id === "t-one")?.view).toBe("preview");
  });

  it("keeps the source while the tab is open, and opens the note rendered again once it is closed", async () => {
    item = file("notes.md", "text/plain");
    stored = alone(undefined, "notes.md");
    const server = new LiveServer();
    server.seed(CHANNEL, "# Title\n");
    const socket = server.socket("ana");
    mount(socket);
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Title" })).toBeTruthy());
    fireEvent.click(screen.getByRole("radio", { name: "Edit" }));
    await pump(socket);
    await waitFor(() => expect(editors()).toHaveLength(1));

    const reopen = () =>
      act(() => useWorkspaceStore.getState().openFileTab(CHAT_ID, { nodeId: NODE, name: "notes.md", parentId: ROOT }));
    // Opened again while its tab is open: that tab, as the reader left it.
    reopen();
    await pump(socket);
    expect(editors()).toHaveLength(1);

    act(() => useWorkspaceStore.getState().closeTab(CHAT_ID, "t-one"));
    await pump(socket);
    reopen();
    await pump(socket);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Title" })).toBeTruthy());
    expect(editors()).toHaveLength(0);
  });

  it("is not offered on a file with one view", async () => {
    item = file("greet.py", "text/plain");
    stored = alone(undefined, "greet.py");
    const server = new LiveServer();
    server.seed(CHANNEL, "x = 1\n");
    const socket = server.socket("ana");
    mount(socket);
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(editors()).toHaveLength(1));
    expect(screen.queryByRole("radiogroup")).toBeNull();
    expect(screen.queryByRole("button", { name: "Open preview to the side" })).toBeNull();
  });

  it("opens the rendering to the side from the source", async () => {
    item = file("notes.md", "text/plain");
    stored = alone("edit", "notes.md");
    const server = new LiveServer();
    server.seed(CHANNEL, "# Side\n");
    const socket = server.socket("ana");
    mount(socket);
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(editors()).toHaveLength(1));

    fireEvent.click(screen.getByRole("button", { name: "Open preview to the side" }));
    await pump(socket);

    const side = within(await screen.findByRole("region", { name: "Editor group 2" }));
    await waitFor(() => expect(side.getByRole("heading", { name: "Side" })).toBeTruthy());
    // The source stays where it was.
    expect(editors()).toHaveLength(1);
  });
});

describe("a preview tab", () => {
  it("is kept once the reader types into it", async () => {
    item = file("greet.py", "text/plain");
    stored = alone(undefined, "greet.py", true);
    const server = new LiveServer();
    server.seed(CHANNEL, "x = 1\n");
    const socket = server.socket("ana");
    mount(socket);
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(editors()).toHaveLength(1));
    expect(entry()?.tabs.find((tab) => tab.id === "t-one")?.transient).toBe(true);

    type(editors()[0] as EditorView, 0, "#");

    await waitFor(() => expect(entry()?.tabs.find((tab) => tab.id === "t-one")?.transient).toBe(false));
  });

  it("is not kept by somebody else's edit", async () => {
    item = file("greet.py", "text/plain");
    stored = alone(undefined, "greet.py", true);
    const server = new LiveServer();
    server.seed(CHANNEL, "x = 1\n");
    const socket = server.socket("ana");
    mount(socket);
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(editors()).toHaveLength(1));

    server.edit(CHANNEL, 0, "# from bea\n");
    await pump(socket);

    await waitFor(() => expect(editors()[0]?.state.doc.toString()).toContain("from bea"));
    expect(entry()?.tabs.find((tab) => tab.id === "t-one")?.transient).toBe(true);
  });
});

describe("a page or a drawing", () => {
  it.each([
    ["report.html", "text/html", undefined, "allow-scripts", '<h1 id="planted">Q3</h1><script>window.ran = true</script>'],
    ["chart.svg", "image/svg+xml", undefined, "", '<svg xmlns="http://www.w3.org/2000/svg" id="planted"><rect width="4" height="4"/></svg>'],
  ])("%s renders only in a sandboxed frame on the content origin", async (name, mime, view, sandbox, markup) => {
    item = file(name, mime);
    driveBytes = markup;
    stored = alone(view, name);
    mount(undefined);

    const frame = await waitFor(() => {
      const found = document.querySelector("iframe");
      if (!found) throw new Error("no frame yet");
      return found;
    });
    expect(new URL(frame.getAttribute("src") ?? "").origin).toBe(CONTENT_ORIGIN);
    expect(frame.getAttribute("sandbox")).toBe(sandbox);
    // The grant is a page grant on the content origin; the bytes are never
    // fetched into this document, and nothing in them is in it.
    expect(mints("page")).toBe(1);
    expect(calls.some((call) => call.url.startsWith(CONTENT_ORIGIN))).toBe(false);
    expect(document.getElementById("planted")).toBeNull();
    expect(document.querySelector('img[src^="blob:"]')).toBeNull();
    expect((window as unknown as { ran?: boolean }).ran).toBeUndefined();
  });

  it("edits an HTML file's markup in its editor, which the frame never shares", async () => {
    item = file("report.html", "text/html");
    stored = alone("edit", "report.html");
    const server = new LiveServer();
    server.seed(CHANNEL, "<h1>Q3</h1>\n");
    const socket = server.socket("ana");
    mount(socket);
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(editors()[0]?.state.doc.toString()).toBe("<h1>Q3</h1>\n"));
    expect(document.querySelector("iframe")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Q3" })).toBeNull();
  });
});

describe("a file the editor cannot open", () => {
  it("is shown read-only, and says so", async () => {
    item = file("huge.log.txt", "text/plain");
    driveBytes = "line one\nline two\n";
    stored = alone(undefined, "huge.log.txt");
    const server = new LiveServer();
    server.refuseHellosWith = "not_editable";
    const socket = server.socket("ana");
    mount(socket);
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(screen.getByText(/line one/)).toBeTruthy());
    expect(screen.getByText("View only")).toBeTruthy();
    expect(editors()).toHaveLength(0);
  });
});
