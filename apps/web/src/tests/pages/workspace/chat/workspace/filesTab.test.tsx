import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import { useRealtimeStatus } from "@/api/events/status";
import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import {
  tabKindFor,
  type WorkspaceCtx,
  type WorkspaceTab,
} from "@/pages/workspace/chat/workspace/tabKinds";
import { forgetChatPane, useWorkspaceStore } from "@/pages/workspace/chat/workspace/workspaceStore";

// The Files tab driven through the real explorer: `fetch` is stubbed at the
// wire, so what the tab reads is asserted by the URLs it actually asked for and
// what it drew by what the treegrid painted from the answers. The component
// under test is looked up through the registry, so the test also proves the
// `files` kind is registered rather than imported by name.
import "@/pages/workspace/chat/workspace/FilesTab";
import { filesLiveFact } from "@/tests/fixtures/statusFacts";

/** The registry's own answer, so the test never imports the component by name. */
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
/** The chat's folder in the drive. Above the root — and out of bounds. */
const CHAT_NODE = "nd_chat";
/** The working directory: the tab's root. */
const ROOT = "nd_root";
const SUB = "nd_charts";

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
    capabilities: { can_write: true },
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
  item({ id: "nd_report", name: "q3-report.html", file: { size: 900 } as Item["file"] }),
  item({ id: SUB, name: "charts", kind: "folder" }),
  item({
    id: "nd_inner_chat",
    name: "notes.alkerachat",
    kind: "folder",
    object: {
      id: "obj_chat_2",
      type: "chat",
      title: "Side thread",
      web_url: "/chat/cht_2",
    } as Item["object"],
  }),
];

const SUB_ROWS: Item[] = [
  item({ id: "nd_png", name: "q3.png", parentId: SUB, file: { size: 20 } as Item["file"] }),
];

/* The one-line header is a LAYOUT contract, and jsdom neither lays out nor
 * cascades CSS — so the declarations it rests on are read off the sheets that
 * state them, the way the chat pane's height contract is checked. */
const WEB = process.cwd();
const sheet = (path: string): string => readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const TREEGRID_CSS = sheet(join(WEB, "src/pages/workspace/files/treegrid.css"));
const WORKSPACE_CSS = sheet(join(WEB, "src/pages/workspace/chat/workspace/workspace.css"));
const RESET_CSS = sheet(resolve(WEB, "../../packages/ui/src/theme/reset.css"));

/** The declarations of the first rule whose selector list ends in `selector`. */
function rule(css: string, selector: string): string {
  const at = css.indexOf(`${selector} {`);
  expect(at, `no rule for \`${selector}\``).toBeGreaterThan(-1);
  const open = css.indexOf("{", at);
  return css.slice(open + 1, css.indexOf("}", open));
}

/** Every URL the tab asked for, in order. */
let calls: string[] = [];
/** Items by id, so a test can change one before the mount reads it. */
let items: Record<string, Item>;
let children: Record<string, Item[]>;

function stubWire(): void {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url =
        typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
      calls.push(url);
      const children_ = /\/items\/([^/?]+)\/children/.exec(url);
      if (children_) {
        const rows = children[decodeURIComponent(children_[1] ?? "")] ?? [];
        return json({ value: rows, nextMarker: null });
      }
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const found = items[decodeURIComponent(one[1] ?? "")];
        if (!found) return json({ code: "files.not_found", message: "no" }, 404);
        return json(found);
      }
      return json({ value: [], nextMarker: null });
    }),
  );
}

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
}

const CTX: WorkspaceCtx = { chatId: CHAT_ID, driveId: DRIVE, rootNodeId: ROOT };

function mount(tab: Partial<WorkspaceTab> = {}) {
  const client = createQueryClient({ retry: false });
  const tree = (over: Partial<WorkspaceTab>) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <FilesTabComponent tab={{ id: "files", kind: "files", name: "Files", ...over }} ctx={CTX} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(tree(tab));
  // The pane re-renders the same tab with new params when the folder changes;
  // the client and the mounted tree are the same ones, as they are in the app.
  return {
    ...view,
    showFolder: (folderId: string) => view.rerender(tree({ params: { folderId } })),
  };
}

