import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FILES_NARROW_QUERY, FilesScreen } from "@/pages/workspace/files/FilesPage";
import { MOVE_MIME, type DropEntry } from "@/pages/workspace/files/dropHandlers";

// Drag and drop through the assembled Files page over a scripted namespace.
//
// Two drags share the wire. A row picked up in the listing is a MOVE: it lands
// on a folder row, on a segment of the trail, or nowhere (the folder on screen,
// which is where it already is). A drag from the desktop is an UPLOAD into the
// folder under the pointer, the listing itself standing for the folder on screen.
// Every refusal here is decided BEFORE a write is sent, which is what the
// request logs below pin.

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
      can_lease: false,
      can_lease_request: false,
      can_lease_force: false,
    },
    ...over,
  } as Item;
}

// A personal home: a folder under the `home` root container, never the container itself
// (the drive's three root containers are signposts that take no files).
const HOME = item({ id: "nd_home", kind: "folder", name: "dana", nameDisplay: "dana", path: "/home/dana", parentId: "nd_homes", etag: "et_home" });
const DEST = item({ id: "nd_dest", kind: "folder", name: "dest", nameDisplay: "dest", path: "/home/dest", etag: "et_dest" });
const LOCKED = item({
  id: "nd_locked",
  kind: "folder",
  name: "locked",
  nameDisplay: "locked",
  path: "/home/locked",
  etag: "et_locked",
  capabilities: {
    can_read: true,
    can_write: false,
    can_share: false,
    can_delete: false,
    can_purge: false,
    can_rename: false,
    can_download: true,
    can_lease: false,
    can_lease_request: false,
    can_lease_force: false,
  },
});
// A folder whose read carries no `can_write` at all: an older server, or a field
// dropped on the way. A capability the server did not grant is not granted.
const BARE = item({
  id: "nd_bare",
  kind: "folder",
  name: "bare",
  nameDisplay: "bare",
  path: "/home/bare",
  etag: "et_bare",
  capabilities: { can_read: true, can_download: true },
} as unknown as Partial<Item>);
const MOVER = item({ id: "nd_mover", kind: "folder", name: "mover", nameDisplay: "mover", path: "/home/mover", etag: "et_mover" });
// Listed beside its own ancestor so a drop "into a folder inside it" is reachable
// from one listing; the path is what the refusal reads.
const INNER = item({ id: "nd_inner", kind: "folder", name: "inner", nameDisplay: "inner", path: "/home/mover/inner", etag: "et_inner" });
const REPORT = item({ id: "nd_report", name: "report.csv", nameDisplay: "report.csv", etag: "et_report" });
// A folder a machine is holding for a chat and still accepting writes into: the
// server admits an upload under it, and the Files page refuses one anyway,
// because here a leased folder is read-only for the whole lease.
const INBOX = item({
  id: "nd_inbox",
  kind: "folder",
  name: "inbox",
  nameDisplay: "inbox",
  path: "/home/inbox",
  etag: "et_inbox",
  lease: {
    holder: "Dana",
    machine: "gpu-1",
    purpose: "chat",
    live: true,
    inbound: true,
    pending: 0,
    expires_at: "2100-01-01T00:00:00Z",
  },
} as unknown as Partial<Item>);
// The same shape after the machine is gone: the row outlives the holder until
// the reaper runs, and its expiry is the only thing that says so.
const LAPSED = item({
  id: "nd_lapsed",
  kind: "folder",
  name: "lapsed",
  nameDisplay: "lapsed",
  path: "/home/lapsed",
  etag: "et_lapsed",
  lease: {
    holder: "Dana",
    machine: "gpu-1",
    purpose: "chat",
    live: true,
    inbound: true,
    pending: 0,
    expires_at: "2000-01-01T00:00:00Z",
  },
} as unknown as Partial<Item>);
const ANCHOR = item({ id: "nd_anchor", name: "anchor.csv", nameDisplay: "anchor.csv", path: "/home/dest/anchor.csv", parentId: "nd_dest", etag: "et_anchor" });

