// A file open beside the conversation.
//
// The tab is driven through the registry and the store, the way the pane drives
// it, and the wire is stubbed at `fetch` — so what the tab did is asserted by
// the requests it actually made and the DOM it actually painted, never by a
// mock reporting on itself.
//
// Four properties are load-bearing and are asserted by counting requests rather
// than by reading copy: a tab that is not in front buys NO bytes; a tab that is
// not in front still NOTICES its file changing; a change is spoken at most once
// every five seconds however often the file is rewritten; and a file that has
// gone is said to have gone rather than drawn from the last copy the tab saw.

import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import type { RealtimeEventFrame } from "@/api/events/eventMap";
import type { Item } from "@/api/files";
import { enterOrg, forgetActiveOrg } from "@/api/activeOrg";
import { createQueryClient } from "@/api/queryClient";
import {
  tabKindFor,
  type WorkspaceCtx,
  type WorkspaceTab,
} from "@/pages/workspace/chat/workspace/tabKinds";
import { FILES_TAB_ID, forgetChatPane, useWorkspaceStore } from "@/pages/workspace/chat/workspace/workspaceStore";

// The module under test is imported for its registration only; the component is
// looked up through the registry, so the test proves the `file` kind is there
// rather than importing it by name.
import "@/pages/workspace/chat/workspace/FileTab";
import { GONE_TITLE } from "@/pages/workspace/chat/workspace/FileTab";

const DRIVE = "drv_1";
const CHAT_ID = "cht_1";
/** The chat's working directory — the root every tab resolves under. */
const ROOT = "nd_root";
const NODE = "nd_report";
const TAB_ID = "tab-report";
const GRANT_URL = "http://files.localhost:8000/c/bm9uY2U.Y2xhaW0.c2ln";
const PAGE_URL = "http://files.localhost:8000/c/p/bm9uY2U.Y2xhaW0.c2ln/q3-report.html";

const CTX: WorkspaceCtx = { chatId: CHAT_ID, driveId: DRIVE, rootNodeId: ROOT };

function item(overrides: Partial<Item> & { id: string; name: string }): Item {
  return {
    driveId: DRIVE,
    kind: "file",
    nameDisplay: overrides.name,
    pathBytes: "",
    parentId: ROOT,
    path: null,
    etag: "e1",
    attrs: { mtime: "2026-09-01T10:00:00Z" },
    file: null,
    object: null,
    lease: null,
    live: null,
    stale: false,
    trashed: false,
    capabilities: { can_write: true, can_download: true },
    ...overrides,
  } as unknown as Item;
}

const ROOT_FOLDER = item({
  id: ROOT,
  name: "scratch",
  kind: "folder",
  parentId: "nd_chat",
  pathBytes: "/home/dana/Chats/Q3.alkerachat/scratch",
});

function textFile(over: Partial<Item> = {}): Item {
  return item({
    id: NODE,
    name: "notes.txt",
    pathBytes: "/home/dana/Chats/Q3.alkerachat/scratch/charts/notes.txt",
    file: { mime_type: "text/plain", size: 2048, content_hash: "sha256-test", scan_state: "clean" },
    parentId: "nd_charts",
    ...over,
  } as Partial<Item> & { id: string; name: string });
}

/** Every URL the tab asked for, with the method, in order. */
let calls: { url: string; method: string }[] = [];
let items: Record<string, Item>;
/** What a 404 answers for, so a test can make one node disappear. */
let missing: Set<string>;
let trashEntries: { trashOpId: string; item: Item }[];
let restores: string[];
/** What the content origin serves for the grant it minted. */
let served: { body: string; type: string };
let grant: { url: string; kind: "file" | "page" };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function stubWire(): void {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ url, method });

      if (url.startsWith("http://files.localhost")) {
        // The content origin honours a single `Range`, as the real one does.
        const range = /^bytes=(\d+)-(\d+)$/.exec(new Headers(init?.headers).get("Range") ?? "");
        if (range) {
          const all = new TextEncoder().encode(served.body);
          const start = Number(range[1]);
          const end = Math.min(Number(range[2]), all.length - 1);
          return Promise.resolve(
            new Response(all.slice(start, end + 1), {
              status: 206,
              headers: {
                "content-type": served.type,
                "content-range": `bytes ${start}-${end}/${all.length}`,
              },
            }),
          );
        }
        return Promise.resolve(
          new Response(served.body, { status: 200, headers: { "content-type": served.type } }),
        );
      }
      if (url.includes("/content-grants")) {
        return Promise.resolve(
          json({
            url: grant.url,
            expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
            kind: grant.kind,
            etag: "e1",
          }),
        );
      }
      if (url.includes("/trash/") && url.includes("/restore")) {
        restores.push(url);
        return Promise.resolve(json({ ok: true }));
      }
      if (url.includes("/trash")) {
        return Promise.resolve(json({ entries: trashEntries, nextMarker: null }));
      }
      // The share dialog reads who it could share with; nobody, here.
      if (url.includes("/org/members") || url.includes("/teams")) {
        return Promise.resolve(json([]));
      }
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const id = decodeURIComponent(one[1] ?? "");
        if (missing.has(id)) return Promise.resolve(json({ code: "files.not_found" }, 404));
        const found = items[id];
        if (!found) return Promise.resolve(json({ code: "files.not_found" }, 404));
        return Promise.resolve(json(found));
      }
      return Promise.resolve(json({ value: [], nextMarker: null }));
    }),
  );
}

