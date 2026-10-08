// @vitest-environment jsdom
//
// A rendered document reaching the files stored beside it.
//
// A markdown report names `charts/q3.png`; a preview of that report has to turn
// the name into something an `<img>` can load, and the only bytes it may reach
// are the ones under the folder the document itself lives in. So the walk starts
// at that folder and steps child by exact name — a path that tries to climb out,
// start at the root, or name a folder as its leaf resolves to nothing, and
// nothing is minted for it. What comes back is an object URL: the grant that
// bought the bytes is spent inside this module and never handed to the page.

import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CHILDREN_PAGE, toChildrenParams, type ContentGrant, type Item } from "@/api/files";
import { keys } from "@/api/keys";
import { folderResolver } from "@/pages/workspace/files/preview/folderResolver";

const DRIVE = "d1";
const ROOT = "folder-root";
const GRANT_URL = "http://files.localhost:8000/c/bm9uY2U.Y2xhaW0.c2ln";

function row(over: Partial<Item>): Item {
  return {
    id: "x",
    driveId: DRIVE,
    kind: "file",
    name: "x",
    nameDisplay: "x",
    etag: "e1",
    file: { mime_type: "image/png", size: 12, content_hash: "sha256-test", scan_state: "clean" },
    ...over,
  } as Item;
}

const REPORT = row({ id: "report", name: "report.md", file: undefined });
const CHARTS = row({ id: "charts", name: "charts", kind: "folder", file: undefined });
const Q3 = row({ id: "q3", name: "q3.png" });
const SECRET = row({ id: "secret", name: "secret.png" });

function seed(qc: QueryClient, parentId: string, rows: Item[]): void {
  qc.setQueryData(keys.files.children(DRIVE, parentId, toChildrenParams(), CHILDREN_PAGE), {
    pages: [{ value: rows, nextMarker: null }],
    pageParams: [undefined],
  });
}

function seededClient(): QueryClient {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  seed(qc, ROOT, [REPORT, CHARTS, SECRET]);
  seed(qc, "charts", [Q3]);
  return qc;
}

function mintSpy() {
  const calls: unknown[] = [];
  const mint = vi.fn(async (vars: unknown): Promise<ContentGrant> => {
    calls.push(vars);
    return { url: GRANT_URL, expiresAt: "2026-09-16T00:05:00Z", kind: "file", etag: "e1" };
  });
  return { mint, calls };
}

const folder = row({ id: ROOT, name: "reports", kind: "folder", file: undefined });

describe("folderResolver — the walk", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("finds a file beside the document", async () => {
    const { mint } = mintSpy();
    const resolver = folderResolver(folder, mint, seededClient());

    await expect(resolver.locate("secret.png")).resolves.toEqual({
      nodeId: "secret",
      parentId: ROOT,
      name: "secret.png",
      path: "secret.png",
    });
  });

  it("walks into a subfolder", async () => {
    const { mint } = mintSpy();
    const resolver = folderResolver(folder, mint, seededClient());

    await expect(resolver.locate("charts/q3.png")).resolves.toEqual({
      nodeId: "q3",
      parentId: "charts",
      name: "q3.png",
      path: "charts/q3.png",
    });
  });

  it.each([
    ["..", "a bare climb"],
    ["../secret.png", "a climb out of the folder"],
    ["charts/../../secret.png", "a climb in the middle"],
    ["/secret.png", "an absolute path"],
    ["charts\\q3.png", "a backslash"],
    ["charts//q3.png", "an empty segment"],
    ["./secret.png", "a dot segment"],
    ["", "nothing at all"],
    ["charts/", "a trailing slash"],
  ])("refuses %s (%s) without minting", async (path) => {
    const { mint, calls } = mintSpy();
    const resolver = folderResolver(folder, mint, seededClient());

    await expect(resolver.locate(path)).resolves.toBeNull();
    await expect(resolver.resolveUrl(path)).resolves.toBeNull();
    expect(calls).toEqual([]);
  });

  it("refuses a path with a NUL in it", async () => {
    const { mint, calls } = mintSpy();
    const resolver = folderResolver(folder, mint, seededClient());

    await expect(resolver.locate("charts/q3\u0000.png")).resolves.toBeNull();
    expect(calls).toEqual([]);
  });

  it("answers nothing for a name that is not there, and for a folder named as the leaf", async () => {
    const { mint } = mintSpy();
    const resolver = folderResolver(folder, mint, seededClient());

    await expect(resolver.locate("nope.png")).resolves.toBeNull();
    await expect(resolver.locate("charts")).resolves.toBeNull();
    await expect(resolver.locate("secret.png/q3.png")).resolves.toBeNull();
  });

  it("reads the folder from the server when the browser has not listed it yet", async () => {
    const { mint } = mintSpy();
    const listing = vi.fn(async () =>
      jsonResponse({ value: [{ ...Q3, name: "late.png" }], nextMarker: null }),
    );
    vi.stubGlobal("fetch", listing);
    const resolver = folderResolver(folder, mint, new QueryClient());

    await expect(resolver.locate("late.png")).resolves.toMatchObject({ nodeId: "q3", name: "late.png" });
    expect(listing).toHaveBeenCalledTimes(1);
    const asked = (listing.mock.calls as unknown as [Request | string][])[0]![0];
    expect(typeof asked === "string" ? asked : asked.url).toContain(
      `/api/v1/files/drives/${DRIVE}/items/${ROOT}/children`,
    );
  });
});