interface Seen {
  method: string;
  url: string;
  path: string;
  ifMatch: string | undefined;
  body: Record<string, unknown> | undefined;
}

interface Script {
  seen: Seen[];
  /** The writes, in order: moves, tree creates, upload session opens. */
  writes: () => Seen[];
  moves: () => Seen[];
  opens: () => Seen[];
  trees: () => Seen[];
  /** Requests handed to the stub that have not answered yet. */
  inflight: () => number;
  /** Answer the next move with this status instead of 200. */
  moveStatus: number;
}

/** The script the case now running installed, so what it left in flight can be
 *  drained against its own log rather than the next case's. */
let live: Script | null = null;

function stubApi(): Script {
  const parents = new Map<string, string>([
    ["nd_home", "nd_homes"],
    ["nd_dest", "nd_home"],
    ["nd_locked", "nd_home"],
    ["nd_mover", "nd_home"],
    ["nd_inner", "nd_home"],
    ["nd_report", "nd_home"],
    ["nd_anchor", "nd_dest"],
    ["nd_inbox", "nd_home"],
    ["nd_lapsed", "nd_home"],
    ["nd_bare", "nd_home"],
  ]);
  const byId = new Map<string, Item>(
    [HOME, DEST, LOCKED, MOVER, INNER, REPORT, ANCHOR, INBOX, LAPSED, BARE].map((node) => [
      node.id,
      node,
    ]),
  );
  let sessions = 0;
  let open = 0;
  const seen: Seen[] = [];
  const script: Script = {
    seen,
    writes: () => seen.filter((r) => r.method === "PATCH" || r.method === "POST"),
    moves: () => seen.filter((r) => r.method === "PATCH" && r.body?.parentId !== undefined),
    opens: () => seen.filter((r) => r.method === "POST" && r.path.endsWith("/api/v1/files/uploads")),
    trees: () => seen.filter((r) => r.method === "POST" && r.path.endsWith("/tree")),
    inflight: () => open,
    moveStatus: 200,
  };
  const node = (id: string): Item => item({ ...byId.get(id), parentId: parents.get(id) ?? null });

  const answerFor = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const request = input instanceof Request ? input : new Request(String(input), init);
    const url = request.url;
    const path = new URL(url).pathname;
    const method = request.method.toUpperCase();
    let body: Record<string, unknown> | undefined;
    if (method === "PATCH" || method === "POST") {
      const raw = await request.clone().text();
      try {
        body = raw ? (JSON.parse(raw) as Record<string, unknown>) : undefined;
      } catch {
        body = undefined;
      }
    }
    seen.push({ method, url, path, ifMatch: request.headers.get("if-match") ?? undefined, body });
    const answer = (payload: unknown, status = 200) =>
      new Response(JSON.stringify(payload), {
        status,
        headers: { "content-type": "application/json" },
      });

    if (/\/files\/drives\/?(\?|$)/.test(url)) {
      return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", homeId: "nd_home", quotaBytes: 0 });
    }
    // The upload session, as the real client walks it.
    if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
      sessions += 1;
      return answer({ uploadId: `sess-${sessions}`, partSize: 1024, partsTotal: 1, expiresAt: "" });
    }
    const session = /\/uploads\/(sess-\d+)(\/|$)/.exec(path)?.[1];
    if (session && method === "GET") {
      return answer({ uploadId: session, state: "open", offset: 0, length: 1024, complete: false, partsDone: 0, partsTotal: 1, acceptedParts: [] });
    }
    if (session && method === "PUT") return answer({ ok: true });
    if (session && path.endsWith("/complete")) {
      return answer({ item: { id: `up-${session}`, etag: "et_up" }, unchanged: false });
    }
    if (method === "POST" && path.endsWith("/tree")) {
      const paths = (body?.paths as string[] | undefined) ?? [];
      return answer(paths.map((p, index) => ({ id: `dir-${index}`, name: p.split("/").pop(), path: p })));
    }
    if (method === "PATCH") {
      const id = /\/items\/([^/?]+)/.exec(url)?.[1] ?? "";
      if (body?.parentId === undefined) return answer({ id, etag: "et_patched" });
      if (script.moveStatus !== 200) {
        return answer({ code: "files.precondition_failed", message: "version moved on" }, script.moveStatus);
      }
      parents.set(id, String(body.parentId));
      return answer(node(id));
    }
    if (url.includes("/leases")) return answer([]);
    if (url.includes("/permissions")) return answer({ value: [], nextMarker: null });
    if (url.includes("/versions") || url.includes("/activity")) return answer({ value: [], nextMarker: null });
    if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
    const children = /\/items\/([^/]+)\/children/.exec(url);
    if (children) {
      const parent = children[1];
      const value = [...parents.entries()].filter(([, held]) => held === parent).map(([id]) => node(id));
      return answer({ value, nextMarker: null });
    }
    const one = /\/items\/([^/?]+)(\?|$)/.exec(url);
    if (one) return answer(node(one[1]!));
    return answer({});
  };

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      open += 1;
      try {
        return await answerFor(input, init);
      } finally {
        open -= 1;
      }
    }),
  );
  live = script;
  return script;
}