function paintedIds(): string[] {
  return Array.from(document.querySelectorAll("[data-row-id]")).map(
    (element) => element.getAttribute("data-row-id") ?? "",
  );
}

beforeEach(() => {
  items = {
    [CHAT_NODE]: CHAT_FOLDER,
    [ROOT]: ROOT_FOLDER,
    ...Object.fromEntries(ROWS.map((row) => [row.id, row])),
  };
  children = { [ROOT]: ROWS, [SUB]: SUB_ROWS };
  stubWire();
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "connected" });
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetFrameBus();
  forgetChatPane(CHAT_ID);
  useWorkspaceStore.setState({ chats: {} });
  useRealtimeStatus.setState({ sse: "idle" });
});

describe("the tab kind", () => {
  it("registers itself as the pinned, singleton `files` kind", () => {
    const kind = tabKindFor("files");
    expect(kind).toBeDefined();
    expect(kind?.pinned).toBe(true);
    expect(kind?.singleton).toBe(true);
  });
});

describe("where it is rooted", () => {
  it("lists the chat's working directory and names the trail after the conversation", async () => {
    mount();
    await waitFor(() => expect(paintedIds()).toEqual(ROWS.map((row) => row.id)));
    // The listing is the working directory's, never the chat folder's.
    expect(calls.some((url) => url.includes(`/items/${ROOT}/children`))).toBe(true);
    expect(calls.some((url) => url.includes(`/items/${CHAT_NODE}/children`))).toBe(false);
    // The trail says the conversation's title, not the minted directory name.
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    await within(trail).findByText("Q3 review");
    expect(within(trail).queryByText("scratch")).toBeNull();
    expect(within(trail).queryByText("3952c9e2.alkerachat")).toBeNull();
  });
});

describe("the folder line", () => {
  it("draws the conversation's mark inside the segment that carries its title", async () => {
    mount();
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    await within(trail).findByText("Q3 review");
    const current = trail.querySelector(".alk-files-crumbs__current");
    // One segment holding both: a mark drawn beside the segment rather than
    // inside it is a mark that can be left on a line of its own.
    expect(current).not.toBeNull();
    expect(current?.querySelector("svg")).not.toBeNull();
    expect(current).toHaveTextContent("Q3 review");
  });

  it("carries the whole title on hover, because the line may be narrower than it", async () => {
    mount();
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    await within(trail).findByText("Q3 review");
    expect(trail.querySelector(".alk-files-crumbs__current")).toHaveAttribute("title", "Q3 review");
  });

  it("cuts a title too long for the line instead of wrapping it", () => {
    const segment = rule(TREEGRID_CSS, ".alk-files-crumbs__current");
    expect(segment).toMatch(/white-space:\s*nowrap/);
    expect(segment).toMatch(/overflow:\s*hidden/);
    expect(segment).toMatch(/text-overflow:\s*ellipsis/);
    expect(rule(TREEGRID_CSS, ".alk-files-crumbs__item")).toMatch(/min-width:\s*0/);
  });

  it("keeps the mark on the title's line", () => {
    // The product reset makes every svg a block. Left that way the chat's mark
    // takes the first line on its own and the title falls to the second, which
    // is the header this tab was showing.
    expect(rule(RESET_CSS, "canvas")).toMatch(/display:\s*block/);
    expect(rule(WORKSPACE_CSS, ".alk-ic")).toMatch(/display:\s*inline-block/);
  });

  it("lets the trail shrink so one line never becomes two", () => {
    // Without this the trail refuses to give width back, and the bar — which
    // wraps — drops the buttons beside it onto a second row.
    expect(rule(WORKSPACE_CSS, ".alk-ws-files .alk-files-crumbs")).toMatch(/min-width:\s*0/);
    expect(rule(WORKSPACE_CSS, ".alk-ws-files .alk-files-crumbs__list")).toMatch(
      /flex-wrap:\s*nowrap/,
    );
  });
});

