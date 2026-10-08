// The chat page as a workspace: three resizable columns, and what happens to
// them on a screen that cannot hold three.
//
// The rules under test are all about WHEN the files pane exists and WHO decides
// how wide the columns are. A pane is offered only for a chat this reader can
// actually open and that actually has a folder — an empty composer has no
// files, and a chat the server refused has none to show — because a panel that
// is present but permanently empty reads as a broken product rather than as an
// absent one. And the widths belong to this browser: they are read back clamped
// to what the page can draw, a browser that refuses storage lays out from the
// defaults instead of not laying out, and below the breakpoint they are neither
// read nor written, because a width measured on a phone was never a preference
// about a desk.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { accountKey } from "@alkera/ui/storage";

import { createQueryClient } from "@/api/queryClient";
import { RAIL_BOUNDS } from "@/pages/workspace/chat/workspace/layoutStorage";

// The transcript and the roster have their own suites; mounting either here
// would open a socket per case and prove nothing about the arrangement.
vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: (props: { chatId?: string }) => (
    <div data-testid="chat-surface" data-chat-id={props.chatId ?? ""} />
  ),
}));
vi.mock("@/pages/workspace/chat/ChatPresence", () => ({ ChatPresence: () => null }));

// The two tab kinds, stood in for. They are mocked as REGISTRATIONS rather than
// as components: the pane learns what a tab is by importing the module that
// draws it, so a pane that stopped importing them would leave the strip
// rendering the "needs a newer version" plate — which is what these stubs are
// here to rule out, without mounting the whole file explorer.
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
    label: (tab) => tab.name,
    Component: ({ tab }: { tab: { name: string } }) => (
      <div data-testid="file-tab">{tab.name}</div>
    ),
  });
  return {};
});

import { CHAT_GONE_TITLE, CONVERSATION_MIN, ChatPage, WORKSPACE_PANE_LABEL } from "@/pages/workspace/chat/ChatPage";

const USER = "u1";
const ORG = "org_a";
const KEY = accountKey(USER, ORG, "chat.layout");
const LAST = accountKey(USER, ORG, "chat.last");

const CHAT = {
  id: "c1",
  title: "Yesterday's orders",
  machine_id: "m1",
  machine_status: "ready" as const,
  files_node_id: "nd_chat",
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
  last_seq: 3,
};

const CHAT_FOLDER = {
  id: "nd_chat",
  driveId: "drv_1",
  kind: "folder",
  name: "c1.alkerachat",
  nameDisplay: "Yesterday's orders",
  parentId: "nd_root",
  pathBytes: "/Chats/c1.alkerachat",
  etag: "1",
  ctag: "c1",
  object: { type: "chat", web_url: "/chat/c1", metadata: { files_node_id: "nd_work" } },
  capabilities: { can_read: true, can_write: true, can_share: true, refusals: {} },
};

const FILE_NODE = {
  id: "nd_file",
  driveId: "drv_1",
  kind: "file",
  name: "q3-report.html",
  nameDisplay: "q3-report.html",
  parentId: "nd_work",
  pathBytes: "/Chats/c1.alkerachat/work/q3-report.html",
  etag: "1",
  ctag: "f1",
  file: { size: 1200, mime_type: "text/html" },
  capabilities: { can_read: true, can_write: true, can_share: true, refusals: {} },
};

const WORK_DIR = {
  id: "nd_work",
  driveId: "drv_1",
  kind: "folder",
  name: "work",
  nameDisplay: "work",
  parentId: "nd_chat",
  pathBytes: "/Chats/c1.alkerachat/work",
  etag: "1",
  ctag: "w1",
  capabilities: { can_read: true, can_write: true, can_share: true, refusals: {} },
};

interface ScriptOptions {
  /** What the chat read answers. A status refuses it. */
  chatStatus?: number;
  /** The chat's folder, or `null` for a chat that has none. */
  filesNodeId?: string | null;
  /** The stored workspace document this reader comes back to. */
  tabs?: { id: string; kind: string; name: string; node_id?: string; params?: unknown }[];
  activeTabId?: string;
}

