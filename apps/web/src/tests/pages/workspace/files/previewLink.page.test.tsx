// @vitest-environment jsdom
//
// Where a link to a file in Files actually lands.
//
// A `/files/<id>` link is the one URL the product hands out, and the id in it is
// whatever was shared. Three landings come out of that, and they are pinned here
// through the REAL page — the reads, the listing, the trail, the preview — because
// the interesting part is what each one does NOT render:
//
//  * a file inside a folder the reader can also open: the folder's rows, that row
//    selected, the file open over it, and closing it leaves the URL on the folder;
//  * a file shared ALONE: no siblings, no path, no create controls, and not one
//    request for the folder's children — the reader was granted the file and
//    nothing around it, and a page that listed the folder anyway would say so;
//  * an id that names nothing they may have: one card, identical whether the node
//    is missing or merely unshared, and one request to find that out.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { DocumentTitle } from "@/app/documentTitle";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";
import { NOT_HERE } from "@/pages/workspace/files/NotHere";
import { SOLO_LINE } from "@/pages/workspace/files/SoloItem";

const DRIVE = "dr_1";
const ROOT = "nd_root";
const HOME = "nd_home";
const FOLDER = "nd_folder";
const FILE = "nd_file";
const SIBLING = "nd_sibling";
const NOTE = "nd_note";

function item(over: Partial<Item> = {}): Item {
  return {
    id: FILE,
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    name: "q3-report.png",
    nameDisplay: "q3-report.png",
    nameEncoding: "utf-8",
    parentId: FOLDER,
    pathBytes: "/home/dana/reports/q3-report.png",
    path: "/home/dana/reports/q3-report.png",
    etag: "et_1",
    ctag: "ct_1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    file: { mime_type: "image/png", size: 2048, content_hash: "sha256-test", scan_state: "clean" },
    attrs: { mtime: "2026-09-01T10:00:00Z" },
    capabilities: {
      can_read: true,
      can_write: true,
      can_share: true,
      can_delete: true,
      can_download: true,
    },
    ...over,
  } as Item;
}

const ROOT_ROWS: Item[] = [
  item({ id: "nd_home_container", kind: "folder", name: "home", nameDisplay: "home", parentId: ROOT }),
  item({ id: "nd_teams", kind: "folder", name: "Teams", nameDisplay: "Teams", parentId: ROOT }),
];

/** The shared file, the folder it lives in, and a sibling whose NAME is the thing
 *  a solo landing must never put on screen. */
const NODES: Record<string, Item> = {
  [HOME]: item({ id: HOME, kind: "folder", name: "dana", nameDisplay: "dana", parentId: "nd_home_container", path: "/home/dana", pathBytes: "/home/dana" }),
  [FOLDER]: item({
    id: FOLDER,
    kind: "folder",
    name: "reports",
    nameDisplay: "reports",
    parentId: HOME,
    path: "/home/dana/reports",
    pathBytes: "/home/dana/reports",
  }),
  [FILE]: item(),
  [SIBLING]: item({ id: SIBLING, name: "salaries.csv", nameDisplay: "salaries.csv" }),
  // A document that NAMES the two files beside it: the reason the modal needs a
  // resolver at all.
  [NOTE]: item({
    id: NOTE,
    name: "notes.md",
    nameDisplay: "notes.md",
    pathBytes: "/home/dana/reports/notes.md",
    path: "/home/dana/reports/notes.md",
    file: {
      mime_type: "text/markdown",
      size: 120,
      content_hash: "sha256-test",
      scan_state: "clean",
    } as Item["file"],
  }),
};

/** What `notes.md` says: an image and two links, all folder-relative — and one
 *  of the links names a file that is not there. */
const NOTE_TEXT =
  "![the plot](q3-report.png)\n\nSee [the numbers](salaries.csv) and [the old one](gone.csv).";

const CHILDREN: Record<string, Item[]> = {
  [ROOT]: ROOT_ROWS,
  [FOLDER]: [NODES[FILE] as Item, NODES[SIBLING] as Item, NODES[NOTE] as Item],
  [HOME]: [NODES[FOLDER] as Item],
};

