import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

// A chat is the one row in the drive that is BOTH a page and a folder: the
// conversation on one side, and on the other the files it holds — what it was
// given, what it worked on and what it produced, all in one working directory
// the server names on the chat's facet. So opening one asks which, and
// answering "files" walks into that directory dressed as the chat: the trail
// says the conversation's title, the rows are ordinary files — read, renamed,
// downloaded and shared through the same routes as any other, because the
// grant on the chat already covers everything beneath it — and the directory's
// own name is never shown.

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    name: "notes.md",
    nameDisplay: "notes.md",
    nameEncoding: "utf-8",
    pathBytes: "/home/notes.md",
    path: "/home/notes.md",
    etag: "et_1",
    ctag: "ct_1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: {
      can_read: true,
      can_write: true,
      can_share: true,
      can_delete: true,
      can_rename: true,
      can_download: true,
    },
    ...over,
  } as Item;
}

const HOME = item({ id: "nd_home", kind: "folder", name: "home", nameDisplay: "home" });

/** The server names where the chat's files live; the name of that node is
 *  deliberately the one word the page must never show. */
const CHAT = item({
  id: "nd_chat",
  kind: "folder",
  parentId: "nd_home",
  name: "8f1e0c22-0000-4000-8000-000000000001.alkerachat",
  nameDisplay: "8f1e0c22-0000-4000-8000-000000000001.alkerachat",
  object: {
    type: "chat",
    id: "cht_9",
    title: "Warehouse spike",
    web_url: "/chat/cht_9",
    metadata: { files_node_id: "nd_files" },
  },
} as unknown as Partial<Item>);
const FILES = item({
  id: "nd_files",
  kind: "folder",
  parentId: "nd_chat",
  name: "scratch",
  nameDisplay: "scratch",
});

/** A chat from before it had a working directory: the server names none, so
 *  its files are the chat folder itself, minus the box's runtime state. */
const OLD = item({
  id: "nd_old",
  kind: "folder",
  parentId: "nd_home",
  name: "2b7c1d33-0000-4000-8000-000000000002.alkerachat",
  nameDisplay: "2b7c1d33-0000-4000-8000-000000000002.alkerachat",
  object: { type: "chat", id: "cht_2", title: "Old notes", web_url: "/chat/cht_2" },
} as unknown as Partial<Item>);

/** The box's own state, beside the working directory: never a row a person sees. */
const RUNTIME = item({
  id: "nd_runtime",
  kind: "folder",
  parentId: "nd_chat",
  name: ".runtime",
  nameDisplay: ".runtime",
});
const MANIFEST = item({
  id: "nd_manifest",
  parentId: "nd_chat",
  name: "manifest.json",
  nameDisplay: "manifest.json",
  // The server refuses the write; the row says so rather than the client guessing.
  capabilities: {
    can_read: true,
    can_write: false,
    can_share: true,
    can_delete: false,
    can_rename: false,
    can_download: true,
    refusals: { write: "The chat owns this file." },
  },
} as unknown as Partial<Item>);
const OLD_RUNTIME = item({ ...RUNTIME, id: "nd_old_runtime", parentId: "nd_old" });
const OLD_INSIDE = item({
  id: "nd_old_inside",
  parentId: "nd_old",
  name: "draft.md",
  nameDisplay: "draft.md",
});

const INSIDE = item({
  id: "nd_inside",
  parentId: "nd_files",
  name: "findings.csv",
  nameDisplay: "findings.csv",
  path: "/home/chat/scratch/findings.csv",
  pathBytes: "/home/chat/scratch/findings.csv",
});
const DATA = item({
  id: "nd_data",
  kind: "folder",
  parentId: "nd_files",
  name: "data",
  nameDisplay: "data",
});

const CHAT_FOLDER_CHILDREN = [RUNTIME, FILES, MANIFEST];
const CHAT_FILES = [INSIDE, DATA];

/** A chat template is the second row that is a page AND a folder of files, and
 *  it keeps its files the same way — at a working folder the server names on the
 *  facet, under a name nobody typed. The listing has to dress it as the template
 *  for exactly the reason it dresses a chat as the chat. */