function script(options: ScriptOptions = {}): void {
  const json = (payload: unknown, code = 200): Response =>
    new Response(JSON.stringify(payload), {
      status: code,
      headers: { "content-type": "application/json" },
    });
  const items: Record<string, unknown> = { nd_chat: CHAT_FOLDER, nd_work: WORK_DIR, nd_file: FILE_NODE };
  const chat = {
    ...CHAT,
    files_node_id: options.filesNodeId === undefined ? "nd_chat" : options.filesNodeId,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      // openapi-fetch hands a Request, not a string: stringifying one yields
      // "[object Request]" and every branch below would miss.
      const url = input instanceof Request ? input.url : String(input);
      const at = new URL(url, "http://x").pathname;
      if (at === "/api/v1/auth/me") return json({ id: USER, email: "dana@example.com", org_team_id: ORG });
      if (at === "/api/v1/machines/current") {
        return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
      }
      if (at === "/api/v1/files/drives") {
        return json({ id: "drv_1", orgId: "org_1", rootId: "nd_root", quotaBytes: 1 });
      }
      const child = /\/api\/v1\/files\/drives\/[^/]+\/items\/([^/]+)\/children$/.exec(at);
      if (child) return json({ items: [], nextMarker: null });
      const item = /\/api\/v1\/files\/drives\/[^/]+\/items\/([^/]+)$/.exec(at);
      if (item) {
        const found = items[item[1] as string];
        return found ? json(found) : json({ detail: "Not found" }, 404);
      }
      if (/\/api\/v1\/chats\/[^/]+\/workspace$/.test(at)) {
        return json({
          state: {
            tabs: options.tabs ?? [{ id: "files", kind: "files", name: "Files", params: {} }],
            active_tab_id: options.activeTabId ?? "files",
          },
          updated_at: null,
        });
      }
      if (/\/api\/v1\/chats\/[^/?]+$/.test(at)) {
        return options.chatStatus
          ? json({ detail: "Forbidden" }, options.chatStatus)
          : json(chat);
      }
      if (at.startsWith("/api/v1/chats")) return json({ items: [chat], next_cursor: null });
      return json({});
    }),
  );
}

/** The window the page thinks it is in. Both queries are answered explicitly so
 *  no case depends on what jsdom happens to report. */
function stubViewport(kind: "wide" | "medium" | "narrow"): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches:
      query.includes("prefers-reduced-motion")
        ? true
        : query.includes("max-width: 900px")
          ? kind === "narrow"
          : query.includes("min-width: 1280px")
            ? kind === "wide"
            : false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }));
}

function renderChat(path = "/chat/c1") {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/chat/new" element={<ChatPage />} />
          <Route path="/chat/:chatId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const stored = (): {
  rail?: { collapsed?: boolean; width?: number };
  pane?: { collapsed?: boolean; width?: number };
} | null => JSON.parse(localStorage.getItem(KEY) ?? "null");

/** Make this browser's store refuse to be read, and hand back the way out.
 *
 *  Which object actually answers `localStorage.getItem` is not the same on
 *  every engine: where jsdom's own store works it is `Storage.prototype`, and
 *  where the setup installs its in-memory stand-in instead it is that object
 *  itself. A spy pinned to `Storage.prototype` therefore patches, on some
 *  runtimes, a prototype the page never reads through — refusing nothing and
 *  proving nothing — while on others it patches the store every later case in
 *  this file reads, which is not this case's to break. So the object that owns
 *  the property is found first, and the refusal is undone by the case that
 *  asked for it.
 *
 *  `readAnyway` reads past the refusal, so a case can still see what the page
 *  managed to WRITE while its reads were failing.
 */
function storageOwnerOf(method: "getItem" | "setItem"): Storage {
  let owner: object = localStorage;
  while (!Object.getOwnPropertyDescriptor(owner, method)) {
    const next: object | null = Object.getPrototypeOf(owner) as object | null;
    if (!next) throw new Error(`this runtime's localStorage has no ${method}`);
    owner = next;
  }
  return owner as Storage;
}