describe("the pop-out to Files", () => {
  it("opens the chat's folder in Files, in a tab of its own", async () => {
    mount();
    await screen.findByText("q3-report.html");
    const popout = screen.getByRole("link", { name: "Open in Files" });
    expect(popout).toHaveAttribute("href", `/files/${ROOT}`);
    expect(popout).toHaveAttribute("target", "_blank");
    expect(popout).toHaveAttribute("rel", "noopener");
    expect(popout).toHaveAttribute("title", "Open in Files");
  });

  it("sits with the folder's own buttons rather than under the trail", async () => {
    mount();
    await screen.findByText("q3-report.html");
    const acts = document.querySelector(".alk-ws-files__acts");
    expect(acts).not.toBeNull();
    expect(acts?.contains(screen.getByRole("link", { name: "Open in Files" }))).toBe(true);
    expect(acts?.contains(screen.getByRole("button", { name: "New folder" }))).toBe(true);
  });

  it("is withheld when the folder behind the tab cannot be read", async () => {
    // The same asymmetry the Files page withholds it for: the page it would
    // open is "this isn't here", and spending the one click the reader has on
    // that is worse than not offering the click.
    const remaining = { ...items };
    delete remaining[ROOT];
    items = remaining;
    mount();
    // The listing is still readable — what went is the node the link addressed.
    await screen.findByText("q3-report.html");
    await waitFor(() => expect(screen.queryByRole("link", { name: "Open in Files" })).toBeNull());
    expect(screen.getByText("q3-report.html")).toBeInTheDocument();
  });
});

describe("what the status bar says about the saved copy", () => {
  it("says the folder is settled without inventing a time for it", async () => {
    mount();
    await screen.findByText("q3-report.html");
    const bar = document.querySelector(".alk-ws-status");
    await waitFor(() => expect(bar).toHaveAttribute("data-state", "persisted"));
    // The drive does not know when this copy was written, so the time slot
    // says nothing rather than guessing.
    expect(bar).toHaveTextContent(/^Saved copy$/);
  });

  it("dates the copy when the drive knows when it was written", async () => {
    items = {
      ...items,
      [CHAT_NODE]: {
        ...CHAT_FOLDER,
        attrs: { mtime: new Date(Date.now() - 3 * 60_000).toISOString() },
      } as Item,
    };
    mount();
    await screen.findByText("q3-report.html");
    await waitFor(() =>
      expect(document.querySelector(".alk-ws-status")).toHaveTextContent("Saved 3 min ago"),
    );
  });
});

