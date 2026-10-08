/**
 * Opening a row in the file browser, by every gesture.
 *
 * A chat, a workspace and a template are rows that are a page AND a folder.
 * Opening a chat or a template goes to its page, whether the gesture was a
 * double-click, Enter, or the context menu's first row; the menu's second row
 * lists the files instead. A workspace is a place several chats keep files, so
 * opening one lists its files like any folder, and its page is the second way
 * in: a menu row, and a button in the bar while its files are listed. None of
 * them may fall through to the object page, which renders a result and
 * otherwise says the kind was retired.
 *
 * Everything below is asserted through the real page: where the router lands
 * after a gesture, and what the menu offers for a row.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    name: "notes.txt",
    nameDisplay: "notes.txt",
    nameEncoding: "utf-8",
    pathBytes: "/home/notes.txt",
    path: "/home/notes.txt",
    etag: "et_1",
    ctag: "ct_1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: { can_read: true, can_write: true, can_share: true, can_delete: true },
    ...over,
  } as Item;
}

const HOME = item({ id: "nd_home", kind: "folder", name: "home", nameDisplay: "home" });

/** A chat: a FOLDER carrying the object facet, whose page is the conversation. */
const CHAT = item({
  id: "nd_chat",
  kind: "folder",
  name: "Q3 review.alkerachat",
  nameDisplay: "Q3 review.alkerachat",
  object: {
    type: "chat",
    id: "cht_9",
    title: "Q3 review",
    web_url: "/chat/cht_9",
    metadata: { files_node_id: "nd_chat_files" },
  },
} as unknown as Partial<Item>);

/** A workspace: the folder several chats share, with its own page. */
const WORKSPACE = item({
  id: "nd_ws",
  kind: "folder",
  name: "Pricing.alkeraworkspace",
  nameDisplay: "Pricing.alkeraworkspace",
  object: {
    type: "workspace",
    id: "ws_3",
    title: "Pricing",
    web_url: "/workspaces/ws_3",
    metadata: { files_node_id: "nd_ws_files" },
  },
} as unknown as Partial<Item>);

/** A chat template, whose page is the brief it starts a chat from. */
const TEMPLATE = item({
  id: "nd_tpl",
  kind: "folder",
  name: "Warehouse audit.alkerachat.template",
  nameDisplay: "Warehouse audit.alkerachat.template",
  object: {
    type: "chat_template",
    id: "tpl_4",
    title: "Warehouse audit",
    web_url: "/templates/tpl_4",
    metadata: { files_node_id: "nd_tpl_files" },
  },
} as unknown as Partial<Item>);

const PLAIN = item({ id: "nd_plain", kind: "folder", name: "papers", nameDisplay: "papers" });

/** The folder a workspace keeps its files in, under the workspace's own node. */
const WORKSPACE_FILES = item({
  id: "nd_ws_files",
  kind: "folder",
  name: "files",
  nameDisplay: "files",
  parentId: "nd_ws",
} as unknown as Partial<Item>);

/** A folder that merely shares the workspace's name: no facet, so no workspace. */
const LOOKALIKE = item({
  id: "nd_lookalike",
  kind: "folder",
  name: "Pricing.alkeraworkspace",
  nameDisplay: "Pricing.alkeraworkspace",
  parentId: "nd_home",
} as unknown as Partial<Item>);

/** Every node the item read can answer for, by id; anything else reads as home. */
const NODES: Readonly<Record<string, Item>> = {
  nd_ws: WORKSPACE,
  nd_ws_files: WORKSPACE_FILES,
  nd_lookalike: LOOKALIKE,
  nd_plain: PLAIN,
};

function stubApi(children: Item[]): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/search")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) {
        return answer({ value: [HOME], nextMarker: null });
      }
      if (url.includes("/children")) return answer({ value: children, nextMarker: null });
      if (url.includes("/permissions")) return answer({ value: [] });
      const read = /\/items\/([^/?]+)(\?|$)/.exec(url);
      if (read !== null) return answer(NODES[read[1] ?? ""] ?? HOME);
      return answer({});
    }),
  );
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        media: query,
        matches: false,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
}