const TEMPLATE = item({
  id: "nd_tpl",
  kind: "folder",
  parentId: "nd_home",
  name: "6c2a1b90-0000-4000-8000-000000000003.alkerachat.template",
  nameDisplay: "6c2a1b90-0000-4000-8000-000000000003.alkerachat.template",
  object: {
    type: "chat_template",
    id: "tpl_4",
    title: "Warehouse audit",
    web_url: "/templates/tpl_4",
    metadata: { files_node_id: "nd_tpl_files" },
  },
} as unknown as Partial<Item>);
const TPL_FILES = item({
  id: "nd_tpl_files",
  kind: "folder",
  parentId: "nd_tpl",
  name: "seed",
  nameDisplay: "seed",
});
const TPL_INSIDE = item({
  id: "nd_tpl_inside",
  parentId: "nd_tpl_files",
  name: "checklist.md",
  nameDisplay: "checklist.md",
});

let requests: Array<{ url: string; method: string; body: string | null }> = [];

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : String(input);
      // The typed client sends a `Request`, so the body is a stream rather than
      // a string on `init`: reading it back is the only way a test sees what
      // was actually written.
      const sent =
        typeof init?.body === "string"
          ? init.body
          : input instanceof Request
            ? await input.clone().text()
            : null;
      requests.push({
        url,
        method: (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase(),
        body: sent === "" ? null : sent,
      });
      const answer = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/members")) return answer([]);
      if (url.includes("/teams")) return answer([]);
      if (url.includes("/search")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) {
        return answer({ value: [HOME], nextMarker: null });
      }
      if (url.includes("/items/nd_chat/children")) {
        return answer({ value: CHAT_FOLDER_CHILDREN, nextMarker: null });
      }
      if (url.includes("/items/nd_files/children")) {
        return answer({ value: CHAT_FILES, nextMarker: null });
      }
      if (url.includes("/items/nd_old/children")) {
        return answer({ value: [OLD_RUNTIME, OLD_INSIDE], nextMarker: null });
      }
      if (url.includes("/items/nd_data/children")) {
        return answer({ value: [], nextMarker: null });
      }
      if (url.includes("/items/nd_tpl_files/children")) {
        return answer({ value: [TPL_INSIDE], nextMarker: null });
      }
      if (url.includes("/items/nd_tpl/children")) {
        return answer({ value: [TPL_FILES], nextMarker: null });
      }
      if (url.includes("/children")) {
        return answer({ value: [CHAT, OLD, TEMPLATE], nextMarker: null });
      }
      if (url.includes("/permissions")) return answer({ value: [] });
      if (url.includes("/items/nd_tpl_files")) return answer(TPL_FILES);
      if (url.includes("/items/nd_tpl")) return answer(TEMPLATE);
      if (url.includes("/items/nd_chat")) return answer(CHAT);
      if (url.includes("/items/nd_files")) return answer(FILES);
      if (url.includes("/items/nd_old")) return answer(OLD);
      if (url.includes("/items/nd_data")) return answer(DATA);
      if (url.includes("/items/nd_inside")) return answer(INSIDE);
      if (url.includes("/items/")) return answer(HOME);
      return answer({});
    }),
  );
}

function stubViewport(): void {
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

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}