describe("navigation is confined to the chat", () => {
  it("descends into a subfolder and extends the trail", async () => {
    const user = userEvent.setup();
    const { showFolder } = mount();
    await screen.findByText("charts");
    await user.dblClick(screen.getByText("charts"));
    await waitFor(() =>
      expect(useWorkspaceStore.getState().chats[CHAT_ID]?.tabs[0]?.params).toEqual({
        folderId: SUB,
      }),
    );

    // The pane re-renders the tab from what the store now says; the trail then
    // reads chat / subfolder, and the rows are the subfolder's.
    showFolder(SUB);
    await screen.findByText("q3.png");
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    // The chat stays at the head of the trail and the subfolder is appended to
    // it — the tab did not start over at the folder it descended into.
    await within(trail).findByRole("button", { name: "Q3 review" });
    expect(within(trail).getByText("charts")).toBeInTheDocument();
  });

  it("offers the way up from a deep link, by the folder's own parent", async () => {
    const user = userEvent.setup();
    // A tab restored into the subfolder: the trail has one segment and nothing
    // above it to press, so the way up has to come from the folder itself.
    const { showFolder } = mount({ params: { folderId: SUB } });
    await screen.findByText("q3.png");
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(trail).queryByRole("button", { name: "Q3 review" })).toBeNull();

    // Named after the conversation, as the trail names the directory.
    await user.click(within(trail).getByRole("button", { name: "Up to Q3 review" }));
    await waitFor(() =>
      expect(useWorkspaceStore.getState().chats[CHAT_ID]?.tabs[0]?.params?.["folderId"]).not.toBe(
        SUB,
      ),
    );
    const landed = useWorkspaceStore.getState().chats[CHAT_ID]?.tabs[0]?.params?.["folderId"];
    showFolder(typeof landed === "string" ? landed : ROOT);
    await waitFor(() => expect(paintedIds()).toEqual(ROWS.map((row) => row.id)));
  });

  it("opens the folder above on Cmd+Up from a deep link too", async () => {
    mount({ params: { folderId: SUB } });
    await screen.findByText("q3.png");
    const listing = document.querySelector(".alk-ws-files") as HTMLElement;
    fireEvent.keyDown(listing, { key: "ArrowUp", metaKey: true, ctrlKey: true });
    await waitFor(() =>
      expect(useWorkspaceStore.getState().chats[CHAT_ID]?.tabs[0]?.params?.["folderId"]).not.toBe(
        SUB,
      ),
    );
  });

  it("offers the way up disabled at the working directory", async () => {
    mount();
    await screen.findByText("q3-report.html");
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(trail).getByRole("button", { name: /^Up( to |$)/u })).toBeDisabled();
  });

  it("refuses to walk up out of the chat: the folder above the root is never opened", async () => {
    mount();
    await screen.findByText("q3-report.html");
    const asked = calls.length;

    // Cmd+Up is "open the folder above". At the root of the chat there is no
    // folder above that belongs to the chat, so the gesture does nothing —
    // rather than landing the reader in the chat folder, or in the drive.
    const listing = document.querySelector(".alk-ws-files") as HTMLElement;
    fireEvent.keyDown(listing, { key: "ArrowUp", metaKey: true, ctrlKey: true });

    await Promise.resolve();
    const after = calls.slice(asked);
    expect(after.some((url) => url.includes(`/items/${CHAT_NODE}/children`))).toBe(false);
    // And the tab is still showing the root.
    expect(useWorkspaceStore.getState().chats[CHAT_ID]?.tabs[0]?.params ?? {}).toEqual({});
    expect(paintedIds()).toEqual(ROWS.map((row) => row.id));
  });
});

describe("opening a row", () => {
  it("opens a file in a tab beside this one rather than navigating", async () => {
    const user = userEvent.setup();
    const open = vi.fn();
    vi.stubGlobal("open", open);
    mount();
    await screen.findByText("q3-report.html");
    await user.dblClick(screen.getByText("q3-report.html"));
    await waitFor(() => {
      const tabs = useWorkspaceStore.getState().chats[CHAT_ID]?.tabs ?? [];
      expect(tabs.map((entry) => entry.node_id)).toContain("nd_report");
    });
    expect(open).not.toHaveBeenCalled();
  });

  it("previews a file in the group beside the browser on a single click, keeping the browser in view", async () => {
    const user = userEvent.setup();
    useWorkspaceStore.getState().hydrate(CHAT_ID, {
      tabs: [
        { id: "files", kind: "files", name: "Files", group: "g-l" },
        { id: "t-other", kind: "file", node_id: "nd_other", name: "other.md", group: "g-r" },
      ],
      active_tab_id: "files",
      layout: {
        v: 1,
        root: { split: "row", children: [{ g: "g-l" }, { g: "g-r" }], sizes: [0.5, 0.5] },
        active_group: "g-l",
        active: { "g-l": "files", "g-r": "t-other" },
      },
    });
    mount();
    await screen.findByText("q3-report.html");
    await user.click(screen.getByText("q3-report.html"));

    await waitFor(() => {
      const opened = useWorkspaceStore.getState().chats[CHAT_ID]?.tabs.find((tab) => tab.node_id === "nd_report");
      expect(opened?.group).toBe("g-r");
      expect(opened?.transient).toBe(true);
    });
    const groups = useWorkspaceStore.getState().chats[CHAT_ID]?.groups ?? [];
    expect(groups.find((group) => group.id === "g-l")?.activeTabId).toBe("files");
  });

  it("only selects on a single click when the browser has no group beside it", async () => {
    const user = userEvent.setup();
    useWorkspaceStore.getState().hydrate(CHAT_ID, { tabs: [{ id: "files", kind: "files", name: "Files" }], active_tab_id: "files" });
    mount();
    await screen.findByText("q3-report.html");
    await user.click(screen.getByText("q3-report.html"));
    await act(async () => {
      await Promise.resolve();
    });
    const tabs = useWorkspaceStore.getState().chats[CHAT_ID]?.tabs ?? [];
    expect(tabs.some((tab) => tab.kind === "file")).toBe(false);
  });

  it("opens an object-backed row's own page in a new window, and opens no tab for it", async () => {
    const user = userEvent.setup();
    const open = vi.fn();
    vi.stubGlobal("open", open);
    mount();
    await screen.findByText("Side thread");
    await user.dblClick(screen.getByText("Side thread"));
    await waitFor(() => expect(open).toHaveBeenCalledWith("/chat/cht_2", "_blank", "noopener"));
    const tabs = useWorkspaceStore.getState().chats[CHAT_ID]?.tabs ?? [];
    expect(tabs.some((entry) => entry.kind === "file")).toBe(false);
  });
});

