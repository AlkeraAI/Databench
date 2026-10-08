/**
 * What a double-click on a FILE does, now that both terminal answers are wired.
 *
 * Until these branches landed, opening a file in the drive did nothing a person
 * could see: the dispatch fell through the folder and folder-object cases and
 * returned. There are only two honest answers, and which one a row gets is the
 * preview registry's business, not a list of extensions — a row something can
 * draw opens in the large preview, over the listing it came from; a row nothing
 * draws is saved, through the same anchor the row's own Download uses.
 *
 * Driven through the real page so the proof is the gesture, not the callback.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";
import { objectRoute } from "@/lib/files/objectRoute";

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
    parentId: "nd_home",
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

/** A row the registry draws. */
const SHOT = item({
  id: "nd_png",
  name: "chart.png",
  nameDisplay: "chart.png",
  file: { mime_type: "image/png", size: 2048, content_hash: "sha256-test", scan_state: "clean" },
} as unknown as Partial<Item>);

/** A second drawable row, so a step has somewhere to land. */
const SHOT_2 = item({
  id: "nd_png2",
  name: "after.png",
  nameDisplay: "after.png",
  file: { mime_type: "image/png", size: 4096, content_hash: "sha256-test", scan_state: "clean" },
} as unknown as Partial<Item>);

/** A row nothing draws. */
const ARCHIVE = item({
  id: "nd_zip",
  name: "audit.zip",
  nameDisplay: "audit.zip",
  file: { mime_type: "application/zip", size: 900, content_hash: "sha256-test", scan_state: "clean" },
} as unknown as Partial<Item>);

/** The type a server falls back to when it sniffed nothing it recognises — the
 *  row a double-click must still answer. */
const BLOB = item({
  id: "nd_bin",
  name: "capture.bin",
  nameDisplay: "capture.bin",
  file: {
    mime_type: "application/octet-stream",
    size: 3_145_728,
    content_hash: "sha256-test",
    scan_state: "clean",
  },
} as unknown as Partial<Item>);

/** A template: a folder whose facet names the page it IS. */
const TEMPLATE = item({
  id: "nd_tpl",
  kind: "folder",
  name: "6c2a1b90-0000-4000-8000-000000000002.alkerachat.template",
  nameDisplay: "6c2a1b90-0000-4000-8000-000000000002.alkerachat.template",
  object: {
    type: "chat_template",
    id: "tpl_4",
    title: "Warehouse audit",
    web_url: "/templates/tpl_4",
    metadata: { files_node_id: "nd_tpl_files" },
  },
} as unknown as Partial<Item>);

/** A text file past the text reader's two-mebibyte budget, and a type the
 *  content route WOULD serve inline. Both facts together would tempt a double-click
 *  into `window.open` — a tab in a browser, and nothing at all
 *  on a page that blocks popups or a person who never looked. */
const LONG_LINE = item({
  id: "nd_txt",
  name: "one-long-line.txt",
  nameDisplay: "one-long-line.txt",
  file: {
    mime_type: "text/plain",
    size: 3 * 1024 * 1024,
    content_hash: "sha256-test",
    scan_state: "clean",
  },
} as unknown as Partial<Item>);

const ROWS = [SHOT, SHOT_2, ARCHIVE, BLOB, LONG_LINE, TEMPLATE];

/** Every grant the page bought, so a preview that draws nothing can be held to
 *  spending nothing. A grant is a bearer credential for as long as it lives. */
let grants = 0;

function grantsMinted(): number {
  return grants;
}

