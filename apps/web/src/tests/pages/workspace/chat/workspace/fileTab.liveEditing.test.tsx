// A text file open in a chat's workspace is edited live.
//
// The tab is driven through the registry the way the pane drives it, its live
// channel runs against the stand-in server holding a real Loro document, and
// the drive is stubbed at `fetch`. Pinned: a text file opens in the editor on
// the document's text and buys no bytes; a file the server will not open live,
// and a file that is not text, are shown the ordinary way; a reader is told it
// may only view; and the write backs the server makes while people type do not
// mark the tab as changed under its own editor. And what the reader typed that
// the server has not taken is never dropped unseen: it survives a socket that
// drops and a page that reloads, each keystroke landing once, and text the
// document cannot keep is offered back with a way to copy it.

import { EditorView } from "@codemirror/view";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { closeAllLiveFiles } from "@/api/realtime/crdt/liveFile";
import { tabKindFor, type WorkspaceCtx } from "@/pages/workspace/chat/workspace/tabKinds";
import { FILES_TAB_ID, forgetChatPane, useWorkspaceStore } from "@/pages/workspace/chat/workspace/workspaceStore";

import "@/pages/workspace/chat/workspace/FileTab";
import { offersLiveEditing } from "@/pages/workspace/chat/workspace/FileTab";
import { COPY_MY_TEXT } from "@/pages/workspace/chat/workspace/LiveEditsNotice";
import { VIEW_ONLY, savingPausedMessage } from "@/pages/workspace/chat/workspace/LiveFileEditor";

import { LiveServer, loroNode, settle, type FakeSocket } from "../../../../api/realtime/crdt/liveServer";

// The portal loads Loro's WebAssembly build; under jsdom the Node build stands
// in for it, the one thing about the lane this test does not exercise.
vi.mock("@/api/realtime/crdt/loro", async (original) => ({
  ...(await original<typeof import("@/api/realtime/crdt/loro")>()),
  loadLoro: () => Promise.resolve(loroNode),
}));

const DRIVE = "drv_1";
const CHAT_ID = "cht_1";
const ROOT = "nd_root";
const NODE = "0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f";
const TAB_ID = "tab-file";
const CHANNEL = `doc:file:${NODE}`;
/** Who is signed in: what the tab keeps for the next page is filed under them. */
const ME = { id: "usr_ana", org_team_id: "org_a", email: "ana@acme.test" };

function file(over: Partial<Item> & { name: string; mime: string }): Item {
  return {
    id: NODE,
    driveId: DRIVE,
    kind: "file",
    nameDisplay: over.name,
    pathBytes: `/home/dana/Chats/Q3.alkerachat/scratch/${over.name}`,
    parentId: ROOT,
    path: null,
    etag: "e1",
    ctag: "c1",
    attrs: { mtime: "2026-09-01T10:00:00Z" },
    file: { mime_type: over.mime, size: 64, content_hash: "h", scan_state: "clean" },
    object: null,
    lease: null,
    live: null,
    stale: false,
    trashed: false,
    capabilities: { can_write: true, can_download: true },
    ...over,
  } as unknown as Item;
}

const ROOT_FOLDER = {
  id: ROOT,
  driveId: DRIVE,
  kind: "folder",
  name: "scratch",
  nameDisplay: "scratch",
  pathBytes: "/home/dana/Chats/Q3.alkerachat/scratch",
  parentId: "nd_chat",
  etag: "e1",
  capabilities: { can_write: true },
} as unknown as Item;

let items: Record<string, Item>;
let calls: string[];

function stubWire(): void {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      calls.push(url);
      const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
      // The reader is known before any file is held live.
      if (url.includes("/auth/me")) return Promise.resolve(json(ME));
      if (url.startsWith("http://files.localhost")) {
        return Promise.resolve(new Response("the drive's copy", { status: 200, headers: { "content-type": "text/plain" } }));
      }
      if (url.includes("/content-grants")) {
        return Promise.resolve(
          json({ url: "http://files.localhost:8000/c/x", expiresAt: new Date(Date.now() + 60_000).toISOString(), kind: "file", etag: "e1" }),
        );
      }
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const found = items[decodeURIComponent(one[1] ?? "")];
        return Promise.resolve(found ? json(found) : json({ code: "files.not_found" }, 404));
      }
      return Promise.resolve(json({ value: [], nextMarker: null }));
    }),
  );
}