describe("folderResolver — the bytes", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubObjectUrls() {
    let n = 0;
    const revoked: string[] = [];
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => `blob:made-${(n += 1)}`),
      revokeObjectURL: vi.fn((url: string) => revoked.push(url)),
    });
    return { revoked };
  }

  it("mints a file grant, spends it anonymously, and answers an object URL", async () => {
    stubObjectUrls();
    const { mint, calls } = mintSpy();
    const fetchMock = vi.fn(async () => bytesResponse("image/png"));
    vi.stubGlobal("fetch", fetchMock);
    const resolver = folderResolver(folder, mint, seededClient());

    const url = await resolver.resolveUrl("charts/q3.png");

    expect(url).toBe("blob:made-1");
    expect(calls).toEqual([{ driveId: DRIVE, itemId: "q3", kind: "file" }]);
    const [fetched, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(fetched).toBe(GRANT_URL);
    expect(init.credentials).toBe("omit");
    expect(init.mode).toBe("cors");
    expect(url).not.toContain("/c/");
  });

  it("buys the bytes once per path and lets go of them on request", async () => {
    const { revoked } = stubObjectUrls();
    const { mint, calls } = mintSpy();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => bytesResponse("image/png")),
    );
    const resolver = folderResolver(folder, mint, seededClient());

    const first = await resolver.resolveUrl("charts/q3.png");
    const second = await resolver.resolveUrl("charts/q3.png");

    expect(second).toBe(first);
    expect(calls).toHaveLength(1);

    resolver.revokeAll();
    expect(revoked).toEqual([first]);
    await expect(resolver.resolveUrl("charts/q3.png")).resolves.toBe("blob:made-2");
  });

  it("answers nothing when the bytes are refused", async () => {
    stubObjectUrls();
    const { mint } = mintSpy();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("", { status: 404 })),
    );
    const resolver = folderResolver(folder, mint, seededClient());

    await expect(resolver.resolveUrl("charts/q3.png")).resolves.toBeNull();
  });
});

describe("folderResolver — clicking a reference", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("hands the page the row the reference names, so the listing selects it and the preview moves onto it", async () => {
    const { mint } = mintSpy();
    const shown: Item[] = [];
    const resolver = folderResolver(folder, mint, seededClient(), (item) => shown.push(item));

    const ref = await resolver.locate("charts/q3.png");
    expect(ref).not.toBeNull();
    resolver.reveal?.(ref!);

    expect(shown).toEqual([Q3]);
  });

  it("shows nothing for a reference the walk never resolved", async () => {
    const { mint } = mintSpy();
    const shown: Item[] = [];
    const resolver = folderResolver(folder, mint, seededClient(), (item) => shown.push(item));

    // A reference the reader could not have seen rendered as a door: the page
    // must not be sent to a row this resolver never found.
    resolver.reveal?.({ nodeId: "secret", parentId: ROOT, name: "secret.png", path: "../secret.png" });

    expect(shown).toEqual([]);
  });

  it("shows nothing when the page kept no place to show it", async () => {
    const { mint } = mintSpy();
    const resolver = folderResolver(folder, mint, seededClient());

    const ref = await resolver.locate("secret.png");
    expect(() => resolver.reveal?.(ref!)).not.toThrow();
  });
});

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function bytesResponse(contentType: string): Response {
  return new Response(new Blob(["bytes"]), {
    status: 200,
    headers: { "content-type": contentType },
  });
}