function mount(at = "/files/nd_home") {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[at]}>
        <Where />
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen />} />
          <Route path="/chat" element={<p>a new conversation</p>} />
          <Route path="/chat/:chatId" element={<p>the conversation</p>} />
          <Route path="/templates/:templateId" element={<p>the template</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Double-click a chat row the way a person does. */
async function openTheChatRow(
  user: ReturnType<typeof userEvent.setup>,
  title = "Warehouse spike",
): Promise<void> {
  const row = await screen.findByText(title);
  await user.dblClick(row);
}

/** Lists a chat's or a template's files the way a person does: Browse files
 *  on the row's context menu. */
async function browseFiles(title = "Warehouse spike"): Promise<void> {
  const menu = await rowMenu(title);
  await userEvent.click(within(menu).getByRole("menuitem", { name: /^Browse files/ }));
}

/** What the page shows, as text — the one place a directory's name could leak. */
function everythingShown(): string {
  return document.body.textContent ?? "";
}

beforeEach(() => {
  requests = [];
  stubViewport();
  stubApi();
});
afterEach(() => {
  vi.unstubAllGlobals();
  // `unstubAllGlobals` only undoes `stubGlobal`; a `spyOn` survives it, so the
  // download case's patched `document.createElement` would otherwise stay
  // installed for every case after it.
  vi.restoreAllMocks();
});

describe("opening a chat from the file browser", () => {
  it("opens the conversation on a double-click, asking nothing first", async () => {
    const user = userEvent.setup();
    mount();

    await openTheChatRow(user);

    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/chat/cht_9"));
    expect(screen.getByText("the conversation")).toBeInTheDocument();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("walks into the chat's files at the directory the server named", async () => {
    mount();
    await browseFiles();

    // The page lands ON the working directory, not on the folder around it.
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_files"));
    // The working material is listed like any folder's contents…
    expect(await screen.findByText("findings.csv")).toBeInTheDocument();
    expect(screen.getByText("data")).toBeInTheDocument();
    // …and the chat's own records beside it are not in the listing at all.
    expect(screen.queryByText("manifest.json")).toBeNull();
    expect(screen.queryByText(".runtime")).toBeNull();
    // The directory's name is the box's business: nowhere on the page.
    expect(everythingShown()).not.toMatch(/scratch/i);
  });

  it("names the chat on the trail, never the directory or the folder it is stored as", async () => {
    mount();
    await browseFiles();

    const trail = await screen.findByRole("navigation", { name: "Breadcrumb" });
    // The listing says nothing about what a chat's files ride: it was a second
    // sentence over the rows saying what the live line already says.
    expect(
      screen.queryByText("These are the chat's files. Anyone who can open the chat can see them."),
    ).toBeNull();
    await waitFor(() => expect(within(trail).getByText("Warehouse spike")).toBeInTheDocument());
    // The trail continues from the home the chat lives in: the chat folder
    // itself is never a segment, so the files sit where the chat does.
    expect(within(trail).getByText("Home")).toBeInTheDocument();
    expect(within(trail).queryByText(/alkerachat/)).toBeNull();
    expect(within(trail).queryByText(/scratch/i)).toBeNull();
  });

  it("sends a deep link to the chat folder on to the chat's files", async () => {
    mount("/files/nd_chat");

    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_files"));
    expect(await screen.findByText("findings.csv")).toBeInTheDocument();
    expect(screen.queryByText("manifest.json")).toBeNull();
    // The chat folder the link named is not a segment: the working directory
    // stands where the chat would have, once, under the chat's own title.
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    await waitFor(() => expect(within(trail).getAllByText("Warehouse spike")).toHaveLength(1));
    expect(within(trail).queryByText(/scratch|alkerachat/i)).toBeNull();
  });

  it("lists a chat from before it had a working directory at the chat itself", async () => {
    mount();
    await browseFiles("Old notes");

    // No directory was named, so the folder itself is what there is to browse —
    // its files listed, the box's runtime state still kept out of sight.
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_old"));
    expect(await screen.findByText("draft.md")).toBeInTheDocument();
    expect(screen.queryByText(".runtime")).toBeNull();
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(trail).getByText("Old notes")).toBeInTheDocument();
  });

  it("lists an ordinary folder", async () => {
    const user = userEvent.setup();
    mount("/files/nd_files");
    const row = await screen.findByText("data");

    await user.dblClick(row);

    // A folder is its contents, so there is nothing to choose between.
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_data"));
    expect(screen.queryByRole("menu")).toBeNull();
  });
});

/** Reaches a row's context menu the way a person does. */
async function rowMenu(name: string): Promise<HTMLElement> {
  const row = await waitFor(() =>
    within(screen.getByRole("treegrid")).getByRole("row", { name: new RegExp(name) }),
  );
  const cell = within(row).getAllByRole("gridcell")[0] as HTMLElement;
  await userEvent.pointer([{ target: cell }, { keys: "[MouseRight]", target: cell }]);
  return screen.findByRole("menu");
}

describe("a file inside a chat is an ordinary file", () => {
  it("downloads through the same content route as any other file", async () => {
    mount("/files/nd_files");
    const anchors: HTMLAnchorElement[] = [];
    const create = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const element = create(tag);
      if (tag === "a") {
        const anchor = element as HTMLAnchorElement;
        anchor.click = vi.fn();
        anchors.push(anchor);
      }
      return element;
    });

    const menu = await rowMenu("findings\\.csv");
    const download = within(menu).getByRole("menuitem", { name: /^Download/ });
    // The chat's own seal stops at the chat: the file inside is downloadable.
    expect(download).not.toHaveAttribute("aria-disabled", "true");
    await userEvent.click(download);

    await waitFor(() => expect(anchors).toHaveLength(1));
    expect(anchors[0]!.getAttribute("href")).toContain("/drives/dr_1/items/nd_inside/content");
    expect(anchors[0]!.getAttribute("download")).toBe("findings.csv");
  });

  it("hands the next case an unpatched document.createElement", () => {
    // The download case above spies on `document.createElement` to catch the
    // anchor it builds. Nothing here restores spies except the file's own
    // teardown, so this reads the seam every later case depends on: an element
    // built now is a real one, and its `click` still navigates.
    expect(vi.isMockFunction(document.createElement)).toBe(false);
    expect(vi.isMockFunction(document.createElement("a").click)).toBe(false);
  });

  it("renames through the same route as any other file", async () => {
    mount("/files/nd_files");
    const menu = await rowMenu("findings\\.csv");
    await userEvent.click(within(menu).getByRole("menuitem", { name: /^Rename/ }));

    const field = await screen.findByDisplayValue("findings.csv");
    fireEvent.change(field, { target: { value: "results.csv" } });
    fireEvent.keyDown(field, { key: "Enter" });

    await waitFor(() => {
      const wrote = requests.find(
        (call) => call.method === "PATCH" && call.url.includes("/items/nd_inside"),
      );
      expect(wrote?.body).toContain("results.csv");
    });
  });

  it("shares one file on its own, against the file's own node", async () => {
    mount("/files/nd_files");
    const menu = await rowMenu("findings\\.csv");
    await userEvent.click(within(menu).getByRole("menuitem", { name: /^Share/ }));

    // The dialog reads the FILE's grants — the inherited rows are what name the
    // chat above it, which is the rule this whole surface rests on.
    await waitFor(() =>
      expect(
        requests.some(
          (call) => call.method === "GET" && call.url.includes("/items/nd_inside/permissions"),
        ),
      ).toBe(true),
    );
  });
});