interface Network {
  /** Every URL the page asked for, in order. */
  urls: string[];
  /** How many requests named this id, whatever route they were on. */
  about: (id: string) => number;
}

/** The whole drive, scripted. `refuse` maps a node id to the status its read
 *  answers with, which is how a folder the reader may not open is expressed. */
function stubNetwork(refuse: Record<string, number> = {}): Network {
  const urls: string[] = [];
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      urls.push(url);
      if (url.includes("/config")) return json({ product_name: "Databench" });
      const grant = /\/items\/([^/]+)\/content-grants/.exec(url);
      if (grant) {
        // A grant per node, so the bytes served below can be that node's own.
        return json({
          url: `http://files.localhost:8000/c/${grant[1] as string}`,
          expiresAt: new Date(Date.now() + 300_000).toISOString(),
          kind: "file",
          etag: "et_1",
        });
      }
      if (url.includes("/leases")) return json([]);
      if (url.includes("/permissions") || url.includes("/versions")) {
        return json({ value: [], nextMarker: null });
      }
      if (url.includes("/search")) return json({ value: [], nextMarker: null });
      if (url.includes("/trash")) return json({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return json({ id: DRIVE, orgId: "or_1", rootId: ROOT, homeId: HOME, quotaBytes: 0 });
      }
      const children = /\/items\/([^/]+)\/children/.exec(url);
      if (children) {
        const id = children[1] as string;
        const refused = refuse[id];
        if (refused) return json({ code: "files.not_found", message: "no" }, refused);
        return json({ value: CHILDREN[id] ?? [], nextMarker: null });
      }
      const one = /\/items\/([^/?]+)/.exec(url);
      if (one) {
        const id = one[1] as string;
        const refused = refuse[id];
        if (refused) return json({ code: "files.not_found", message: "no" }, refused);
        const node = NODES[id];
        return node ? json(node) : json({ code: "files.not_found", message: "no" }, 404);
      }
      const spent = /^http:\/\/files\.localhost:8000\/c\/(.+)$/.exec(url);
      if (spent) {
        const id = spent[1] as string;
        return new Response(id === NOTE ? NOTE_TEXT : "bytes", {
          status: 200,
          headers: { "content-type": NODES[id]?.file?.mime_type ?? "image/png" },
        });
      }
      return new Response("bytes", { status: 200, headers: { "content-type": "image/png" } });
    }),
  );
  return { urls, about: (id) => urls.filter((url) => url.includes(id)).length };
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
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

/** The browser's Back button, as a control a test can press. */
function Back() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(-1)}>
      Back
    </button>
  );
}

function mount(at: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[at]}>
        <DocumentTitle>
          <Where />
          <Back />
          <Routes>
            <Route path="/files" element={<FilesScreen platform="other" />} />
            <Route path="/files/:nodeId" element={<FilesScreen platform="other" />} />
          </Routes>
        </DocumentTitle>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const where = () => screen.getByTestId("where").textContent;

