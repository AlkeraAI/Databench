import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen, objectRoute } from "@/pages/workspace/files/FilesPage";

// A node in the tree that IS something else — a chat, a saved report — must open
// that thing. Opening it as a folder would show a person the files the chat keeps,
// which is precisely what a chat folder is not for; opening it as a file would do
// nothing at all. Both are pinned through the real page, by where the router lands.

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    name: "report.csv",
    nameDisplay: "report.csv",
    nameEncoding: "utf-8",
    pathBytes: "/home/report.csv",
    path: "/home/report.csv",
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
const FOLDER = item({ id: "nd_plain", kind: "folder", name: "papers", nameDisplay: "papers" });
/** A chat: the artifacts lane ships it as a FOLDER carrying the object facet, so the
 *  dispatch must not key on the node's kind. */
const CHAT = item({
  id: "nd_chat",
  kind: "folder",
  name: "Q3 review.alkerachat",
  nameDisplay: "Q3 review.alkerachat",
  object: { type: "chat", id: "cht_9", web_url: "/chat/cht_9" },
} as unknown as Partial<Item>);

/** An artifact an agent left behind: the deliverable a reader double-clicks to
 *  READ. The mime is the server's sniff, which is what decides. */
const PDF = item({
  id: "nd_pdf",
  name: "Q3 brief.pdf",
  nameDisplay: "Q3 brief.pdf",
  file: { mime_type: "application/pdf" },
} as unknown as Partial<Item>);
/** A file nothing renders in a browsing context. */
const ARCHIVE = item({
  id: "nd_zip",
  name: "last-quarter.zip",
  nameDisplay: "last-quarter.zip",
  file: { mime_type: "application/zip" },
} as unknown as Partial<Item>);

function stubApi(children: Item[]): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // The search read is a raw `fetch` with a relative url, so the stub must not
      // try to build a `Request` out of it — jsdom has no base to resolve one against.
      void init;
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
      if (url.includes("/items/")) return answer(HOME);
      return answer({});
    }),
  );
  stubViewport();
}

/** A wide viewport, so the browser is the treegrid rather than a narrow sheet. */
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

/** Reports the path the router is on, so what is asserted is where a person landed. */
function Where() {
  const location = useLocation();
  return <output data-testid="where">{location.pathname}</output>;
}