/** Every touch of this browser's store, in the order the page made it — so a
 *  case can pin not only WHAT the page remembered but WHEN, relative to the
 *  other things the same pass does. */
function traceStorage(): string[] {
  const ops: string[] = [];
  const getOwner = storageOwnerOf("getItem");
  const setOwner = storageOwnerOf("setItem");
  const read = getOwner.getItem;
  const write = setOwner.setItem;
  vi.spyOn(getOwner, "getItem").mockImplementation(function (this: Storage, key: string) {
    ops.push(`read ${key}`);
    return read.call(this, key) as string | null;
  });
  vi.spyOn(setOwner, "setItem").mockImplementation(function (
    this: Storage,
    key: string,
    value: string,
  ) {
    ops.push(`write ${key}`);
    write.call(this, key, value);
  });
  return ops;
}

function refuseStorageReads(): { readAnyway: (key: string) => string | null; allow: () => void } {
  const owner = storageOwnerOf("getItem");
  const original = (owner as Storage).getItem;
  const spy = vi.spyOn(owner as Storage, "getItem").mockImplementation(() => {
    throw new Error("storage is not available");
  });
  return {
    readAnyway: (key) => original.call(localStorage, key) as string | null,
    allow: () => spy.mockRestore(),
  };
}

// jsdom ships no PointerEvent, so a fired `pointerdown` arrives with a null
// `clientX` and every drag reads as a no-op. Give it the one the browser has.
class JsdomPointerEvent extends MouseEvent {
  readonly pointerId: number;
  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
  }
}

beforeEach(() => {
  localStorage.clear();
  stubViewport("wide");
  script();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  // `clearAllMocks` forgets what a spy was called with; only `restoreAllMocks`
  // puts the method it replaced back. A case that makes the store throw and
  // leaves it throwing takes the next case down with it.
  vi.restoreAllMocks();
  vi.clearAllMocks();
  localStorage.clear();
});

describe("the files pane beside the chat", () => {
  it("opens on a chat this reader can read that has a folder", async () => {
    renderChat();

    // The pane's own strip, with the folder browser leading it: the kind
    // registry answered, which is only true if the pane imported the module
    // that registers it.
    const strip = await screen.findByRole("tablist", { name: "Open files" });
    expect(within(strip).getByRole("tab", { name: /Files/ })).toBeTruthy();
    expect(await screen.findByTestId("files-tab")).toBeTruthy();
  });

  it("gives the reader a gutter to resize it with", async () => {
    renderChat();

    const gutter = await screen.findByRole("separator", { name: `Resize ${WORKSPACE_PANE_LABEL}` });
    expect(gutter).toHaveAttribute("aria-orientation", "vertical");
  });

  it("is absent on the empty composer, which has no chat and so no files", async () => {
    renderChat("/chat/new");

    await screen.findByTestId("chat-surface");
    expect(screen.queryByRole("tablist", { name: "Open files" })).toBeNull();
    expect(screen.queryByRole("separator", { name: `Resize ${WORKSPACE_PANE_LABEL}` })).toBeNull();
  });

  it("is absent on a chat the server refused", async () => {
    script({ chatStatus: 403 });
    renderChat();

    await screen.findByText("Ask the owner to share it with you.");
    expect(screen.queryByRole("tablist", { name: "Open files" })).toBeNull();
  });

  // The other window on a chat somebody has just deleted. It is holding the
  // whole surface — transcript, dock, composer — against a chat the server no
  // longer has, and the read it stands on is the only thing that can tell it
  // so. Being left there to type into a dead chat is the failure; landing on an
  // empty composer is the answer.
  it("is gone, with the chat, for a reader whose chat was deleted under them", async () => {
    script({ chatStatus: 404 });
    renderChat();

    // Nothing of the chat is drawn around an id the server has no chat for —
    // not the transcript, and not the files that were beside it.
    await screen.findByText(CHAT_GONE_TITLE);
    expect(screen.queryByTestId("chat-surface")).toBeNull();
    expect(screen.queryByRole("tablist", { name: "Open files" })).toBeNull();
  });

  it("is absent on a chat that has no folder at all", async () => {
    script({ filesNodeId: null });
    renderChat();

    await screen.findByTestId("chat-surface");
    await waitFor(() => expect(screen.getByRole("group", { name: "Chat workspace" })).toBeTruthy());
    expect(screen.queryByRole("tablist", { name: "Open files" })).toBeNull();
  });

  it("draws the tab the reader left in front, not the browser", async () => {
    script({
      tabs: [
        { id: "files", kind: "files", name: "Files", params: {} },
        { id: "tab-1", kind: "file", name: "q3-report.html", node_id: "nd_file" },
      ],
      activeTabId: "tab-1",
    });
    renderChat();

    expect(await screen.findByTestId("file-tab")).toHaveTextContent("q3-report.html");
    // One panel at a time: the browser is in the strip but not mounted.
    expect(screen.queryByTestId("files-tab")).toBeNull();
  });
});