describe("a folder the machine is holding", () => {
  it("offers no create buttons while the lease is not inbound", async () => {
    items = {
      ...items,
      [ROOT]: {
        ...ROOT_FOLDER,
        lease: { holder: "Dana", machine: "box-1", live: true, status: filesLiveFact("live"), inbound: false },
      } as Item,
    };
    mount();
    await screen.findByText("q3-report.html");
    expect(screen.getByRole("button", { name: "New folder" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Upload" })).toBeDisabled();
    // The files are still readable: the refusal is about writing, not looking.
    expect(screen.getByText("q3-report.html")).toBeInTheDocument();
  });

  it("offers them again once the lease admits inbound writes", async () => {
    items = {
      ...items,
      [ROOT]: {
        ...ROOT_FOLDER,
        lease: { holder: "Dana", machine: "box-1", live: true, status: filesLiveFact("live"), inbound: true },
      } as Item,
    };
    mount();
    await screen.findByText("q3-report.html");
    expect(screen.getByRole("button", { name: "New folder" })).toBeEnabled();
  });
});

describe("the status bar", () => {
  it("says what the machine is holding back, counted off the rows on screen", async () => {
    // The box takes the CHAT's folder, and a lease is a fact about the whole
    // subtree under it — so the folder and the working directory inside it both
    // carry the facet, as the server sends them.
    const lease = {
      holder: "Dana",
      machine: "box-1",
      live: true,
      status: filesLiveFact("live"),
      inbound: true,
      expires_at: new Date(Date.now() + 600_000).toISOString(),
      pending: 0,
    };
    items = {
      ...items,
      [CHAT_NODE]: { ...CHAT_FOLDER, lease } as Item,
      [ROOT]: { ...ROOT_FOLDER, lease } as Item,
    };
    children = {
      ...children,
      [ROOT]: [{ ...ROWS[0]!, live: { state: "on_box", box_size: 2097152, content: "unlanded" } } as Item, ...ROWS.slice(1)],
    };
    mount();
    await screen.findByText("q3-report.html");
    const bar = document.querySelector(".alk-ws-status");
    await waitFor(() => expect(bar).toHaveAttribute("data-state", "live"));
    expect(bar).toHaveTextContent("1 file on the machine");
  });
});

describe("a listing that refreshes under the reader", () => {
  /** Everything the page ever drew while the watcher was on, so a listing that
   *  emptied for one frame is caught even though it is whole again by the time
   *  the test looks. */
  function watchEveryFrame(): { frames: () => string[]; stop: () => void } {
    const seen: string[] = [document.body.innerHTML];
    const observer = new MutationObserver(() => seen.push(document.body.innerHTML));
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    return { frames: () => seen, stop: () => observer.disconnect() };
  }

  function rowOf(id: string): HTMLElement {
    const found = document.querySelector<HTMLElement>(`[data-row-id="${id}"]`);
    if (found === null) throw new Error(`no row for ${id}`);
    return found;
  }

  function scroller(): HTMLElement {
    const found = document.querySelector<HTMLElement>(".alk-files-grid__scroll");
    if (found === null) throw new Error("the listing rendered no scroller");
    return found;
  }

  const folderFrame = (entityId: string): RealtimeEventFrame =>
    ({
      type: "file_node.changed",
      entity: "file_node",
      entity_id: entityId,
      version: 2,
      org_id: "org_1",
      drive_id: DRIVE,
      parent_id: ROOT,
    }) as RealtimeEventFrame;

  /** The box changed the folder, and the plane says so. */
  async function folderChanged(next: Item[], entityId = "nd_new"): Promise<void> {
    children = { ...children, [ROOT]: next };
    items = { ...items, ...Object.fromEntries(next.map((entry) => [entry.id, entry])) };
    await act(async () => {
      publishFrame(folderFrame(entityId));
      await new Promise((resolve) => setTimeout(resolve, 300));
    });
  }

  it("patches the rows in place, keeping what was selected and where the reader was", async () => {
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(paintedIds()).toEqual(ROWS.map((entry) => entry.id)));

    await user.click(rowOf("nd_report"));
    await waitFor(() => expect(rowOf("nd_report")).toHaveAttribute("aria-selected", "true"));
    const selected = rowOf("nd_report");
    scroller().scrollTop = 240;

    const watch = watchEveryFrame();
    const added = item({ id: "nd_new", name: "appendix.txt", file: { size: 12 } as Item["file"] });
    await folderChanged([...ROWS, added]);
    await waitFor(() => expect(paintedIds()).toContain("nd_new"));
    watch.stop();

    // The listing grew a row; the one the reader had picked is the SAME element,
    // still picked, and the scroller never went back to the top.
    expect(rowOf("nd_report")).toBe(selected);
    expect(selected.isConnected).toBe(true);
    expect(selected).toHaveAttribute("aria-selected", "true");
    expect(scroller().scrollTop).toBe(240);
    // And the grid was never emptied on the way — not for a single frame.
    expect(watch.frames().every((html) => html.includes('data-row-id="nd_report"'))).toBe(true);
  });

  it("keeps drawing the rows it has while the refresh is still in flight", async () => {
    mount();
    await waitFor(() => expect(paintedIds()).toEqual(ROWS.map((entry) => entry.id)));
    const before = paintedIds();

    // A refresh that never answers. What is on screen must stay on screen: a
    // listing that blanks itself the moment it asks again is a listing that
    // blinks every time the machine touches the folder.
    const pending = new Promise<Response>(() => {});
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof Request
              ? input.url
              : input.toString();
        return url.includes("/children") ? pending : json({ value: [], nextMarker: null });
      }),
    );
    await act(async () => {
      publishFrame(folderFrame("nd_report"));
      await new Promise((resolve) => setTimeout(resolve, 300));
    });

    expect(paintedIds()).toEqual(before);
  });

  it("drops a row the folder no longer has without emptying the listing", async () => {
    mount();
    await waitFor(() => expect(paintedIds()).toEqual(ROWS.map((entry) => entry.id)));
    scroller().scrollTop = 120;

    const watch = watchEveryFrame();
    await folderChanged(
      ROWS.filter((entry) => entry.id !== "nd_report"),
      "nd_report",
    );
    await waitFor(() => expect(paintedIds()).not.toContain("nd_report"));
    watch.stop();

    // The rows that survived are still there, in order, and the reader is still
    // where they were. (The lanes the treegrid draws them into are keyed by
    // POSITION, so a removal above a row hands it a different element — which is
    // the grid's business, not this tab's; what this asserts is that the listing
    // was never torn down and rebuilt around it.)
    expect(paintedIds()).toEqual([SUB, "nd_inner_chat"]);
    expect(scroller().scrollTop).toBe(120);
    expect(watch.frames().every((html) => html.includes(`data-row-id="${SUB}"`))).toBe(true);
  });
});