function mount(at = "/files/nd_home") {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[at]}>
        <Where />
        <Routes>
          <Route path="/files" element={<FilesScreen />} />
          <Route path="/files/:nodeId" element={<FilesScreen />} />
          <Route path="/chat/:chatId" element={<p>the chat</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The artefact an agent writes into a chat's `outputs/`: one file, everything
 *  embedded, produced to be read rather than filed. */
const REPORT = item({
  id: "nd_html",
  name: "summary.html",
  nameDisplay: "summary.html",
  file: { mime_type: "text/html", size: 2048 },
} as unknown as Partial<Item>);

beforeEach(() => stubApi([FOLDER, CHAT, item(), REPORT, PDF, ARCHIVE]));
afterEach(() => vi.unstubAllGlobals());

describe("opening a node that is something else", () => {
  it("opens the chat rather than the folder it is stored as", async () => {
    const user = userEvent.setup();
    mount();
    const row = await screen.findByText("Q3 review.alkerachat");

    await user.dblClick(row);

    // A chat is a page and a folder; opening it means the conversation, and
    // its files are the context menu's Browse files.
    expect(screen.queryByRole("menu")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/chat/cht_9"));
    expect(screen.getByText("the chat")).toBeInTheDocument();
  });

  it("asks nothing of a folder-backed object that has only one way in", async () => {
    const user = userEvent.setup();
    // A report is a page, and a page is all it is: nothing of it is browsed, so
    // the gesture has nothing to ask about and goes straight there. Asking would
    // put a menu with one row in front of a person who already knew.
    const REPORT_NODE = item({
      id: "nd_report",
      kind: "folder",
      name: "Q3 numbers.alkerareport",
      nameDisplay: "Q3 numbers.alkerareport",
      object: { type: "report", id: "rpt_3", web_url: "/reports/rpt_3" },
    } as unknown as Partial<Item>);
    stubApi([FOLDER, REPORT_NODE]);
    mount();

    await user.dblClick(await screen.findByText("Q3 numbers.alkerareport"));

    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/reports/rpt_3"));
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("still walks into an ordinary folder", async () => {
    const user = userEvent.setup();
    mount();
    const row = await screen.findByText("papers");

    await user.dblClick(row);

    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_plain"));
  });

  it("leaves a file the browser cannot render where it is", async () => {
    const user = userEvent.setup();
    const open = vi.fn();
    vi.stubGlobal("open", open);
    stubApi([FOLDER, CHAT, ARCHIVE]);
    mount();
    const row = await screen.findByText("last-quarter.zip");

    await user.dblClick(row);

    // Nothing renders a zip, so the listing stays where the person left it
    // rather than opening a tab on a download.
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_home"));
    expect(open).not.toHaveBeenCalled();
  });

  it("draws an artifact the registry claims, over the listing it came from", async () => {
    const user = userEvent.setup();
    const open = vi.fn();
    vi.stubGlobal("open", open);
    stubApi([FOLDER, CHAT, PDF]);
    mount();
    const row = await screen.findByText("Q3 brief.pdf");

    await user.dblClick(row);

    // The preview, not a tab: a reader who opened one artifact means to look at
    // it and then at the next one, and a tab per file loses the listing.
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getAllByText("Q3 brief.pdf").length).toBeGreaterThan(0);
    expect(open).not.toHaveBeenCalled();
    // And the person keeps their place.
    expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_home");
  });
});

describe("opening a node that is something else in a new tab", () => {
  /** Reaches the menu row the way a person does: right-click the row, click the item. */
  async function openInNewTab(name: string): Promise<void> {
    const row = await waitFor(() =>
      within(screen.getByRole("treegrid")).getByRole("row", { name: new RegExp(name) }),
    );
    fireEvent.contextMenu(within(row).getAllByRole("gridcell")[0] as HTMLElement);
    const menu = await screen.findByRole("menu");
    fireEvent.click(within(menu).getByRole("menuitem", { name: /^Open in new tab/ }));
  }

  it("opens the chat, not a listing of the files the chat keeps", async () => {
    const opened = vi.fn();
    vi.stubGlobal("open", opened);
    mount();

    await openInNewTab("Q3 review\\.alkerachat");

    // The node IS the chat, so the new tab is the chat's page. A `/files/<node>` url
    // here would list the chat's attachments, outputs and scratch — the one thing a
    // chat folder is not for, and what the double-click path already refuses.
    await waitFor(() => expect(opened).toHaveBeenCalled());
    expect(opened.mock.calls[0]?.[0]).toBe("/chat/cht_9");
  });

  it("still opens an ordinary folder as a folder", async () => {
    const opened = vi.fn();
    vi.stubGlobal("open", opened);
    mount();

    await openInNewTab("papers");

    await waitFor(() => expect(opened).toHaveBeenCalled());
    expect(opened.mock.calls[0]?.[0]).toBe("/files/nd_plain");
  });
});

// A deliverable is the other thing a node can be: the one self-contained file an
// agent is told to write — a PDF, or a page with everything embedded. Opening it
// has to show it, or half of what the product produces can only be saved to disk.

describe("opening a deliverable", () => {
  it("shows a self-contained page in the preview, not in a tab", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const user = userEvent.setup();
    mount();
    const row = await screen.findByText("summary.html");

    await user.dblClick(row);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getAllByText("summary.html").length).toBeGreaterThan(0);
    // The reading surface is here, so no tab is spent on it — and the folder the
    // report was opened from is still underneath.
    expect(open).not.toHaveBeenCalled();
    expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_home");
  });

  it("shows a PDF the same way", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const user = userEvent.setup();
    mount();

    await user.dblClick(await screen.findByText("Q3 brief.pdf"));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getAllByText("Q3 brief.pdf").length).toBeGreaterThan(0);
    expect(open).not.toHaveBeenCalled();
  });

  it("opens no tab for bytes the browser would only download", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const user = userEvent.setup();
    mount();

    await user.dblClick(await screen.findByText("last-quarter.zip"));

    // A tab showing a download is worse than no tab: the archive stays put.
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_home"));
    expect(open).not.toHaveBeenCalled();
  });
});

describe("the object route", () => {
  it("is the facet's page, for an object node and for a folder that carries one", () => {
    expect(objectRoute(CHAT)).toBe("/chat/cht_9");
    expect(
      objectRoute(
        item({
          kind: "object",
          object: { type: "report", id: "r1", web_url: "/reports/r1" },
        } as unknown as Partial<Item>),
      ),
    ).toBe("/reports/r1");
  });

  it("reduces a same-origin absolute url to a path the SPA can route", () => {
    expect(
      objectRoute(
        item({
          object: { type: "chat", id: "c", web_url: `${window.location.origin}/chat/c?tab=files` },
        } as unknown as Partial<Item>),
      ),
    ).toBe("/chat/c?tab=files");
  });

  it("refuses to route a url that leads off this origin", () => {
    // A node whose facet names another host is not a page this router owns, and
    // navigating to it as if it were a path would land on a route that does not exist.
    expect(
      objectRoute(
        item({
          object: { type: "chat", id: "c", web_url: "https://elsewhere.example/chat/c" },
        } as unknown as Partial<Item>),
      ),
    ).toBeNull();
  });

  it("is null for a node that is only itself", () => {
    expect(objectRoute(item())).toBeNull();
    expect(objectRoute(FOLDER)).toBeNull();
    expect(
      objectRoute(
        item({ object: { type: "chat", id: "c", web_url: "" } } as unknown as Partial<Item>),
      ),
    ).toBeNull();
  });
});