describe("how wide the columns are, and who remembers", () => {
  it("folds the rail away and writes that down for this account", async () => {
    renderChat();
    await screen.findByRole("navigation", { name: /chats/i });

    fireEvent.keyDown(screen.getByRole("separator", { name: "Resize Chats" }), { key: "Enter" });

    await waitFor(() => expect(screen.queryByRole("navigation", { name: /chats/i })).toBeNull());
    // And the key that brings it back is where the column was.
    expect(screen.getByRole("button", { name: "Show Chats" })).toBeTruthy();
    await waitFor(() => expect(stored()?.rail?.collapsed).toBe(true));
  });

  it("brings the rail back from the key the fold left behind", async () => {
    renderChat();
    await screen.findByRole("navigation", { name: /chats/i });
    fireEvent.keyDown(screen.getByRole("separator", { name: "Resize Chats" }), { key: "Enter" });
    const show = await screen.findByRole("button", { name: "Show Chats" });

    fireEvent.click(show);

    await screen.findByRole("navigation", { name: /chats/i });
    await waitFor(() => expect(stored()?.rail?.collapsed).toBe(false));
  });

  // Folding is a preference, not a gesture: a reader who put the rail away on
  // this machine comes back to it away. The write has its own case above; this
  // is the read, which is the half a reload actually depends on.
  it("comes back folded on a browser that was told so, without being asked again", async () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({
        v: 1,
        rail: { collapsed: true, width: 256 },
        pane: { collapsed: false, width: 576 },
      }),
    );
    renderChat();

    // The layout is only read once the account is known, and the note of which
    // chat is open is written in the same pass — so waiting for that note is
    // what makes this about a read that happened, rather than about a page that
    // has not reached it yet.
    await waitFor(() => expect(localStorage.getItem(LAST)).toBe("c1"));
    expect(screen.queryByRole("navigation", { name: /chats/i })).toBeNull();
    expect(screen.getByRole("button", { name: "Show Chats" })).toBeTruthy();
  });

  // And it is read BEFORE the frame that pass draws, not after it. The account
  // arrives from a read, so the arrangement it unlocks and the note of which
  // chat is open become due in the same pass — and the note is due first. Read
  // a pass later, the columns spend a painted frame at the defaults, with a
  // rail the reader folded away standing open. The order is what rules that
  // out, and it is the order, not the clock, that this pins: a suite on a
  // loaded machine that polls between the two passes sees the open rail.
  it("is arranged before that pass writes anything else down", async () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({
        v: 1,
        rail: { collapsed: true, width: 256 },
        pane: { collapsed: false, width: 576 },
      }),
    );
    const ops = traceStorage();
    renderChat();

    await waitFor(() => expect(localStorage.getItem(LAST)).toBe("c1"));
    const arranged = ops.indexOf(`read ${KEY}`);
    const noted = ops.indexOf(`write ${LAST}`);
    expect(arranged).toBeGreaterThanOrEqual(0);
    expect(noted).toBeGreaterThanOrEqual(0);
    expect(arranged).toBeLessThan(noted);
  });

  it("clamps a stored width to what the column can actually be drawn at", async () => {
    // A width left behind by a much wider monitor, or by a hand-edited value.
    localStorage.setItem(
      KEY,
      JSON.stringify({ v: 1, rail: { collapsed: false, width: 9999 }, pane: { collapsed: false, width: 576 } }),
    );
    renderChat();

    const gutter = await screen.findByRole("separator", { name: "Resize Chats" });
    await waitFor(() =>
      expect(gutter.getAttribute("aria-valuenow")).toBe(String(RAIL_BOUNDS.max)),
    );
  });

  it("lays the page out from the defaults when the browser refuses to be read", async () => {
    // A private window, or a browser told to block site data: `getItem` itself
    // throws. That must cost the reader their remembered widths and nothing
    // else — a page that fails to lay out is a page nobody can use.
    //
    // A width is written down first, and it is deliberately NOT the default:
    // laying out at the default is only evidence of a refused read if a read
    // that went through would have produced something else.
    localStorage.setItem(
      KEY,
      JSON.stringify({ v: 1, rail: { collapsed: false, width: 400 }, pane: { collapsed: false, width: 576 } }),
    );
    const store = refuseStorageReads();
    try {
      renderChat();

      // The widths are only looked up once the account is known, and the note
      // of which chat is open is written in the same pass. Waiting for that
      // note is what makes the assertions below about a read that happened and
      // was absorbed, rather than about a page that has not reached it yet.
      await waitFor(() => expect(store.readAnyway(LAST)).toBe("c1"));

      const gutter = screen.getByRole("separator", { name: "Resize Chats" });
      expect(gutter.getAttribute("aria-valuenow")).toBe(String(RAIL_BOUNDS.size));
      expect(screen.getByRole("navigation", { name: /chats/i })).toBeTruthy();
    } finally {
      store.allow();
    }
  });

  it("opens the files pane unasked only on a screen wide enough for it", async () => {
    stubViewport("medium");
    renderChat();

    // The pane is still THERE — its key is in the gutter's place — but the
    // width goes to the conversation until the reader asks otherwise.
    expect(
      await screen.findByRole("button", { name: `Show ${WORKSPACE_PANE_LABEL}` }),
    ).toBeTruthy();
    expect(screen.queryByRole("tablist", { name: "Open files" })).toBeNull();
  });

  it("lays the columns out on every frame of a drag, and remembers one width", async () => {
    // The reader is dragging the files panel narrower. Every frame of that
    // gesture has to reach the screen — the column the cursor is on moves with
    // it — but only ONE of them is a width they chose: the one they stopped on.
    // Writing each frame instead spends a synchronous serialize-and-store on
    // the same main thread that has to redraw three columns under the pointer,
    // and leaves this browser remembering a width the reader merely passed over.
    vi.stubGlobal("PointerEvent", JsdomPointerEvent);
    localStorage.setItem(
      KEY,
      JSON.stringify({
        v: 1,
        rail: { collapsed: false, width: 256 },
        pane: { collapsed: false, width: 576 },
      }),
    );
    renderChat();

    const gutter = await screen.findByRole("separator", {
      name: `Resize ${WORKSPACE_PANE_LABEL}`,
    });
    await waitFor(() => expect(gutter.getAttribute("aria-valuenow")).toBe("576"));

    fireEvent.pointerDown(gutter, { clientX: 1000, pointerId: 1, button: 0 });
    for (const x of [1010, 1020, 1030]) {
      fireEvent.pointerMove(window, { clientX: x, pointerId: 1 });
    }

    // Mid-gesture: the page is laying out from the live width...
    expect(gutter.getAttribute("aria-valuenow")).toBe("546");
    // ...and this browser still holds the width the drag started from. None of
    // the frames crossed on the way has been written down as a preference.
    expect(stored()?.pane?.width).toBe(576);

    fireEvent.pointerUp(window, { pointerId: 1 });

    // The width the gesture settled on is the one that is remembered.
    await waitFor(() => expect(stored()?.pane?.width).toBe(546));
  });
});