function fileKind() {
  const kind = tabKindFor("file");
  if (kind === undefined) throw new Error("the `file` tab kind was never registered");
  return kind;
}

/** The store as the pane leaves it: the browser plus this file tab, with the
 *  file tab in front unless the test says otherwise. */
function seedStore(options: { active?: string } = {}): void {
  const tab: WorkspaceTab = {
    id: TAB_ID,
    kind: "file",
    node_id: NODE,
    name: "notes.txt",
    params: {},
  };
  useWorkspaceStore.getState().hydrate(CHAT_ID, {
    tabs: [{ id: FILES_TAB_ID, kind: "files", name: "Files", params: {} }, tab],
    active_tab_id: options.active ?? TAB_ID,
  });
}

function mount(tab: Partial<WorkspaceTab> = {}) {
  const Component = fileKind().Component;
  const client = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Component
          tab={{ id: TAB_ID, kind: "file", node_id: NODE, name: "notes.txt", ...tab }}
          ctx={CTX}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const nodeFrame = (entityId: string): RealtimeEventFrame => ({
  type: "file_node.changed",
  entity: "file_node",
  entity_id: entityId,
  version: 1,
  org_id: "org_1",
});

/** A change to the file on the server, then the frame that says so. */
async function fileChanged(next: Partial<Item>): Promise<void> {
  items = { ...items, [NODE]: { ...(items[NODE] as Item), ...next } as Item };
  await act(async () => {
    publishFrame(nodeFrame(NODE));
    await Promise.resolve();
  });
}

function mints(): number {
  return calls.filter((call) => call.url.includes("/content-grants")).length;
}

beforeEach(() => {
  items = { [ROOT]: ROOT_FOLDER, [NODE]: textFile() };
  missing = new Set();
  trashEntries = [];
  restores = [];
  served = { body: "the quarterly numbers", type: "text/plain" };
  grant = { url: GRANT_URL, kind: "file" };
  stubWire();
  seedStore();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetFrameBus();
  forgetChatPane(CHAT_ID);
  useWorkspaceStore.setState({ chats: {} });
});

describe("the tab kind", () => {
  it("registers itself as a closable `file` kind that is named after the live file", () => {
    const kind = tabKindFor("file");
    expect(kind).toBeDefined();
    // Never pinned: a file tab is the one kind a reader closes.
    expect(kind?.pinned).toBeUndefined();
    const tab: WorkspaceTab = { id: TAB_ID, kind: "file", node_id: NODE, name: "notes.txt" };
    // The stored name is the floor; a file renamed on the machine reads as its
    // new name as soon as the tab has read it.
    expect(kind?.label(tab)).toBe("notes.txt");
    expect(kind?.label(tab, textFile({ name: "renamed.txt", nameDisplay: "renamed.txt" }))).toBe(
      "renamed.txt",
    );
  });
});

describe("the toolbar", () => {
  it("names the file, carries its chat-relative path and states its size and when it changed", async () => {
    mount();
    const name = await screen.findByTitle("charts/notes.txt");
    expect(name).toHaveTextContent("notes.txt");
    // The path is relative to the chat's own working directory: the drive chain
    // above it is not the reader's business and not on screen.
    expect(screen.queryByTitle(/home\/dana/)).toBeNull();
    expect(screen.getByText("2 KB")).toBeInTheDocument();
    expect(screen.getByText(/^Updated /)).toBeInTheDocument();
  });

  it("offers the five doors a file has", async () => {
    mount();
    await screen.findByTitle("charts/notes.txt");
    for (const label of ["Download", "Open in new tab", "Share", "Reveal in Files", "Copy link"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
  });

  it("hands the browser the file to save, with the name the reader sees", async () => {
    const user = userEvent.setup();
    const saved: HTMLAnchorElement[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      saved.push(this);
    });
    mount();
    await screen.findByTitle("charts/notes.txt");
    await user.click(screen.getByRole("button", { name: "Download" }));

    expect(saved).toHaveLength(1);
    const anchor = saved[0]!;
    expect(anchor.getAttribute("href")).toContain(`/items/${NODE}/content`);
    expect(anchor.getAttribute("href")).toContain("disposition=attachment");
    expect(anchor.download).toBe("notes.txt");
  });

  it("opens a file the browser renders itself on the content origin, under its own grant", async () => {
    const user = userEvent.setup();
    const open = vi.fn();
    vi.stubGlobal("open", open);
    items = {
      ...items,
      [NODE]: textFile({
        name: "q3-report.html",
        nameDisplay: "q3-report.html",
        file: {
          mime_type: "text/html",
          size: 900,
          content_hash: "sha256-test",
          scan_state: "clean",
        } as Item["file"],
      }),
    };
    grant = { url: PAGE_URL, kind: "page" };
    mount();
    await screen.findByRole("button", { name: "Open in new tab" });
    await user.click(screen.getByRole("button", { name: "Open in new tab" }));

    await waitFor(() =>
      expect(open).toHaveBeenCalledWith(PAGE_URL, "_blank", "noopener,noreferrer"),
    );
    // The page grant, not the app's own inline route: a document that renders
    // itself must never be served from this origin.
    expect(open.mock.calls[0]?.[0]).not.toContain("/api/v1/files");
  });

  it("copies the link to the file and says so only once the clipboard took it", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    mount();
    await screen.findByTitle("charts/notes.txt");
    await user.click(screen.getByRole("button", { name: "Copy link" }));

    await screen.findByText("Link copied");
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining(`/files/${NODE}`));
  });

  it("names the org the link was copied in", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    enterOrg("org_a");
    try {
      mount();
      await screen.findByTitle("charts/notes.txt");
      await user.click(screen.getByRole("button", { name: "Copy link" }));
      await screen.findByText("Link copied");
      expect(writeText).toHaveBeenCalledWith(expect.stringMatching(new RegExp(`/files/${NODE}\\?org=org_a$`)));
    } finally {
      forgetActiveOrg();
    }
  });

  it("says the copy failed when the clipboard refused it", async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: vi.fn(async () => {
          throw new Error("denied");
        }),
      },
      configurable: true,
    });
    mount();
    await screen.findByTitle("charts/notes.txt");
    await user.click(screen.getByRole("button", { name: "Copy link" }));

    await screen.findByText("Couldn't copy the link");
    expect(screen.queryByText("Link copied")).toBeNull();
  });

  it("reveals the file in the browser tab: its folder, its row, and the browser in front", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByTitle("charts/notes.txt");
    await user.click(screen.getByRole("button", { name: "Reveal in Files" }));

    const entry = useWorkspaceStore.getState().chats[CHAT_ID];
    expect(entry?.revealId).toBe(NODE);
    expect(entry?.tabs.find((tab) => tab.id === FILES_TAB_ID)?.params).toEqual({
      folderId: "nd_charts",
    });
    // Revealing means the reader wants the browser, not the file they were
    // already looking at.
    expect(entry?.activeTabId).toBe(FILES_TAB_ID);
  });

  it("opens the share dialog over this node", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByTitle("charts/notes.txt");
    await user.click(screen.getByRole("button", { name: "Share" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/notes\.txt/)).toBeInTheDocument();
  });
});