function mount(socket: FakeSocket | undefined, name: string, view?: string) {
  const kind = tabKindFor("file");
  if (kind === undefined) throw new Error("the `file` tab kind was never registered");
  const ctx: WorkspaceCtx = { chatId: CHAT_ID, driveId: DRIVE, rootNodeId: ROOT, liveSocket: socket };
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <kind.Component tab={{ id: TAB_ID, kind: "file", node_id: NODE, name, ...(view ? { view } : {}) }} ctx={ctx} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Let the editor load, the channel sync and the frames flow. */
async function pump(socket: FakeSocket): Promise<void> {
  for (let i = 0; i < 40; i += 1) {
    await act(async () => {
      await socket.deliver();
      await settle();
    });
  }
}

const grants = (): number => calls.filter((url) => url.includes("/content-grants")).length;

beforeEach(() => {
  stubWire();
  useWorkspaceStore.getState().hydrate(CHAT_ID, {
    tabs: [
      { id: FILES_TAB_ID, kind: "files", name: "Files", params: {} },
      { id: TAB_ID, kind: "file", node_id: NODE, name: "greet.py", params: {} },
    ],
    active_tab_id: TAB_ID,
  });
});

afterEach(() => {
  closeAllLiveFiles();
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
  resetFrameBus();
  forgetChatPane(CHAT_ID);
  useWorkspaceStore.setState({ chats: {} });
});

describe("offersLiveEditing", () => {
  it.each([
    ["greet.py", "text/plain", true],
    ["notes.md", "text/plain", true],
    ["notes.txt", "text/plain", true],
    // A table, a page and a drawing are text too: each has an editing view
    // beside its rendering.
    ["table.csv", "text/csv", true],
    ["report.html", "text/html", true],
    ["chart.svg", "image/svg+xml", true],
    ["chart.png", "image/png", false],
    ["report.pdf", "application/pdf", false],
  ])("%s (%s): %s", (name, mime, offered) => {
    expect(offersLiveEditing(file({ name, mime }))).toBe(offered);
  });

  it("never offers a file in the trash, or a folder", () => {
    expect(offersLiveEditing(file({ name: "a.py", mime: "text/plain", trashed: true }))).toBe(false);
    expect(offersLiveEditing({ ...file({ name: "a", mime: "text/plain" }), kind: "folder" } as Item)).toBe(false);
    expect(offersLiveEditing(undefined)).toBe(false);
  });
});

describe("a text file in the workspace", () => {
  it("opens in the live editor on the document's text and buys no bytes", async () => {
    items = { [ROOT]: ROOT_FOLDER, [NODE]: file({ name: "greet.py", mime: "text/plain" }) };
    const server = new LiveServer();
    server.seed(CHANNEL, "def greet():\n    return 'hi'\n");
    const socket = server.socket("ana");
    const { container } = mount(socket, "greet.py");
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(container.querySelector(".cm-content")?.textContent).toContain("return 'hi'"));
    expect(screen.queryByText(VIEW_ONLY)).toBeNull();
    expect(grants()).toBe(0);
  });

  it.each([
    ["notes.md", true],
    ["greet.py", false],
  ])("%s starts with soft wrap %s in its editor, and the header's toggle turns it over", async (name, wraps) => {
    items = { [ROOT]: ROOT_FOLDER, [NODE]: file({ name, mime: "text/plain" }) };
    const server = new LiveServer();
    server.seed(CHANNEL, "one line\n");
    const socket = server.socket("ana");
    const { container } = mount(socket, name, "edit");
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    const content = () => container.querySelector(".cm-content");
    await waitFor(() => expect(content()).not.toBeNull());
    expect(content()?.classList.contains("cm-lineWrapping")).toBe(wraps);
    const toggle = screen.getByRole("button", { name: "Soft wrap" });
    expect(toggle.getAttribute("aria-pressed")).toBe(String(wraps));
    await act(async () => {
      toggle.click();
      await settle();
    });
    expect(content()?.classList.contains("cm-lineWrapping")).toBe(!wraps);
    expect(screen.getByRole("button", { name: "Soft wrap" }).getAttribute("aria-pressed")).toBe(String(!wraps));
  });

  it("reads the document's own size in the header as it changes, not the drive's last one", async () => {
    // The drive's item says 64 bytes; the live document is what people see,
    // in the rendering the note opens in as much as in its editor.
    items = { [ROOT]: ROOT_FOLDER, [NODE]: file({ name: "notes.md", mime: "text/plain" }) };
    const server = new LiveServer();
    server.seed(CHANNEL, "x".repeat(3000));
    const socket = server.socket("ana");
    mount(socket, "notes.md");
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    const facts = () => Array.from(document.querySelectorAll(".alk-ws-file__fact")).map((el) => el.textContent);
    await waitFor(() => expect(facts()[0]).toBe("3 KB"));
    server.edit(CHANNEL, 0, "y".repeat(3000));
    await pump(socket);
    await waitFor(() => expect(facts()[0]).toBe("6 KB"));
  });

  it("is shown the ordinary way when the server will not open it live", async () => {
    items = { [ROOT]: ROOT_FOLDER, [NODE]: file({ name: "greet.py", mime: "text/plain" }) };
    const server = new LiveServer();
    server.refuseHellosWith = "not_editable";
    const socket = server.socket("ana");
    const { container } = mount(socket, "greet.py");
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(screen.getByText("the drive's copy")).toBeTruthy());
    expect(container.querySelector(".cm-editor")).toBeNull();
    expect(grants()).toBeGreaterThan(0);
  });

  it("tells a reader it may only view", async () => {
    items = { [ROOT]: ROOT_FOLDER, [NODE]: file({ name: "greet.py", mime: "text/plain" }) };
    const server = new LiveServer();
    server.seed(CHANNEL, "x = 1\n");
    server.readers.add("ana");
    const socket = server.socket("ana");
    mount(socket, "greet.py");
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(screen.getByText(VIEW_ONLY)).toBeTruthy());
    // Not a caret over text that ignores typing: the editor takes no input.
    const content = document.querySelector(".cm-content");
    expect(content?.getAttribute("contenteditable")).toBe("false");
    expect(content?.getAttribute("aria-label")).toBe("greet.py");
  });

  it("says while its changes are not saving, and why, until they are again", async () => {
    items = { [ROOT]: ROOT_FOLDER, [NODE]: file({ name: "greet.py", mime: "text/plain" }) };
    const server = new LiveServer();
    server.seed(CHANNEL, "x = 1\n");
    const socket = server.socket("ana");
    mount(socket, "greet.py");
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    const leased = savingPausedMessage("leased");
    expect(screen.queryByText(leased)).toBeNull();
    server.tell(CHANNEL, "crdt", { t: "saving", state: "paused", reason: "leased" });
    await pump(socket);
    expect(screen.getByText(leased).getAttribute("role")).toBe("status");
    server.tell(CHANNEL, "crdt", { t: "saving", state: "ok" });
    await pump(socket);
    expect(screen.queryByText(leased)).toBeNull();
  });

  it("says saving is paused from the moment it opens, when it paused before", async () => {
    items = { [ROOT]: ROOT_FOLDER, [NODE]: file({ name: "greet.py", mime: "text/plain" }) };
    const server = new LiveServer();
    server.seed(CHANNEL, "x = 1\n");
    server.saving.set(CHANNEL, { state: "paused", reason: "gone" });
    const socket = server.socket("ana");
    mount(socket, "greet.py");
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    const gone = savingPausedMessage("gone");
    await waitFor(() => expect(screen.getByText(gone).getAttribute("role")).toBe("status"));
  });

  it.each([
    "leased",
    "no_writer",
    "gone",
    "too_large",
    "text_too_large",
    "binary",
    "files.frozen",
    "files.quota_bytes",
    "files.quota_nodes",
    "files.quota_exceeded",
    "files.user_quota_bytes",
    "quarantined_lost",
  ])("names why saving paused (%s) in one sentence", (reason) => {
    const message = savingPausedMessage(reason);
    expect(message).not.toBe(savingPausedMessage("some_new_reason"));
    expect(message).toMatch(/^[A-Z][^.:;]+\.$/);
  });

  it("is not marked as changed by the write backs made while it is edited", async () => {
    items = { [ROOT]: ROOT_FOLDER, [NODE]: file({ name: "greet.py", mime: "text/plain" }) };
    const server = new LiveServer();
    server.seed(CHANNEL, "x = 1\n");
    const socket = server.socket("ana");
    const { container } = mount(socket, "greet.py");
    await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
    await pump(socket);
    await waitFor(() => expect(container.querySelector(".cm-content")).not.toBeNull());
    items = { ...items, [NODE]: { ...(items[NODE] as Item), ctag: "c2", etag: "e2" } as Item };
    await act(async () => {
      publishFrame({ type: "file_node.changed", entity: "file_node", entity_id: NODE, version: 2, org_id: "org_1" });
      await settle();
    });
    await pump(socket);
    expect(screen.queryByText("greet.py updated")).toBeNull();
    expect(useWorkspaceStore.getState().chats[CHAT_ID]?.updated ?? []).toEqual([]);
  });
});