/** Let everything a case started finish before the next one installs its stub.
 *
 *  An upload is four legs long — open, part, complete, and the mtime patch that
 *  ends it — and a case that has seen the session open still has three of them
 *  to come. `fetch` is a global: a leg that lands after the next case has
 *  stubbed it is recorded as that case's own first request, which is how a
 *  refusal asserting "with no request" was once handed two of someone else's. */
async function drain(): Promise<void> {
  const script = live;
  live = null;
  if (!script) return;
  for (let quiet = 0; quiet < 4; quiet += 1) {
    const before = script.seen.length;
    await new Promise((resolve) => setTimeout(resolve, 25));
    if (script.seen.length !== before || script.inflight() > 0) quiet = -1;
  }
}

function stubViewport(): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        media: query,
        matches: query === FILES_NARROW_QUERY ? false : false,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
}

function mount(at = "/files/nd_home") {
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path="/files" element={<FilesScreen platform="mac" />} />
          <Route path="/files/:nodeId" element={<FilesScreen platform="mac" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const grid = () => screen.getByRole("treegrid");
const rowNamed = (name: string) => within(grid()).getByRole("row", { name: new RegExp(`^${name}`) });
const noRowNamed = (name: string) => within(grid()).queryByRole("row", { name: new RegExp(`^${name}`) });
const listing = () => document.querySelector<HTMLElement>(".alk-files__drop")!;
const crumb = (id: string) => document.querySelector<HTMLElement>(`.alk-files-crumbs__item[data-node-id="${id}"]`)!;

/** What jsdom has no `DataTransfer` for: the fields the page reads. */
interface FakeTransfer {
  types: string[];
  items: { kind: string; webkitGetAsEntry: () => DropEntry | null; getAsFile: () => File | null }[];
  effectAllowed: string;
  dropEffect: string;
  setData(type: string, value: string): void;
  getData(type: string): string;
}

function transfer(): FakeTransfer {
  const data = new Map<string, string>();
  return {
    types: [],
    items: [],
    effectAllowed: "",
    dropEffect: "",
    setData(type, value) {
      data.set(type, value);
      if (!this.types.includes(type)) this.types.push(type);
    },
    getData: (type) => data.get(type) ?? "",
  };
}

function fileEntry(name: string, body = "x"): DropEntry {
  const file = new File([body], name, { lastModified: Date.UTC(2020, 0, 1) });
  return { isFile: true, isDirectory: false, name, file: (onSuccess) => onSuccess(file) };
}

function dirEntry(name: string, children: DropEntry[]): DropEntry {
  return {
    isFile: false,
    isDirectory: true,
    name,
    createReader: () => {
      let drained = false;
      return {
        readEntries: (onSuccess) => {
          const batch = drained ? [] : children;
          drained = true;
          onSuccess(batch);
        },
      };
    },
  };
}

/** A drag from the desktop carrying these entries. */
function desktopDrag(entries: DropEntry[]): FakeTransfer {
  const dt = transfer();
  dt.types.push("Files");
  dt.items = entries.map((entry) => ({ kind: "file", webkitGetAsEntry: () => entry, getAsFile: () => null }));
  return dt;
}

/** Pick a row up the way the browser reports it. */
async function pickUp(name: string): Promise<FakeTransfer> {
  const row = await waitFor(() => rowNamed(name));
  const dt = transfer();
  fireEvent.dragStart(row, { dataTransfer: dt });
  expect(dt.types).toContain(MOVE_MIME);
  return dt;
}

function hover(target: HTMLElement, dt: FakeTransfer): void {
  fireEvent.dragEnter(target, { dataTransfer: dt });
  fireEvent.dragOver(target, { dataTransfer: dt });
}

function dropOn(target: HTMLElement, dt: FakeTransfer): void {
  fireEvent.drop(target, { dataTransfer: dt });
  fireEvent.dragEnd(target, { dataTransfer: dt });
}

async function settled(): Promise<void> {
  // Enough turns for the target to resolve and a refused verdict to render.
  await new Promise((resolve) => setTimeout(resolve, 20));
}

beforeEach(() => stubViewport());
afterEach(async () => {
  await drain();
  vi.unstubAllGlobals();
});

describe("a row dragged onto a folder", () => {
  it("highlights the folder under the pointer and moves the row there on drop", async () => {
    const script = stubApi();
    mount();
    const dt = await pickUp("mover");
    const dest = rowNamed("dest");

    hover(dest, dt);
    expect(dest).toHaveAttribute("data-drop-active", "true");
    // Leaving for the row beside it clears the ring — one target at a time.
    fireEvent.dragLeave(dest, { dataTransfer: dt, relatedTarget: rowNamed("locked") });
    expect(dest).not.toHaveAttribute("data-drop-active");

    hover(dest, dt);
    dropOn(dest, dt);

    await waitFor(() => expect(script.moves()).toHaveLength(1));
    expect(script.moves()[0]).toMatchObject({ ifMatch: "et_mover", body: { parentId: "nd_dest" } });
    expect(script.moves()[0]!.url).toContain("/items/nd_mover");
    await waitFor(() => expect(noRowNamed("mover")).toBeNull());
  });

  it("drags the whole selection when the picked-up row is part of it", async () => {
    const script = stubApi();
    mount();
    const report = await waitFor(() => rowNamed("report.csv"));
    fireEvent.click(within(report).getAllByRole("gridcell")[0]!);
    fireEvent.click(within(rowNamed("mover")).getAllByRole("gridcell")[0]!, { metaKey: true });

    const dt = await pickUp("mover");
    dropOn(rowNamed("dest"), dt);

    await waitFor(() => expect(script.moves()).toHaveLength(2));
    expect(script.moves().map((m) => /\/items\/([^/?]+)/.exec(m.url)?.[1]).sort()).toEqual([
      "nd_mover",
      "nd_report",
    ]);
  });

  it("refuses a folder dropped into a folder inside it, with no request", async () => {
    const script = stubApi();
    mount();
    const dt = await pickUp("mover");
    dropOn(rowNamed("inner"), dt);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("mover cannot be moved into a folder inside it.");
    await settled();
    expect(script.writes()).toHaveLength(0);
    expect(rowNamed("mover")).toBeInTheDocument();
  });

  it("does not offer the dragged folder itself as a target", async () => {
    stubApi();
    mount();
    const dt = await pickUp("mover");
    const self = rowNamed("mover");
    hover(self, dt);
    expect(self).not.toHaveAttribute("data-drop-active");
    hover(rowNamed("dest"), dt);
    expect(rowNamed("dest")).toHaveAttribute("data-drop-active", "true");
  });

  it("does nothing when dropped onto the folder it is already in", async () => {
    const script = stubApi();
    mount();
    const dt = await pickUp("report.csv");
    // The listing is the folder on screen; the last crumb is the same folder.
    dropOn(listing(), dt);
    dropOn(crumb("nd_home"), dt);
    await settled();

    expect(script.writes()).toHaveLength(0);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(rowNamed("report.csv")).toBeInTheDocument();
  });

  it("refuses a folder the person cannot write to, before any request", async () => {
    const script = stubApi();
    mount();
    const dt = await pickUp("report.csv");
    dropOn(rowNamed("locked"), dt);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("You do not have permission to add to locked.");
    expect(script.writes()).toHaveLength(0);
  });

  it("puts the row back and says why when the server refuses the move", async () => {
    const script = stubApi();
    script.moveStatus = 412;
    mount();
    const dt = await pickUp("report.csv");
    dropOn(rowNamed("dest"), dt);

    await waitFor(() => expect(script.moves()).toHaveLength(1));
    // The row left at once (the move is optimistic) and is back once refused.
    await screen.findByRole("alert");
    await waitFor(() => expect(rowNamed("report.csv")).toBeInTheDocument());
  });

  it("moves onto an ancestor on the trail", async () => {
    const script = stubApi();
    mount();
    const dest = await waitFor(() => rowNamed("dest"));
    fireEvent.doubleClick(within(dest).getAllByRole("gridcell")[0]!);
    await waitFor(() => rowNamed("anchor.csv"));

    const dt = await pickUp("anchor.csv");
    const home = crumb("nd_home");
    hover(home, dt);
    expect(home).toHaveAttribute("data-drop-active", "true");
    dropOn(home, dt);

    await waitFor(() => expect(script.moves()).toHaveLength(1));
    expect(script.moves()[0]).toMatchObject({ ifMatch: "et_anchor", body: { parentId: "nd_home" } });
  });
});

describe("a drag from the desktop", () => {
  it("shows the listing as the target and uploads into the folder on screen", async () => {
    const script = stubApi();
    mount();
    await waitFor(() => rowNamed("dest"));
    const dt = desktopDrag([fileEntry("notes.txt")]);

    hover(listing(), dt);
    expect(listing()).toHaveAttribute("data-drop-active", "true");
    expect(listing()).toHaveTextContent("Drop to upload into dana");
    dropOn(listing(), dt);

    await waitFor(() => expect(script.opens()).toHaveLength(1));
    expect(script.opens()[0]!.body).toMatchObject({ name: "notes.txt", parentId: "nd_home" });
    expect(listing()).not.toHaveAttribute("data-drop-active");
  });

  it("lands inside the folder row under the pointer, and only there", async () => {
    const script = stubApi();
    mount();
    const dest = await waitFor(() => rowNamed("dest"));
    const dt = desktopDrag([fileEntry("notes.txt")]);

    hover(listing(), dt);
    hover(dest, dt);
    expect(dest).toHaveAttribute("data-drop-active", "true");
    dropOn(dest, dt);

    await waitFor(() => expect(script.opens()).toHaveLength(1));
    // One session, into the row — the listing behind it did not also take the drop.
    await settled();
    expect(script.opens()).toHaveLength(1);
    expect(script.opens()[0]!.body).toMatchObject({ parentId: "nd_dest" });
  });

  it("lands in an ancestor on the trail", async () => {
    const script = stubApi();
    mount();
    const dest = await waitFor(() => rowNamed("dest"));
    fireEvent.doubleClick(within(dest).getAllByRole("gridcell")[0]!);
    await waitFor(() => rowNamed("anchor.csv"));

    const dt = desktopDrag([fileEntry("notes.txt")]);
    const home = crumb("nd_home");
    hover(home, dt);
    expect(home).toHaveAttribute("data-drop-active", "true");
    dropOn(home, dt);

    await waitFor(() => expect(script.opens()).toHaveLength(1));
    expect(script.opens()[0]!.body).toMatchObject({ parentId: "nd_home" });
  });

  it("creates a dropped folder's tree first — empty folders included — then uploads into it", async () => {
    const script = stubApi();
    mount();
    const dest = await waitFor(() => rowNamed("dest"));
    const dt = desktopDrag([dirEntry("photos", [dirEntry("empty", []), fileEntry("a.csv")])]);
    dropOn(dest, dt);

    await waitFor(() => expect(script.opens()).toHaveLength(1));
    const writes = script.writes();
    const tree = writes.findIndex((w) => w.path.endsWith("/tree"));
    const open = writes.findIndex((w) => w.path.endsWith("/api/v1/files/uploads"));
    expect(tree).toBeGreaterThanOrEqual(0);
    expect(tree).toBeLessThan(open);
    expect(writes[tree]!.path).toContain("/items/nd_dest/tree");
    expect(writes[tree]!.body).toEqual({ paths: ["photos", "photos/empty"] });
    // Into the folder the tree call created for `photos`, not the drop target.
    expect(writes[open]!.body).toMatchObject({ name: "a.csv", parentId: "dir-0" });
  });

  it("uploads into a folder a machine is holding when its lease admits writes from the web", async () => {
    const script = stubApi();
    mount();
    const inbox = await waitFor(() => rowNamed("inbox"));
    dropOn(inbox, desktopDrag([fileEntry("from-laptop.csv")]));

    // The server admits an inbound write under an awake chat's folder and hands
    // it to the machine, as it does from the chat's own Files tab: the page
    // offers the same write rather than calling the folder read-only.
    await waitFor(() => expect(script.opens()).toHaveLength(1));
    expect(script.opens()[0]!.body).toMatchObject({
      name: "from-laptop.csv",
      parentId: "nd_inbox",
    });
    expect(screen.queryByText(/is leased and read-only/)).toBeNull();
  });

  it("uploads into a folder whose lease has run out, because nobody holds it any more", async () => {
    const script = stubApi();
    mount();
    const lapsed = await waitFor(() => rowNamed("lapsed"));
    dropOn(lapsed, desktopDrag([fileEntry("from-laptop.csv")]));

    await waitFor(() => expect(script.opens()).toHaveLength(1));
    expect(script.opens()[0]!.body).toMatchObject({
      name: "from-laptop.csv",
      parentId: "nd_lapsed",
    });
  });

  it("is refused on a folder the person cannot write to, with no request", async () => {
    const script = stubApi();
    mount();
    const locked = await waitFor(() => rowNamed("locked"));
    dropOn(locked, desktopDrag([fileEntry("notes.txt")]));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("You do not have permission to add to locked.");
    await settled();
    expect(script.writes()).toHaveLength(0);
  });

  it("is refused on a folder whose read names no can_write, with no request", async () => {
    const script = stubApi();
    mount("/files/nd_bare");
    await waitFor(() => expect(crumb("nd_bare")).toBeTruthy());
    const dt = desktopDrag([fileEntry("a.csv")]);
    hover(listing(), dt);
    dropOn(listing(), dt);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("You do not have permission to add to bare.");
    await settled();
    expect(script.writes()).toEqual([]);
  });

  it("ignores dragged text: nothing is allowed to land and nothing is painted", async () => {
    const script = stubApi();
    mount();
    await waitFor(() => rowNamed("dest"));
    const dt = transfer();
    dt.types.push("text/plain");

    hover(listing(), dt);
    expect(listing()).not.toHaveAttribute("data-drop-active");
    dropOn(listing(), dt);
    await settled();
    expect(script.writes()).toHaveLength(0);
  });
});

describe("a folder whose read names no can_write", () => {
  it("offers nothing that adds to it from the listing's menu", async () => {
    stubApi();
    mount("/files/nd_bare");
    await waitFor(() => expect(crumb("nd_bare")).toBeTruthy());
    fireEvent.contextMenu(listing(), { clientX: 10, clientY: 10 });
    const menu = await screen.findByRole("menu");
    for (const id of ["new-folder", "upload-files"]) {
      expect(menu.querySelector(`[data-item="${id}"]`)).toHaveAttribute("aria-disabled", "true");
    }
    expect(within(menu).getAllByText("You cannot add to this folder.").length).toBeGreaterThanOrEqual(2);
  });
});