/** Where the router is, so what is asserted is where a person landed. */
function Where() {
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

function mount(at = "/files/nd_home") {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[at]}>
        <Where />
        <Routes>
          <Route path="/files" element={<FilesScreen platform="other" />} />
          <Route path="/files/:nodeId" element={<FilesScreen platform="other" />} />
          <Route path="/chat" element={<p>a new chat</p>} />
          <Route path="/chat/:chatId" element={<p>the chat</p>} />
          <Route path="/workspaces/:workspaceId" element={<p>the workspace</p>} />
          <Route path="/templates/:templateId" element={<p>the template</p>} />
          <Route path="/objects/:objectId" element={<p>the object page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function rowNamed(name: string): Promise<HTMLElement> {
  return waitFor(() =>
    within(screen.getByRole("treegrid")).getByRole("row", { name: new RegExp(name) }),
  );
}

/** Reaches a row's context menu the way a person does. */
async function rowMenu(name: string): Promise<HTMLElement> {
  const row = await rowNamed(name);
  const cell = within(row).getAllByRole("gridcell")[0] as HTMLElement;
  await userEvent.pointer([{ target: cell }, { keys: "[MouseRight]", target: cell }]);
  return screen.findByRole("menu");
}

function landedOn(path: string): Promise<void> {
  return waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(path));
}

beforeEach(() => stubApi([PLAIN, CHAT, WORKSPACE, TEMPLATE]));
afterEach(() => vi.unstubAllGlobals());

describe("double-clicking a row opens what it is", () => {
  it.each([
    ["Q3 review", "/chat/cht_9", "the chat"],
    ["Warehouse audit", "/templates/tpl_4", "the template"],
  ])("opens %s on its page, asking nothing first", async (name, path, page) => {
    const user = userEvent.setup();
    mount();

    await user.dblClick(await screen.findByText(name));

    await landedOn(path);
    expect(screen.getByText(page)).toBeInTheDocument();
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.queryByText("the object page")).toBeNull();
  });

  it("lists a workspace's files like any folder, at the folder the server named", async () => {
    const user = userEvent.setup();
    mount();

    await user.dblClick(await screen.findByText("Pricing"));

    await landedOn("/files/nd_ws_files");
    expect(screen.queryByText("the workspace")).toBeNull();
  });

  it("lists an ordinary folder", async () => {
    const user = userEvent.setup();
    mount();

    await user.dblClick(await screen.findByText("papers"));

    await landedOn("/files/nd_plain");
  });
});

describe("Enter opens a row the way a double-click does", () => {
  it.each([
    ["Q3 review", "/chat/cht_9"],
    ["Pricing", "/files/nd_ws_files"],
  ])("opens %s on its page", async (name, path) => {
    const user = userEvent.setup();
    mount();
    const row = await rowNamed(name);
    await user.click(within(row).getAllByRole("gridcell")[0] as HTMLElement);

    await user.keyboard("{Enter}");

    await landedOn(path);
  });
});

describe("the context menu names both ways in", () => {
  it.each([
    ["Q3 review", "Open chat"],
    ["Warehouse audit", "Open template"],
  ])("leads %s with %s and Browse files", async (name, open) => {
    mount();
    const menu = await rowMenu(name);

    const labels = within(menu)
      .getAllByRole("menuitem")
      .map((row) => row.textContent ?? "");
    expect(labels[0]).toMatch(new RegExp(`^${open}`));
    expect(labels[1]).toMatch(/^Browse files/);
  });

  it("leads a workspace with Open, which lists its files, then Open workspace", async () => {
    mount();
    const menu = await rowMenu("Pricing");

    const labels = within(menu)
      .getAllByRole("menuitem")
      .map((row) => row.textContent ?? "");
    expect(labels[0]).toMatch(/^Open(?! in| workspace)/);
    expect(labels[1]).toMatch(/^Open workspace/);
    expect(labels.some((label) => label.startsWith("Browse files"))).toBe(false);
  });

  it("offers no Browse files on an ordinary folder, which already is its files", async () => {
    mount();
    const menu = await rowMenu("papers");

    expect(within(menu).getByRole("menuitem", { name: /^Open(?! in)/ })).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: /^Browse files/ })).toBeNull();
  });

  it.each([
    ["Q3 review", "Open chat", "/chat/cht_9"],
    ["Pricing", "Open workspace", "/workspaces/ws_3"],
  ])("opens %s's page from %s", async (name, open, path) => {
    mount();
    const menu = await rowMenu(name);

    await userEvent.click(within(menu).getByRole("menuitem", { name: new RegExp(`^${open}`) }));

    await landedOn(path);
  });

  it.each([
    ["Q3 review", "/files/nd_chat_files"],
    ["Warehouse audit", "/files/nd_tpl_files"],
  ])("browses %s's files at the folder the server named", async (name, path) => {
    mount();
    const menu = await rowMenu(name);

    await userEvent.click(within(menu).getByRole("menuitem", { name: /^Browse files/ }));

    await landedOn(path);
  });

  it("still starts a new chat from a template", async () => {
    mount();
    const menu = await rowMenu("Warehouse audit");

    await userEvent.click(within(menu).getByRole("menuitem", { name: /^New chat from template/ }));

    // The template's NODE is what the chat surface is handed.
    await landedOn("/chat?source=nd_tpl");
  });
});

describe("the bar inside a workspace's files", () => {
  const openWorkspace = () => screen.queryByRole("button", { name: "Open workspace" });

  it("offers Open workspace in the primary colour, and it opens the workspace", async () => {
    const user = userEvent.setup();
    mount("/files/nd_ws_files");

    const button = await waitFor(() => {
      const found = openWorkspace();
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    expect(button).toHaveClass("alk-files-browser__create--page");

    await user.click(button);

    await landedOn("/workspaces/ws_3");
    expect(screen.getByText("the workspace")).toBeInTheDocument();
  });

  it("is reached by double-clicking the workspace row", async () => {
    const user = userEvent.setup();
    mount();

    await user.dblClick(await screen.findByText("Pricing"));

    await landedOn("/files/nd_ws_files");
    await waitFor(() => expect(openWorkspace()).not.toBeNull());
  });

  it.each([
    ["an ordinary folder", "/files/nd_plain"],
    ["a folder that only shares a workspace's name", "/files/nd_lookalike"],
    ["home", "/files/nd_home"],
  ])("is absent in %s", async (_why, at) => {
    mount(at);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "New folder" })).toBeInTheDocument(),
    );
    expect(openWorkspace()).toBeNull();
  });
});