function stubApi(): void {
  grants = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/content-grants")) grants += 1;
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
      if (url.includes("/children")) return answer({ value: ROWS, nextMarker: null });
      if (url.includes("/permissions")) return answer({ value: [] });
      if (url.includes("/items/")) return answer(HOME);
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
          <Route path="/files/:nodeId" element={<FilesScreen />} />
          <Route path="/chat" element={<p>a new chat</p>} />
          <Route path="/templates/:templateId" element={<p>THE TEMPLATE PAGE</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function rowNamed(title: string): Promise<HTMLElement> {
  return waitFor(() =>
    within(screen.getByRole("treegrid")).getByRole("row", { name: new RegExp(title) }),
  );
}

async function openRow(title: string): Promise<HTMLElement> {
  const row = await rowNamed(title);
  fireEvent.doubleClick(within(row).getAllByRole("gridcell")[0] as HTMLElement);
  return row;
}

/** What the browser was actually asked to save, without a jsdom navigation. */
function watchSaves(): { href: string; name: string }[] {
  const saved: { href: string; name: string }[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    saved.push({ href: this.href, name: this.download });
  });
  return saved;
}

beforeEach(stubApi);
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("opening a file", () => {
  it("opens a type nothing draws onto the card that says so, with the way out on it", async () => {
    mount();

    await openRow("capture.bin");

    // A double-click on an unknown binary must still open something that says
    // what the file is and that there is nothing to draw.
    const dialog = await screen.findByRole("dialog");
    const card = within(dialog).getByTestId("preview-fallback");
    expect(card).toHaveTextContent("capture.bin");
    expect(card).toHaveTextContent("application/octet-stream");
    expect(card).toHaveTextContent("Preview is not available for this type");
    // The same actions the image preview offers, so the two stop differing on
    // everything but what a tab could actually render.
    expect(within(dialog).getAllByRole("button", { name: "Download" }).length).toBeGreaterThan(0);
    expect(within(dialog).getByRole("button", { name: "Open in Files" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Copy link" })).toBeInTheDocument();
    expect(within(dialog).getAllByRole("button", { name: "Close" }).length).toBeGreaterThan(0);
    // Including the tab: nothing draws it in the pane, but the bytes are still
    // reachable, and an action row that thinned out with the kind made the same
    // file look differently capable depending on its extension.
    expect(within(dialog).getByRole("button", { name: "Open in new tab" })).toBeInTheDocument();
    // Over the listing, not a place of its own.
    expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_home");
  });

  it("opens a 3 MB text file into the preview and asks for its bytes", async () => {
    const opened = vi.fn();
    vi.stubGlobal("open", opened);
    mount();

    await openRow("one-long-line.txt");

    // Its size once kept it out of the preview behind a card; it is read in
    // windows now, so a double-click opens it like any other text file.
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(grantsMinted()).toBeGreaterThan(0));
    expect(dialog).not.toHaveTextContent(/Too large/);
    expect(opened).not.toHaveBeenCalled();
  });

  it("the card's Download saves the bytes as an attachment, under the listing's name", async () => {
    const saved = watchSaves();
    mount();

    await openRow("audit.zip");
    const card = within(await screen.findByRole("dialog")).getByTestId("preview-fallback");
    fireEvent.click(within(card).getByRole("button", { name: "Download" }));

    await waitFor(() => expect(saved).toHaveLength(1));
    // The content route, asked for an attachment so a type the browser would
    // rather render inline is still saved — and no second credential minted.
    expect(saved[0]?.href).toContain("/files/drives/dr_1/items/nd_zip/content");
    expect(saved[0]?.href).toContain("disposition=attachment");
    expect(saved[0]?.name).toBe("audit.zip");
  });

  it("asks for no bytes for a file it cannot draw — no grant is minted", async () => {
    const before = grantsMinted();
    mount();

    await openRow("capture.bin");
    await screen.findByRole("dialog");

    // The fallback plan needs nothing, so the card costs no request: a preview
    // that cannot draw must not spend a credential to say so.
    expect(grantsMinted()).toBe(before);
  });

  it("draws a file the registry claims in the preview, over the listing", async () => {
    mount();

    await openRow("chart.png");

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getAllByText("chart.png").length).toBeGreaterThan(0);
    expect(within(dialog).getAllByRole("button", { name: "Download" }).length).toBeGreaterThan(0);
    expect(within(dialog).getByRole("button", { name: "Open in Files" })).toBeInTheDocument();
    // Still in the folder it was opened from — the preview is over the listing,
    // not a place of its own.
    expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_home");
  });

  it("steps to the next drawable row and back, skipping what nothing draws", async () => {
    mount();
    await openRow("chart.png");
    const dialog = await screen.findByRole("dialog");

    fireEvent.keyDown(document, { key: "ArrowRight" });
    await waitFor(() => expect(within(dialog).getAllByText("after.png").length).toBeGreaterThan(0));

    // The zip and the binary sit between it and the text file and are never
    // landed on.
    fireEvent.keyDown(document, { key: "ArrowRight" });
    await waitFor(() =>
      expect(within(dialog).getAllByText("one-long-line.txt").length).toBeGreaterThan(0),
    );
    expect(within(dialog).queryByText("audit.zip")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("capture.bin")).not.toBeInTheDocument();

    fireEvent.keyDown(document, { key: "ArrowRight" });
    // The end of the run offers no further step rather than wrapping.
    expect(within(dialog).getAllByText("one-long-line.txt").length).toBeGreaterThan(0);

    fireEvent.keyDown(document, { key: "ArrowLeft" });
    await waitFor(() => expect(within(dialog).getAllByText("after.png").length).toBeGreaterThan(0));
  });

  it("puts focus back on a row when the preview closes", async () => {
    mount();
    await openRow("chart.png");
    const dialog = await screen.findByRole("dialog");

    fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[0] as HTMLElement);

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const row = await rowNamed("chart.png");
    await waitFor(() => expect(row.contains(document.activeElement)).toBe(true));
  });

  it("saves from inside the preview through the same content route", async () => {
    const saved = watchSaves();
    mount();
    await openRow("chart.png");
    const dialog = await screen.findByRole("dialog");

    fireEvent.click(within(dialog).getAllByRole("button", { name: "Download" })[0] as HTMLElement);

    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0]?.href).toContain("/files/drives/dr_1/items/nd_png/content");
    expect(saved[0]?.href).toContain("disposition=attachment");
  });
});

describe("a template's two destinations", () => {
  it("lists the folder when Browse files was picked, never the page", async () => {
    mount();
    const row = await rowNamed("Warehouse audit");
    const cell = within(row).getAllByRole("gridcell")[0] as HTMLElement;
    fireEvent.contextMenu(cell);

    const menu = await screen.findByRole("menu");
    fireEvent.click(within(menu).getByRole("menuitem", { name: /^Browse files/ }));

    await waitFor(() =>
      expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_tpl_files"),
    );
    expect(screen.queryByText("THE TEMPLATE PAGE")).not.toBeInTheDocument();
  });

  it("routes a template row to the page its facet names", () => {
    // The one place every surface asks — the row's open dispatch, the details
    // pane's link, a search result — so all of them reach the template's page
    // rather than each deriving an address of its own.
    expect(objectRoute(TEMPLATE)).toBe("/templates/tpl_4");

    // A deployment that configured no page URL sends no `web_url`, and there is
    // then no page to open: the row opens on its files instead.
    const bare = item({
      id: "nd_bare",
      kind: "folder",
      object: { type: "chat_template", id: "tpl_9" },
    } as unknown as Partial<Item>);
    expect(objectRoute(bare)).toBeNull();

    // A chat is untouched: its page is where it opens.
    const chat = item({
      id: "nd_chat",
      kind: "folder",
      object: { type: "chat", id: "cht_9", web_url: "/chat/cht_9" },
    } as unknown as Partial<Item>);
    expect(objectRoute(chat)).toBe("/chat/cht_9");
  });
});