beforeEach(() => {
  stubViewport();
  const url = URL as unknown as Record<string, unknown>;
  url.createObjectURL = vi.fn(() => "blob:one");
  url.revokeObjectURL = vi.fn();
  document.title = "before";
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("a link to a file whose folder the reader can open", () => {
  it("lists the folder, selects the row and opens the file over it", async () => {
    stubNetwork();
    mount(`/files/${FILE}`);

    const dialog = await screen.findByRole("dialog", { name: /q3-report\.png/ });
    expect(dialog).toBeInTheDocument();
    // The folder's rows are what is underneath — the reader can see where the
    // file lives and what is beside it.
    await screen.findByText("salaries.csv");
    await waitFor(() => {
      const row = document.querySelector(`[data-row-id="${FILE}"]`);
      expect(row?.getAttribute("aria-selected")).toBe("true");
    });
  });

  it("names the file in the browser tab", async () => {
    stubNetwork();
    mount(`/files/${FILE}`);

    await waitFor(() => expect(document.title).toContain("q3-report.png"));
  });

  it("leaves the URL on the folder when the preview is closed", async () => {
    stubNetwork();
    mount(`/files/${FILE}`);

    const dialog = await screen.findByRole("dialog", { name: /q3-report\.png/ });
    // The sheet carries its own dismiss as well as the action row's; either is
    // the reader closing it, and the action row's is the one under the pointer.
    const closes = within(dialog).getAllByRole("button", { name: "Close" });
    await userEvent.click(closes[closes.length - 1] as HTMLElement);

    await waitFor(() => expect(where()).toBe(`/files/${FOLDER}`));
    expect(screen.queryByRole("dialog", { name: /q3-report\.png/ })).toBeNull();
    // Still the folder's listing, not a second load of it.
    expect(screen.getByText("salaries.csv")).toBeInTheDocument();
  });
});

// A preview is a place, not a mode: it has its own address, so it can be linked
// to, reloaded onto, and — the reason it matters — left with the Back button,
// which is what a reader who opened a file over a listing reaches for first.

describe("the address a preview carries", () => {
  async function openTheFile(): Promise<void> {
    stubNetwork();
    mount(`/files/${FOLDER}`);
    await screen.findByText("q3-report.png");
    await userEvent.dblClick(screen.getByText("q3-report.png"));
    await screen.findByRole("dialog", { name: /q3-report\.png/ });
  }

  it("names the row and the kind of preview it opened", async () => {
    await openTheFile();
    // The kind is the registry's answer for these bytes, not the extension: a
    // link carries what the reader will actually be looking at.
    await waitFor(() =>
      expect(where()).toBe(`/files/${FOLDER}?preview=${FILE}&kind=image`),
    );
  });

  it("closes on Back, leaving the listing underneath", async () => {
    await openTheFile();
    await waitFor(() => expect(where()).toContain("preview="));

    await userEvent.click(screen.getByRole("button", { name: "Back" }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: /q3-report\.png/ })).toBeNull(),
    );
    expect(where()).toBe(`/files/${FOLDER}`);
    // The folder is still listed: Back closed the preview, it did not leave.
    expect(screen.getByText("salaries.csv")).toBeInTheDocument();
  });

  it("drops the parameters when the preview is closed from its own action row", async () => {
    await openTheFile();
    const dialog = screen.getByRole("dialog", { name: /q3-report\.png/ });
    const closes = within(dialog).getAllByRole("button", { name: "Close" });
    await userEvent.click(closes[closes.length - 1] as HTMLElement);

    await waitFor(() => expect(where()).toBe(`/files/${FOLDER}`));
  });

  it("follows the preview onto the row a step lands on", async () => {
    await openTheFile();
    await waitFor(() => expect(where()).toContain(`preview=${FILE}`));

    // Arrow to the next previewable row: the address is of what is on screen.
    await userEvent.keyboard("{ArrowRight}");
    await waitFor(() => expect(where()).toContain(`preview=${SIBLING}`));

    // And on to a row of another kind: the kind in the address is the row's, not
    // the one the sheet was opened on.
    await userEvent.keyboard("{ArrowRight}");
    await waitFor(() => expect(where()).toContain(`preview=${NOTE}`));
    expect(where()).toContain("kind=markdown");
  });
});

describe("what a preview offers, whatever it is showing", () => {
  it("offers the same action row for a file nothing can draw", async () => {
    stubNetwork();
    mount(`/files/${FOLDER}`);
    await screen.findByText("salaries.csv");
    await userEvent.dblClick(screen.getByText("salaries.csv"));

    const dialog = await screen.findByRole("dialog", { name: /salaries\.csv/ });
    const labels = within(dialog)
      .getAllByRole("button")
      .map((button) => button.textContent?.trim());
    // A row that no renderer draws is still bytes a person can take away or
    // open whole: the action row does not thin out with the kind.
    for (const action of ["Download", "Copy link", "Open in new tab", "Close"]) {
      expect(labels, `${action} is missing`).toContain(action);
    }
  });
});