describe("a member's home in the pane", () => {
  it("reads as the home mark and the owner's name, in the row and in the trail", async () => {
    const owner = "6b1f0b1e-0000-4000-8000-0000000000c1";
    const facet = { node_id: "nd_their_home", owner_id: owner, owner_name: "Olive Other" };
    const theirHome = item({
      id: "nd_their_home",
      name: owner,
      kind: "folder",
      home: facet,
      pathHome: facet,
    } as Partial<Item> & { id: string; name: string });
    items = { ...items, [theirHome.id]: theirHome };
    children = { ...children, [ROOT]: [theirHome], [theirHome.id]: [] };
    const view = mount();

    await waitFor(() => expect(paintedIds()).toEqual(["nd_their_home"]));
    const row = document.querySelector("[data-row-id='nd_their_home']");
    expect(row).toHaveTextContent("Olive Other");
    expect(row?.querySelector("[data-home='true'] svg")).not.toBeNull();

    view.showFolder(theirHome.id);
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    await within(trail).findByText("Olive Other");
    expect(trail.querySelector(".alk-files-crumbs__current svg[data-home='true']")).not.toBeNull();
    expect(document.body.textContent).not.toContain(owner);
  });
});

describe("a new notebook", () => {
  // jsdom's Blob reads neither as text nor as bytes; the upload reads its part.
  if (typeof Blob.prototype.arrayBuffer !== "function") {
    Blob.prototype.arrayBuffer = function readSlice(this: Blob): Promise<ArrayBuffer> {
      return new Promise((done, fail) => {
        const reader = new FileReader();
        reader.onload = () => done(reader.result as ArrayBuffer);
        reader.onerror = () => fail(reader.error);
        reader.readAsArrayBuffer(this);
      });
    };
  }

  /** The upload session routes, in front of the listing's stub. */
  function stubUploads(): { opened: Record<string, unknown>[]; parts: number } {
    const seen = { opened: [] as Record<string, unknown>[], parts: 0 };
    const listing = globalThis.fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
        const method = init?.method ?? "GET";
        if (method === "POST" && url.endsWith("/api/v1/files/uploads")) {
          seen.opened.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
          return json({ uploadId: "u1", partSize: 1 << 20, partsTotal: 1, limits: { maxPartBytes: 1 << 20, maxParts: 4 }, expiresAt: "2026-12-01T00:00:00Z" }, 201);
        }
        if (url.endsWith("/api/v1/files/uploads/u1")) {
          return json({ uploadId: "u1", state: "open", offset: 0, length: 0, complete: false, partsDone: 0, partsTotal: 1, acceptedParts: [] });
        }
        if (method === "PUT" && url.includes("/uploads/u1/parts/")) {
          seen.parts += 1;
          return json({ partNo: 1, size: 1, duplicate: false });
        }
        if (url.endsWith("/uploads/u1/complete")) {
          return json({ id: "op1", kind: "upload", state: "done", done: 1, total: 1, resultNodeId: "nd_new_nb" }, 202);
        }
        return listing(input, init);
      }),
    );
    return seen;
  }

  it("is created in the listed folder and opens in the Notebook view", async () => {
    const user = userEvent.setup();
    const seen = stubUploads();
    mount();
    await screen.findByText("q3-report.html");
    await user.click(screen.getByRole("button", { name: "New notebook" }));
    const field = screen.getByRole("textbox", { name: "Notebook name" });
    expect(field).toHaveValue("Untitled");
    await user.clear(field);
    await user.type(field, "Revenue{Enter}");
    await waitFor(() => expect(useWorkspaceStore.getState().chats[CHAT_ID]?.tabs.some((t) => t.node_id === "nd_new_nb")).toBe(true));
    const opened = useWorkspaceStore.getState().chats[CHAT_ID]!.tabs.find((t) => t.node_id === "nd_new_nb")!;
    expect(opened).toMatchObject({ name: "Revenue.alknb.py", view: "notebook" });
    expect(seen.opened).toEqual([expect.objectContaining({ name: "Revenue.alknb.py", parentId: ROOT })]);
    expect(seen.parts).toBe(1);
    expect(screen.queryByRole("form", { name: "New notebook" })).toBeNull();
  });

  it("closes the form on Create and says what is being made until it opens", async () => {
    const user = userEvent.setup();
    const seen = stubUploads();
    // The upload's first request waits until the test lets it through.
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const stubbed = globalThis.fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
        if ((init?.method ?? "GET") === "POST" && url.endsWith("/api/v1/files/uploads")) await gate;
        return stubbed(input, init);
      }),
    );
    mount();
    await screen.findByText("q3-report.html");
    await user.click(screen.getByRole("button", { name: "New notebook" }));
    const field = screen.getByRole("textbox", { name: "Notebook name" });
    await user.clear(field);
    await user.type(field, "Revenue{Enter}");

    expect(screen.queryByRole("form", { name: "New notebook" })).toBeNull();
    const creates = screen.getByRole("group", { name: "Create" });
    expect(within(creates).getByRole("status")).toHaveTextContent("Creating Revenue.alknb.py");
    expect(within(creates).getByRole("button", { name: "New notebook" })).toBeDisabled();

    release();
    await waitFor(() => expect(useWorkspaceStore.getState().chats[CHAT_ID]?.tabs.some((t) => t.node_id === "nd_new_nb")).toBe(true));
    await waitFor(() => expect(within(screen.getByRole("group", { name: "Create" })).queryByRole("status")).toBeNull());
    expect(screen.getByRole("button", { name: "New notebook" })).toBeEnabled();
    expect(seen.opened).toHaveLength(1);
  });

  it("is offered on the folder's menu too", async () => {
    mount();
    await screen.findByText("q3-report.html");
    fireEvent.contextMenu(document.querySelector(".alk-ws-files")!);
    expect(await screen.findByRole("menuitem", { name: /New notebook/ })).toBeInTheDocument();
  });

  it("keeps the field open beside the reason when the upload is refused", async () => {
    const user = userEvent.setup();
    const listing = globalThis.fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
        if ((init?.method ?? "GET") === "POST" && url.endsWith("/api/v1/files/uploads")) {
          return json({ error: { code: "files.forbidden", message: "You can view this folder, not add to it." } }, 403);
        }
        return listing(input, init);
      }),
    );
    mount();
    await screen.findByText("q3-report.html");
    await user.click(screen.getByRole("button", { name: "New notebook" }));
    await user.click(screen.getByRole("button", { name: "Create" }));
    const form = screen.getByRole("form", { name: "New notebook" });
    expect(await within(form).findByRole("status")).not.toBeEmptyDOMElement();
    expect(within(form).getByRole("textbox", { name: "Notebook name" })).toBeInTheDocument();
    expect(useWorkspaceStore.getState().chats[CHAT_ID]?.tabs.some((t) => t.kind === "file") ?? false).toBe(false);
  });

  it("hands the field back holding what was typed when the upload is refused", async () => {
    const user = userEvent.setup();
    const listing = globalThis.fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
        if ((init?.method ?? "GET") === "POST" && url.endsWith("/api/v1/files/uploads")) {
          return json({ error: { code: "files.forbidden", message: "You can view this folder, not add to it." } }, 403);
        }
        return listing(input, init);
      }),
    );
    mount();
    await screen.findByText("q3-report.html");
    await user.click(screen.getByRole("button", { name: "New notebook" }));
    const field = screen.getByRole("textbox", { name: "Notebook name" });
    await user.clear(field);
    await user.type(field, "Revenue{Enter}");
    const form = await screen.findByRole("form", { name: "New notebook" });
    expect(await within(form).findByRole("status")).not.toBeEmptyDOMElement();
    expect(within(form).getByRole("textbox", { name: "Notebook name" })).toHaveValue("Revenue");
    expect(screen.queryByText(/^Creating /)).toBeNull();
  });
});