describe("the conversation is never the narrowest column", () => {
  // A 1440 px window, less the app's own 256 px navigation, leaves the split
  // 1184 px. With the rail at 256 and a remembered 555 px files pane, leaving
  // the conversation what was left (about 360 px) clips the permission card's
  // allow key and the composer's model name, while too high a conversation
  // floor leaves the files pane too narrow for two editors side by side.
  it("takes the width it needs from the files pane, and leaves the pane room for a split", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      width: 1184,
      height: 800,
      top: 0,
      left: 0,
      right: 1184,
      bottom: 800,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);
    localStorage.setItem(
      KEY,
      JSON.stringify({
        v: 1,
        rail: { collapsed: false, width: 256 },
        pane: { collapsed: false, width: 555 },
      }),
    );
    renderChat();

    const pane = await screen.findByRole("separator", { name: `Resize ${WORKSPACE_PANE_LABEL}` });
    const rail = screen.getByRole("separator", { name: "Resize Chats" });
    await waitFor(() => expect(Number(pane.getAttribute("aria-valuenow"))).toBeLessThan(555));
    const conversation =
      1184 -
      Number(pane.getAttribute("aria-valuenow")) -
      Number(rail.getAttribute("aria-valuenow")) -
      2 * 6;
    expect(conversation).toBeGreaterThanOrEqual(CONVERSATION_MIN);
    expect(CONVERSATION_MIN).toBeGreaterThanOrEqual(420);
    // Two editor groups of at least 230 px each, and the drag may go that far.
    expect(Number(pane.getAttribute("aria-valuenow"))).toBeGreaterThanOrEqual(460);
    expect(Number(pane.getAttribute("aria-valuemax"))).toBeGreaterThanOrEqual(460);
    // Squeezed for the screen, not re-chosen: the width the reader left stays
    // remembered for a wider window.
    expect(stored()?.pane?.width).toBe(555);
  });
});