describe("a link to a file shared without its folder", () => {
  it("shows the file alone, with nothing of the folder around it", async () => {
    const net = stubNetwork({ [FOLDER]: 404 });
    mount(`/files/${FILE}`);

    expect(await screen.findByText(SOLO_LINE)).toBeInTheDocument();
    expect(await screen.findByRole("dialog", { name: /q3-report\.png/ })).toBeInTheDocument();
    // No sibling: not the row, and not the request that would have fetched it.
    expect(screen.queryByText("salaries.csv")).toBeNull();
    expect(net.urls.some((url) => url.includes(`${FOLDER}/children`))).toBe(false);
    // No ancestor names anywhere on the page, from the path or from the trail.
    expect(screen.queryByText(/home\/dana/)).toBeNull();
    // Nothing to create with: this is not a folder the reader may write into.
    expect(screen.queryByRole("button", { name: "New folder" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Upload files" })).toBeNull();
  });

  it("draws one trail segment: the file, addressing nothing above it", async () => {
    stubNetwork({ [FOLDER]: 403 });
    mount(`/files/${FILE}`);

    await screen.findByText(SOLO_LINE);
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(trail).getAllByRole("listitem")).toHaveLength(1);
    expect(within(trail).getByText("q3-report.png")).toBeInTheDocument();
  });

  it("offers no way into the folder from the preview", async () => {
    stubNetwork({ [FOLDER]: 403 });
    mount(`/files/${FILE}`);

    const dialog = await screen.findByRole("dialog", { name: /q3-report\.png/ });
    expect(within(dialog).queryByRole("button", { name: "Open in Files" })).toBeNull();
  });

  it("leaves the reader on the file when the preview is closed", async () => {
    stubNetwork({ [FOLDER]: 403 });
    mount(`/files/${FILE}`);

    await screen.findByText(SOLO_LINE);
    const dialog = await screen.findByRole("dialog", { name: /q3-report\.png/ });
    const closes = within(dialog).getAllByRole("button", { name: "Close" });
    await userEvent.click(closes[closes.length - 1] as HTMLElement);

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: /q3-report\.png/ })).toBeNull(),
    );
    // Walking to the folder is the one move this reader cannot make: it is the
    // read that was refused. Sending them there — and REPLACING the URL that
    // reaches the file while doing it — turns a shared link into a dead end.
    expect(where()).toBe(`/files/${FILE}`);
    expect(screen.getByText(SOLO_LINE)).toBeInTheDocument();
    expect(screen.queryByText(NOT_HERE)).toBeNull();
  });

  it("sends the reader to the feed of what IS shared with them", async () => {
    stubNetwork({ [FOLDER]: 404 });
    mount(`/files/${FILE}`);

    await screen.findByText(SOLO_LINE);
    await userEvent.click(screen.getByRole("link", { name: "Shared with me" }));

    await waitFor(() => expect(where()).toBe("/files?place=sharedWithMe"));
  });
});

describe("a link to something the reader may not have", () => {
  it.each([
    ["gone", 404],
    ["not theirs", 403],
  ])("says so when the node is %s", async (_label, status) => {
    stubNetwork({ [FILE]: status });
    mount(`/files/${FILE}`);

    expect(await screen.findByText(NOT_HERE)).toBeInTheDocument();
  });

  it("renders the SAME card for a missing id and an unreadable one", async () => {
    // Told apart, the page would answer "does this exist?" for a guessed id.
    stubNetwork({ [FILE]: 404 });
    mount(`/files/${FILE}`);
    await screen.findByText(NOT_HERE);
    const gone = screen.getByRole("region", { name: "Files" }).innerHTML;
    cleanup();
    vi.unstubAllGlobals();

    stubViewport();
    stubNetwork({ [FILE]: 403 });
    mount(`/files/${FILE}`);
    await screen.findByText(NOT_HERE);
    expect(screen.getByRole("region", { name: "Files" }).innerHTML).toBe(gone);
  });

  it("asks once, and does not go looking for the children of an id that is gone", async () => {
    const net = stubNetwork({ [FILE]: 404 });
    mount(`/files/${FILE}`);

    await screen.findByText(NOT_HERE);
    // Give a retry ladder every chance to fire before counting.
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(net.about(FILE)).toBe(1);
  });
});

describe("a row the preview can draw", () => {
  it("opens in place, never in a tab of its own", async () => {
    stubNetwork();
    const open = vi.fn();
    vi.stubGlobal("open", open);
    mount(`/files/${FOLDER}`);

    const row = await screen.findByText("salaries.csv");
    await userEvent.dblClick(row);

    expect(await screen.findByRole("dialog", { name: /salaries\.csv/ })).toBeInTheDocument();
    expect(open).not.toHaveBeenCalled();
  });
});