describe("the bytes", () => {
  it("buys a grant for the file in front and draws what the content origin served", async () => {
    mount();
    await screen.findByText("the quarterly numbers");

    expect(mints()).toBe(1);
    // The bytes are read anonymously from the content origin: the grant IS the
    // authorization, and the session cookie must not go with it.
    const read = calls.find((call) => call.url === GRANT_URL);
    expect(read).toBeDefined();
  });

  it("shows the first window of a 3 MiB text file and brings the next on request", async () => {
    const line = (row: number): string => `row ${String(row).padStart(7, "0")} ${"x".repeat(40)}\n`;
    const rows = Array.from({ length: (3 * 1024 * 1024) / 53 + 1 }, (_, row) => line(row)).join("");
    const body = rows.slice(0, 3 * 1024 * 1024);
    items = {
      ...items,
      [NODE]: textFile({
        file: {
          mime_type: "text/plain",
          size: body.length,
          content_hash: "sha256-test",
          scan_state: "clean",
        },
      } as Partial<Item>),
    };
    served = { body, type: "text/plain" };
    mount();

    const bar = await screen.findByTestId("preview-more");
    expect(bar).toHaveTextContent("Showing 1 MB of 3.1 MB");
    const text = screen.getByTestId("preview-text-body").textContent ?? "";
    expect(text.startsWith(line(0))).toBe(true);
    expect(text.endsWith("\n")).toBe(true);
    expect(text.length).toBeLessThanOrEqual(1024 * 1024);
    expect(mints()).toBe(1);

    await userEvent.click(within(bar).getByRole("button", { name: "Show more" }));

    await waitFor(() =>
      expect(screen.getByTestId("preview-more")).toHaveTextContent("Showing 2.1 MB of 3.1 MB"),
    );
    expect(screen.getByTestId("preview-text-body").textContent?.startsWith(text)).toBe(true);
    // Each window is its own single-use grant.
    expect(mints()).toBe(2);
  });

  it("draws a picture past the size that once kept it out of the preview", async () => {
    const url = URL as unknown as Record<string, unknown>;
    url.createObjectURL = vi.fn(() => "blob:huge");
    url.revokeObjectURL = vi.fn();
    try {
      items = {
        ...items,
        [NODE]: textFile({
          name: "scan.png",
          nameDisplay: "scan.png",
          file: {
            mime_type: "image/png",
            size: 900 * 1024 * 1024,
            content_hash: "sha256-test",
            scan_state: "clean",
          },
        } as Partial<Item>),
      };
      served = { body: "png", type: "image/png" };
      mount();

      const image = await screen.findByAltText("scan.png");
      expect(image.getAttribute("src")).toBe("blob:huge");
      expect(screen.queryByTestId("preview-fallback")).toBeNull();
    } finally {
      cleanup();
      delete url.createObjectURL;
      delete url.revokeObjectURL;
    }
  });

  it("buys nothing for a tab that is not in front", async () => {
    seedStore({ active: FILES_TAB_ID });
    mount();

    // It still reads the node — that is how it notices a change — and it buys no
    // bytes at all.
    await waitFor(() =>
      expect(calls.some((call) => call.url.includes(`/items/${NODE}`))).toBe(true),
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(mints()).toBe(0);
    expect(calls.some((call) => call.url === GRANT_URL)).toBe(false);
    expect(screen.queryByText("the quarterly numbers")).toBeNull();
  });

  it("buys the bytes when the reader brings the tab to the front", async () => {
    seedStore({ active: FILES_TAB_ID });
    mount();
    await waitFor(() =>
      expect(calls.some((call) => call.url.includes(`/items/${NODE}`))).toBe(true),
    );
    expect(mints()).toBe(0);

    act(() => useWorkspaceStore.getState().activate(CHAT_ID, TAB_ID));

    await screen.findByText("the quarterly numbers");
    expect(mints()).toBe(1);
  });
});

describe("a file that changes while it is open", () => {
  it("marks a tab that is not in front, once", async () => {
    seedStore({ active: FILES_TAB_ID });
    mount();
    await waitFor(() =>
      expect(calls.some((call) => call.url.includes(`/items/${NODE}`))).toBe(true),
    );

    // The bytes change: the content tag moves with the etag.
    await fileChanged({ etag: "e2", ctag: "c2" });
    await waitFor(() =>
      expect(useWorkspaceStore.getState().chats[CHAT_ID]?.updated).toEqual([TAB_ID]),
    );

    // A second write is the same news: the tab is already marked.
    await fileChanged({ etag: "e3", ctag: "c3" });
    await act(async () => {
      await Promise.resolve();
    });
    expect(useWorkspaceStore.getState().chats[CHAT_ID]?.updated).toEqual([TAB_ID]);
  });

  it("redraws the tab in front from the new bytes", async () => {
    mount();
    await screen.findByText("the quarterly numbers");

    served = { body: "the revised numbers", type: "text/plain" };
    await fileChanged({ etag: "e2" });

    await screen.findByText("the revised numbers");
    expect(mints()).toBe(2);
  });

  it("announces the change once, and not again inside five seconds", async () => {
    const clock = vi.spyOn(Date, "now");
    clock.mockReturnValue(1_000_000);
    mount();
    await screen.findByText("the quarterly numbers");

    await fileChanged({ etag: "e2", ctag: "c2" });
    const live = screen.getByRole("status");
    await waitFor(() => expect(live).toHaveTextContent("notes.txt updated"));

    // A second later the agent rewrites it, and renames it on the way. Without
    // the floor the region would speak again, under the new name.
    clock.mockReturnValue(1_001_000);
    await fileChanged({ etag: "e3", ctag: "c3", name: "renamed.txt", nameDisplay: "renamed.txt" });
    await waitFor(() =>
      expect(screen.getByTitle("charts/notes.txt")).toHaveTextContent("renamed.txt"),
    );
    expect(live).toHaveTextContent("notes.txt updated");
    expect(live).not.toHaveTextContent("renamed.txt updated");

    // Past the floor the next change is spoken again, under the name it has now.
    clock.mockReturnValue(1_007_000);
    await fileChanged({ etag: "e4", ctag: "c4" });
    await waitFor(() => expect(live).toHaveTextContent("renamed.txt updated"));
  });

  it("speaks politely rather than interrupting", async () => {
    mount();
    await screen.findByText("the quarterly numbers");
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
  });
});

describe("a version arriving under the reader", () => {
  /** Everything the page ever said while the watcher was on. A notice that flashed
   *  for one frame is in here even though it is gone by the time the test looks. */
  function watchEverySay(): { saw: (text: string) => boolean; stop: () => void } {
    const seen: string[] = [document.body.textContent ?? ""];
    const observer = new MutationObserver(() => seen.push(document.body.textContent ?? ""));
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    return {
      saw: (text) => seen.some((snapshot) => snapshot.includes(text)),
      stop: () => observer.disconnect(),
    };
  }

  it("swaps the bytes inside the SAME viewer, never through an empty frame", async () => {
    mount();
    await screen.findByText("the quarterly numbers");
    const body = screen.getByTestId("preview-text-body");
    // Where the reader had scrolled to. It survives only because the element
    // does — a remounted viewer starts at the top with no way to get back.
    body.scrollTop = 420;

    const watch = watchEverySay();
    served = { body: "the revised numbers", type: "text/plain" };
    await fileChanged({ etag: "e2" });
    await screen.findByText("the revised numbers");
    watch.stop();

    // The very same element, still in the document, holding the new bytes.
    expect(screen.getByTestId("preview-text-body")).toBe(body);
    expect(body.isConnected).toBe(true);
    expect(body).toHaveTextContent("the revised numbers");
    expect(body.scrollTop).toBe(420);
    // Not for a single frame in between.
    expect(watch.saw("Loading preview…")).toBe(false);
  });

  it("keeps the reader's soft wrap on across the rewrite", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByText("the quarterly numbers");
    await user.click(screen.getByRole("button", { name: "Soft wrap" }));
    expect(screen.getByTestId("preview-text-body").dataset["wrap"]).toBe("on");

    served = { body: "the revised numbers", type: "text/plain" };
    await fileChanged({ etag: "e2" });
    await screen.findByText("the revised numbers");

    expect(screen.getByTestId("preview-text-body").dataset["wrap"]).toBe("on");
    expect(screen.getByRole("button", { name: "Soft wrap" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("still says a gone file has gone rather than showing the copy it kept", async () => {
    mount();
    await screen.findByText("the quarterly numbers");

    missing = new Set([NODE]);
    await fileChanged({ etag: "e2" });

    await screen.findByText(GONE_TITLE);
    expect(screen.queryByText("the quarterly numbers")).toBeNull();
  });
});

describe("the renderer's own settings", () => {
  it("are in the one header, and the renderer draws no bar of its own", async () => {
    mount();
    await screen.findByText("the quarterly numbers");

    const wrap = screen.getByRole("button", { name: "Soft wrap" });
    // In the header, beside the doors — not in a second bar under it.
    expect(within(screen.getByRole("group", { name: "File actions" })).getByRole("button", {
      name: "Soft wrap",
    })).toBe(wrap);
    expect(document.querySelector(".alk-preview-text__bar")).toBeNull();
  });

  it("switches the file's drawing and reads back as pressed", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByText("the quarterly numbers");
    const wrap = screen.getByRole("button", { name: "Soft wrap" });

    expect(wrap).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("preview-text-body").dataset["wrap"]).toBe("off");

    await user.click(wrap);
    expect(screen.getByRole("button", { name: "Soft wrap" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByTestId("preview-text-body").dataset["wrap"]).toBe("on");
  });
});

describe("a file that is no longer in the chat", () => {
  it("says so, offers only Close, and asks for no bytes", async () => {
    const user = userEvent.setup();
    missing = new Set([NODE]);
    mount();

    await screen.findByText("This file is no longer in the chat");
    expect(screen.queryByRole("button", { name: "Restore" })).toBeNull();
    expect(mints()).toBe(0);

    await user.click(screen.getByRole("button", { name: "Close" }));
    const tabs = useWorkspaceStore.getState().chats[CHAT_ID]?.tabs ?? [];
    expect(tabs.map((tab) => tab.id)).toEqual([FILES_TAB_ID]);
  });

  it("marks the tab as gone so the strip can say so", async () => {
    missing = new Set([NODE]);
    mount();
    await screen.findByText("This file is no longer in the chat");
    expect(useWorkspaceStore.getState().chats[CHAT_ID]?.gone).toEqual([TAB_ID]);
  });

  it("offers Restore for a file in the trash the reader may write, and restores it", async () => {
    const user = userEvent.setup();
    items = { ...items, [NODE]: textFile({ trashed: true }) };
    trashEntries = [{ trashOpId: "op_7", item: items[NODE] as Item }];
    mount();

    await screen.findByText("This file is no longer in the chat");
    await user.click(await screen.findByRole("button", { name: "Restore" }));

    await waitFor(() => expect(restores).toHaveLength(1));
    expect(restores[0]).toContain("/trash/op_7/restore");
  });

  it("offers no Restore for a trashed file the reader may not write", async () => {
    items = {
      ...items,
      [NODE]: textFile({
        trashed: true,
        capabilities: { can_write: false, can_download: true } as Item["capabilities"],
      }),
    };
    trashEntries = [{ trashOpId: "op_7", item: items[NODE] as Item }];
    mount();

    await screen.findByText("This file is no longer in the chat");
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.queryByRole("button", { name: "Restore" })).toBeNull();
    // Nothing was read from the trash either: there was nothing it could offer.
    expect(calls.some((call) => call.url.includes("/trash"))).toBe(false);
  });

  it("offers no Restore for a trashed file the drive does not list as its own deletion", async () => {
    items = { ...items, [NODE]: textFile({ trashed: true }) };
    trashEntries = [];
    mount();

    await screen.findByText("This file is no longer in the chat");
    await waitFor(() => expect(calls.some((call) => call.url.includes("/trash"))).toBe(true));
    expect(screen.queryByRole("button", { name: "Restore" })).toBeNull();
  });

  it("stops drawing the last copy it saw once the file goes", async () => {
    mount();
    await screen.findByText("the quarterly numbers");

    missing = new Set([NODE]);
    await fileChanged({ etag: "e2" });

    await screen.findByText("This file is no longer in the chat");
    expect(screen.queryByText("the quarterly numbers")).toBeNull();
  });
});

describe("a tab with nothing to show", () => {
  it("says the file is no longer in the chat when the stored tab names no node", async () => {
    mount({ node_id: null });
    await screen.findByText("This file is no longer in the chat");
    expect(mints()).toBe(0);
  });
});

describe("copying what the tab shows", () => {
  it("offers Copy for a text file and copies the file's text", async () => {
    // user-event stands in a clipboard of its own for the session.
    const user = userEvent.setup();
    seedStore();
    mount();
    await screen.findByText("the quarterly numbers");

    await user.click(await screen.findByRole("button", { name: "Copy" }));

    await screen.findByText("Copied");
    expect(await navigator.clipboard.readText()).toBe("the quarterly numbers");
  });

  it("offers no Copy for a document only a frame can draw", async () => {
    items = {
      ...items,
      [NODE]: textFile({
        name: "report.pdf",
        file: { mime_type: "application/pdf", size: 2048, content_hash: "sha256-test", scan_state: "clean" },
      } as Partial<Item>),
    };
    seedStore();
    mount();
    await screen.findByRole("button", { name: "Download" });

    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
  });
});