describe("below the breakpoint", () => {
  beforeEach(() => {
    stubViewport("narrow");
  });

  it("drops the columns for one thing at a time", async () => {
    renderChat();

    await screen.findByTestId("chat-surface");
    expect(screen.queryByRole("group", { name: "Chat workspace" })).toBeNull();
    expect(screen.queryByRole("separator")).toBeNull();
  });

  it("switches between the chat and its files instead of showing both", async () => {
    renderChat();

    const view = await screen.findByRole("tablist", { name: "View" });
    expect(screen.queryByTestId("files-tab")).toBeNull();

    fireEvent.click(within(view).getByRole("tab", { name: "Files" }));

    expect(await screen.findByTestId("files-tab")).toBeTruthy();
    expect(screen.queryByTestId("chat-surface")).toBeNull();
  });

  it("keeps the rail behind a drawer rather than in a column", async () => {
    renderChat();

    await screen.findByTestId("chat-surface");
    expect(screen.queryByRole("navigation", { name: /chats/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Chats" }));

    const drawer = await screen.findByRole("dialog");
    expect(within(drawer).getByRole("navigation", { name: /chats/i })).toBeTruthy();
  });

  it("writes no width down: a phone's layout is not a preference about a desk", async () => {
    renderChat();

    const view = await screen.findByRole("tablist", { name: "View" });
    fireEvent.click(within(view).getByRole("tab", { name: "Files" }));
    await screen.findByTestId("files-tab");
    fireEvent.click(within(view).getByRole("tab", { name: "Chat" }));
    await screen.findByTestId("chat-surface");

    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it("reads no width either, so a folded rail on a laptop does not hide it here", async () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ v: 1, rail: { collapsed: true, width: 200 }, pane: { collapsed: true, width: 576 } }),
    );
    renderChat();

    fireEvent.click(await screen.findByRole("button", { name: "Chats" }));

    const drawer = await screen.findByRole("dialog");
    expect(within(drawer).getByRole("navigation", { name: /chats/i })).toBeTruthy();
  });
});

describe("the files pane belongs to the chat it is beside", () => {
  // One conversation is a column of prose and the next is three files being
  // edited side by side. A reader who widened the pane for the second did not
  // ask for the first to open that way — while the rail, which lists every
  // chat, stays one width whichever one is open.
  const paneKey = (chatId: string): string => `alkera.size:chat.pane:${USER}:${chatId}`;
  const remember = (chatId: string, width: number, collapsed = false): void =>
    localStorage.setItem(paneKey(chatId), JSON.stringify({ collapsed, width }));
  const paneGutter = (): Promise<HTMLElement> =>
    screen.findByRole("separator", { name: `Resize ${WORKSPACE_PANE_LABEL}` });

  it("opens each chat at the width that chat was left at", async () => {
    remember("c1", 560);
    remember("c2", 380);

    renderChat("/chat/c1");
    await waitFor(async () => expect((await paneGutter()).getAttribute("aria-valuenow")).toBe("560"));
    cleanup();

    renderChat("/chat/c2");
    await waitFor(async () => expect((await paneGutter()).getAttribute("aria-valuenow")).toBe("380"));
  });

  it("files a width under the chat it was chosen in, and leaves the others alone", async () => {
    remember("c2", 380);
    renderChat("/chat/c1");

    const gutter = await paneGutter();
    await waitFor(() => expect(gutter.getAttribute("aria-valuenow")).toBe("576"));
    // The account is only known once `/auth/me` has answered, and nothing is
    // filed under a chat before it is.
    await waitFor(() => expect(localStorage.getItem(LAST)).toBe("c1"));
    fireEvent.keyDown(gutter, { key: "ArrowRight" });

    await waitFor(() => expect(JSON.parse(localStorage.getItem(paneKey("c1")) ?? "{}").width).toBe(560));
    // The other chat's width is untouched: it was never on screen.
    expect(JSON.parse(localStorage.getItem(paneKey("c2")) ?? "{}").width).toBe(380);
  });

  it("remembers a folded pane for one chat without folding the next", async () => {
    remember("c1", 576, true);
    remember("c2", 576, false);

    renderChat("/chat/c1");
    expect(await screen.findByRole("button", { name: `Show ${WORKSPACE_PANE_LABEL}` })).toBeTruthy();
    cleanup();

    renderChat("/chat/c2");
    await paneGutter();
    expect(screen.queryByRole("button", { name: `Show ${WORKSPACE_PANE_LABEL}` })).toBeNull();
  });

  it("opens a chat it has never laid out at the last width the reader chose", async () => {
    // The account's own document carries the running default — a better guess
    // than the built-in one, and the thing a per-chat record falls back to.
    localStorage.setItem(
      KEY,
      JSON.stringify({ v: 1, rail: { collapsed: false, width: 256 }, pane: { collapsed: false, width: 600 } }),
    );
    renderChat("/chat/c-new");

    await waitFor(async () => expect((await paneGutter()).getAttribute("aria-valuenow")).toBe("600"));
    expect(localStorage.getItem(paneKey("c-new"))).toBeNull();
  });

  it("keeps the rail one width whichever chat is open", async () => {
    localStorage.setItem(
      KEY,
      JSON.stringify({ v: 1, rail: { collapsed: false, width: 320 }, pane: { collapsed: false, width: 576 } }),
    );
    remember("c1", 560);
    remember("c2", 380);

    renderChat("/chat/c1");
    await waitFor(() =>
      expect(screen.getByRole("separator", { name: "Resize Chats" }).getAttribute("aria-valuenow")).toBe("320"),
    );
    cleanup();

    renderChat("/chat/c2");
    await waitFor(() =>
      expect(screen.getByRole("separator", { name: "Resize Chats" }).getAttribute("aria-valuenow")).toBe("320"),
    );
  });

  it("keeps a bounded number of chats rather than a key for every chat ever opened", async () => {
    const index = `alkera.size.lru:chat.pane:${USER}`;
    renderChat("/chat/c1");
    const gutter = await paneGutter();
    // The account is only known once `/auth/me` has answered, and nothing is
    // filed under a chat before it is.
    await waitFor(() => expect(localStorage.getItem(LAST)).toBe("c1"));
    fireEvent.keyDown(gutter, { key: "ArrowRight" });

    await waitFor(() => expect(stored()?.pane?.width).toBe(560));
    expect(localStorage.getItem(index)).not.toBeNull();
    expect(JSON.parse(localStorage.getItem(index) ?? "[]")).toEqual([`chat.pane:${USER}:c1`]);
  });
});