/** The tab's editor, once the file is live in it. */
async function editorIn(container: HTMLElement, socket: FakeSocket): Promise<EditorView> {
  await waitFor(() => expect(socket.held.has(CHANNEL)).toBe(true));
  await pump(socket);
  await waitFor(() => expect(container.querySelector(".cm-editor")).not.toBeNull());
  return EditorView.findFromDOM(container.querySelector<HTMLElement>(".cm-editor")!)!;
}

/** Type `words` at the end of the file, as the keyboard would. */
function typeAtEnd(editor: EditorView, words: string): void {
  act(() => {
    const end = editor.state.doc.length;
    editor.dispatch({ changes: { from: end, insert: words }, selection: { anchor: end + words.length } });
  });
}

const count = (text: string, token: string): number => text.split(token).length - 1;

const TOKENS = ["dn00", "dn01", "dn02", "dn03", "dn04"];

describe("what a reader typed into a live file and the server has not taken", () => {
  beforeEach(() => {
    items = { [ROOT]: ROOT_FOLDER, [NODE]: file({ name: "notes.txt", mime: "text/plain" }) };
  });

  it("stays in the same editor, with its focus, across a server that went away, and lands once", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "notes");
    const socket = server.socket("ana");
    const { container } = mount(socket, "notes.txt");
    const editor = await editorIn(container, socket);
    const node = container.querySelector(".cm-editor");
    act(() => editor.focus());
    // The server goes away mid-sentence and comes back on a new socket.
    typeAtEnd(editor, ` ${TOKENS[0]}`);
    await pump(socket);
    socket.disconnect();
    for (const token of TOKENS.slice(1)) typeAtEnd(editor, ` ${token}`);
    await pump(socket);
    expect(server.text(CHANNEL)).toBe(`notes ${TOKENS[0]}`);
    socket.reconnect();
    await pump(socket);
    await waitFor(() => expect(server.text(CHANNEL)).toBe(`notes ${TOKENS.join(" ")}`));
    expect(container.querySelector(".cm-editor")).toBe(node);
    expect(editor.hasFocus).toBe(true);
    expect(editor.state.doc.toString()).toBe(`notes ${TOKENS.join(" ")}`);
    expect(screen.queryByRole("button", { name: COPY_MY_TEXT })).toBeNull();
  });

  it("is in the editor and on the server exactly once after the page reloads with it pending", async () => {
    const server = new LiveServer();
    server.seed(CHANNEL, "notes");
    const first = server.socket("ana");
    const page = mount(first, "notes.txt");
    const editor = await editorIn(page.container, first);
    first.disconnect();
    for (const token of TOKENS) typeAtEnd(editor, ` ${token}`);
    await pump(first);
    expect(server.text(CHANNEL)).toBe("notes");
    // The page goes away with the server still down, and a new one loads.
    act(() => void window.dispatchEvent(new Event("pagehide")));
    page.unmount();
    closeAllLiveFiles();
    const second = server.socket("ana2");
    const next = mount(second, "notes.txt");
    const reloaded = await editorIn(next.container, second);
    await pump(second);
    await waitFor(() => expect(server.text(CHANNEL)).toBe(`notes ${TOKENS.join(" ")}`));
    await pump(second);
    for (const token of TOKENS) expect(count(reloaded.state.doc.toString(), token)).toBe(1);
    expect(reloaded.state.doc.toString()).toBe(`notes ${TOKENS.join(" ")}`);
  });

  it("is offered back with a way to copy it when the server refuses the edit", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    const server = new LiveServer();
    server.seed(CHANNEL, "notes");
    const socket = server.socket("ana");
    const { container } = mount(socket, "notes.txt");
    const editor = await editorIn(container, socket);
    server.failNext = { code: "crdt_rejected" };
    typeAtEnd(editor, " refused words");
    await pump(socket);
    expect(server.text(CHANNEL)).toBe("notes");
    expect(await screen.findByText("Your last edit could not be shared.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: COPY_MY_TEXT }));
    expect(writeText).toHaveBeenCalledWith(" refused words");
    // The offer stays until the reader dismisses it.
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("button", { name: COPY_MY_TEXT })).toBeNull();
  });

  it("is offered back, once, when the file stops being editable live and the editor goes", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    const server = new LiveServer();
    server.seed(CHANNEL, "notes");
    const socket = server.socket("ana");
    const { container } = mount(socket, "notes.txt");
    const editor = await editorIn(container, socket);
    server.muteAcks = true;
    typeAtEnd(editor, " never taken");
    await pump(socket);
    await act(async () => {
      server.unsupport(CHANNEL);
      await settle();
    });
    await pump(socket);
    // The editor is gone (the drive's copy is shown), the offer is not.
    await waitFor(() => expect(container.querySelector(".cm-editor")).toBeNull());
    expect(await screen.findByText("Your latest edits could not be kept.")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: COPY_MY_TEXT })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: COPY_MY_TEXT }));
    expect(writeText).toHaveBeenCalledWith(" never taken");
  });

  it("opens nothing live before the reader is known, so no file is held with nowhere to keep its edits", async () => {
    let answer: (response: Response) => void = () => {};
    const me = new Promise<Response>((resolve) => (answer = resolve));
    const wire = globalThis.fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
        return url.includes("/auth/me") ? me : wire(input, init);
      }),
    );
    const server = new LiveServer();
    server.seed(CHANNEL, "notes");
    const socket = server.socket("ana");
    const { container } = mount(socket, "notes.txt");
    await pump(socket);
    expect(socket.held.has(CHANNEL)).toBe(false);
    answer(new Response(JSON.stringify(ME), { status: 200, headers: { "content-type": "application/json" } }));
    const editor = await editorIn(container, socket);
    expect(editor.state.doc.toString()).toBe("notes");
  });
});

describe("a file that is not text", () => {
  it("never asks for a live channel and is shown the ordinary way", async () => {
    items = { [ROOT]: ROOT_FOLDER, [NODE]: file({ name: "chart.png", mime: "image/png" }) };
    const server = new LiveServer();
    const socket = server.socket("ana");
    mount(socket, "chart.png");
    await waitFor(() => expect(grants()).toBeGreaterThan(0));
    expect(socket.held.size).toBe(0);
  });
});

describe("a surface without a live socket", () => {
  it("shows a text file the ordinary way", async () => {
    items = { [ROOT]: ROOT_FOLDER, [NODE]: file({ name: "greet.py", mime: "text/plain" }) };
    mount(undefined, "greet.py");
    await waitFor(() => expect(screen.getByText("the drive's copy")).toBeTruthy());
  });
});