describe("a document previewed over the folder it lives in", () => {
  it("draws the picture stored beside it", async () => {
    stubNetwork();
    mount(`/files/${NOTE}`);

    await screen.findByRole("dialog", { name: /notes\.md/ });
    // The reference is a path, not a URL: only a resolver over the document's
    // own folder can turn it into bytes, and what reaches the page is the
    // object URL — never the grant that bought them.
    await waitFor(() => {
      const picture = screen.getByRole("img", { name: "the plot" });
      expect(picture.tagName).toBe("IMG");
      expect(picture.getAttribute("src")).toBe("blob:one");
    });
  });

  it("names the file a reference points at before it is clicked", async () => {
    stubNetwork();
    mount(`/files/${NOTE}`);

    const reference = await screen.findByRole("button", { name: "the numbers" });
    await waitFor(() =>
      expect(reference.getAttribute("title")).toBe("salaries.csv (in the chat's folder)"),
    );
  });

  it("selects the row a clicked reference names and moves the preview onto it", async () => {
    stubNetwork();
    mount(`/files/${NOTE}`);

    const reference = await screen.findByRole("button", { name: "the numbers" });
    await waitFor(() => expect(reference.getAttribute("title")).toContain("salaries.csv"));
    await userEvent.click(reference);

    // The answer to "which file is that?" is the file, in its place: the sheet
    // moves onto it and the row under it is the one selected.
    expect(await screen.findByRole("dialog", { name: /salaries\.csv/ })).toBeInTheDocument();
    await waitFor(() => {
      const row = document.querySelector(`[data-row-id="${SIBLING}"]`);
      expect(row?.getAttribute("aria-selected")).toBe("true");
    });
  });

  it("lets go of the bytes it bought when the sheet is closed", async () => {
    stubNetwork();
    mount(`/files/${NOTE}`);

    const dialog = await screen.findByRole("dialog", { name: /notes\.md/ });
    await waitFor(() =>
      expect(screen.getByRole("img", { name: "the plot" }).getAttribute("src")).toBe("blob:one"),
    );
    const closes = within(dialog).getAllByRole("button", { name: "Close" });
    await userEvent.click(closes[closes.length - 1] as HTMLElement);

    // An object URL nobody released is a page holding a file's bytes for as
    // long as the tab lives.
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:one"));
  });

  it("says a reference it cannot find is not a door", async () => {
    stubNetwork();
    mount(`/files/${NOTE}`);

    await screen.findByRole("dialog", { name: /notes\.md/ });
    // `gone.csv` is not in the folder, so the label is text with a reason
    // beside it rather than a control that would do nothing.
    await waitFor(() => expect(screen.getByText("the old one").tagName).toBe("SPAN"));
    expect(screen.getByText("not in the chat")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "the old one" })).toBeNull();
  });

  it("asks a folder the reader may not list for nothing at all", async () => {
    const net = stubNetwork({ [FOLDER]: 404 });
    mount(`/files/${NOTE}`);

    await screen.findByRole("dialog", { name: /notes\.md/ });
    await screen.findByText(SOLO_LINE);
    // The document's own bytes are a read of their own: the sheet and the solo
    // card are both on screen well before the markdown inside them is, so the
    // reference itself is what this waits for. Counting the folder's requests
    // before the references exist would count a moment at which nothing could
    // have asked for anything.
    const picture = await screen.findByRole("img", { name: "the plot" });
    // `missing` on the frame the reference first draws is the whole answer: a
    // resolver over the folder would have started `resolving` and bought bytes,
    // and a walk for those bytes puts the folder's listing on the wire before
    // this element is ever handed back.
    expect(picture.closest("figure")?.getAttribute("data-state")).toBe("missing");
    expect(picture.tagName).toBe("SPAN");
    // A document shared without its folder must not turn its own references
    // into a probe of that folder's names.
    expect(net.urls.some((url) => url.includes(`${FOLDER}/children`))).toBe(false);
  });
});