describe("the chat row's context menu", () => {
  it("opens the conversation from Open chat, asking nothing first", async () => {
    mount();
    const menu = await rowMenu("Warehouse spike");

    await userEvent.click(within(menu).getByRole("menuitem", { name: /^Open chat/ }));

    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/chat/cht_9"));
    expect(screen.getByText("the conversation")).toBeInTheDocument();
  });

  it("walks into the chat's files from Browse files", async () => {
    mount();
    await browseFiles();

    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_files"));
  });
});

// A chat template keeps its files the same way a chat does, so the listing owes
// it the same treatment: walked into, the rows are the template's and the trail
// says the template's title — never the machine-minted folder name underneath.

describe("a chat template's files", () => {
  it("walks into the template's working folder and names the template on the trail", async () => {
    mount();
    await browseFiles("Warehouse audit");

    await waitFor(() =>
      expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_tpl_files"),
    );
    expect(await screen.findByText("checklist.md")).toBeInTheDocument();
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    await waitFor(() => expect(within(trail).getByText("Warehouse audit")).toBeInTheDocument());
    // Neither the working folder's own name nor the filesystem name of the template.
    expect(within(trail).queryByText(/seed/i)).toBeNull();
    expect(everythingShown()).not.toMatch(/alkerachat\.template/);
  });

  it("says what these files ARE — a starting point, not a copy of something", async () => {
    mount("/files/nd_tpl_files");

    // The chat's sentence would be a lie here: a template's files are not visible
    // to whoever can open a conversation, they are what a new one begins with.
    expect(
      await screen.findByText(
        "These are the template's files. A chat started from this template begins with a copy of them.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("These are the chat's files. Anyone who can open the chat can see them."),
    ).toBeNull();
  });

  it("sends a deep link to the template folder on to the template's files", async () => {
    mount("/files/nd_tpl");

    // The working folder is the effective root of a template exactly as it is of
    // a chat: the folder around it holds only the template's own records.
    await waitFor(() =>
      expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_tpl_files"),
    );
    expect(await screen.findByText("checklist.md")).toBeInTheDocument();
  });
});
