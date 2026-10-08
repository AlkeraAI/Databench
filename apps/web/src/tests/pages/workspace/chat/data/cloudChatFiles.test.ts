// The browser's chat-files port over Files. A message names a file relative
// to the chat's effective root; the port finds it under the chat's node —
// directly, or one folder down, wherever the server put what the chat was
// handed — and buys its bytes with a single-use grant it spends itself, so
// what reaches the transcript's <img> is `blob:`, never a URL anyone else
// could read the file with. An upload is renamed to the deterministic staged
// name, sent to the chat's node, and reported as landed only once it can be
// found back.

import { ChatFileNotReady } from "@alkera/ui";
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";

import { UploadClient, type UploadClientOptions } from "@/api/filesUpload";
import { STAGE_NAME_ATTEMPTS, cloudChatFiles, stagedFileName } from "@/pages/workspace/chat/data/chatFiles";

const CHAT = "11111111-1111-1111-1111-111111111111";
const ROOT = "root-node";
const DRIVE = "drive-1";
/** Where a grant's URL points: the content host, a different origin from the
 *  API, which is why the read is cross-origin and carries no cookie. */
const CONTENT = "http://files.localhost:8000";

interface Row {
  id: string;
  kind: "file" | "folder";
  name: string;
  driveId: string;
  etag: string;
}

function row(id: string, kind: Row["kind"], name: string, etag = "v1"): Row {
  return { id, kind, name, driveId: DRIVE, etag };
}

/** A fetch that serves the chat row, the drive, per-folder listings, the grant
 *  route and the content host — and records what each was asked for. */
function filesServer(
  tree: Record<string, Row[]>,
  chat: { files_node_id: string | null } = { files_node_id: ROOT },
  listing: (parentId: string) => unknown = (parentId) => childrenPage(tree[parentId] ?? []),
  grant: (itemId: string) => Response | null = () => null,
  working: Record<string, string> = {},
) {
  const listed: string[] = [];
  const created: Array<{ parentId: string; name: string; kind: string; conflictBehavior: string }> = [];
  const itemReads: string[] = [];
  const minted: Array<{ itemId: string; body: unknown; credentials?: string }> = [];
  const reads: Array<{ url: string; init: RequestInit }> = [];
  let issued = 0;
  const fetchImpl = vi.fn(async (input: string | URL | Request, init: RequestInit = {}) => {
    const href = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    if (href.startsWith(`${CONTENT}/c/`)) {
      reads.push({ url: href, init });
      return new Response("bytes", { status: 200, headers: { "content-type": "image/png" } });
    }
    const url = new URL(href);
    const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    if (url.pathname === `/api/v1/chats/${CHAT}`) return json({ id: CHAT, title: "t", ...chat });
    if (url.pathname === "/api/v1/files/drives") return json({ id: DRIVE });
    const grants = /\/api\/v1\/files\/drives\/[^/]+\/items\/([^/]+)\/content-grants$/.exec(url.pathname);
    if (grants) {
      const itemId = grants[1];
      minted.push({
        itemId,
        body: init.body ? (JSON.parse(String(init.body)) as unknown) : null,
        credentials: init.credentials,
      });
      const refusal = grant(itemId);
      if (refusal) return refusal;
      issued += 1;
      return json({
        url: `${CONTENT}/c/token-${issued}`,
        expiresAt: "2026-09-16T00:15:00Z",
        kind: "file",
        etag: rowById(tree, itemId)?.etag ?? "",
      });
    }
    const children = /\/api\/v1\/files\/drives\/[^/]+\/items\/([^/]+)\/children$/.exec(url.pathname);
    if (children && init.method === "POST") {
      // A folder made under a folder: refused on a name already there, the
      // way `conflictBehavior: "fail"` is.
      const made = JSON.parse(String(init.body)) as { name: string; kind: string; conflictBehavior: string };
      created.push({ parentId: children[1], ...made });
      const rows = (tree[children[1]] ??= []);
      if (rows.some((candidate) => candidate.name === made.name)) {
        return new Response(JSON.stringify({ code: "name_conflict" }), { status: 409 });
      }
      const folder = row(`made-${created.length}`, "folder", made.name);
      rows.push(folder);
      tree[folder.id] = [];
      return new Response(JSON.stringify(folder), { status: 201 });
    }
    const item = /\/api\/v1\/files\/drives\/[^/]+\/items\/([^/]+)$/.exec(url.pathname);
    if (item) {
      itemReads.push(item[1]);
      // The chat folder names its working directory the way Files does: under
      // the object facet's metadata.
      const target = working[item[1]];
      return json({ id: item[1], kind: "folder", object: target ? { metadata: { files_node_id: target } } : null });
    }
    if (children) {
      listed.push(children[1]);
      // The real `ChildrenPage` wire: the rows ride under `value`, beside the
      // keyset marker — never under `items`.
      return json(listing(children[1]));
    }
    return new Response("{}", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchImpl);
  return { listed, minted, reads, fetchImpl, created, itemReads };
}

function rowById(tree: Record<string, Row[]>, id: string): Row | undefined {
  for (const rows of Object.values(tree)) {
    const hit = rows.find((candidate) => candidate.id === id);
    if (hit) return hit;
  }
  return undefined;
}

function childrenPage(rows: Row[]): { value: Row[]; nextMarker: string | null } {
  return { value: rows, nextMarker: null };
}

/** jsdom has no object-URL store. The two functions are swapped on the real
 *  `URL` rather than the global being replaced, so `new URL(…)` keeps working
 *  inside the fetch stub. */
function stubObjectUrls() {
  const real = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  let made = 0;
  const revoked: string[] = [];
  URL.createObjectURL = () => `blob:made-${(made += 1)}`;
  URL.revokeObjectURL = (url: string) => void revoked.push(url);
  restoreObjectUrls = () => {
    URL.createObjectURL = real.create;
    URL.revokeObjectURL = real.revoke;
  };
  return { revoked };
}

let restoreObjectUrls: (() => void) | null = null;

afterEach(() => {
  vi.unstubAllGlobals();
  restoreObjectUrls?.();
  restoreObjectUrls = null;
});

describe("cloudChatFiles.resolveUrl", () => {
  it("finds a file directly under the chat node, buys its bytes with a file grant and answers an object URL", async () => {
    stubObjectUrls();
    const { listed, minted, reads } = filesServer({ [ROOT]: [row("n1", "file", "revenue.png")] });
    const port = cloudChatFiles();

    await expect(port.resolveUrl(CHAT, "revenue.png")).resolves.toBe("blob:made-1");

    expect(listed).toEqual([ROOT]);
    expect(minted).toEqual([
      { itemId: "n1", body: { kind: "file", disposition: "inline" }, credentials: "include" },
    ]);
    expect(reads.map((read) => read.url)).toEqual([`${CONTENT}/c/token-1`]);
  });

  it("spends the grant anonymously, cross-origin and uncached", async () => {
    stubObjectUrls();
    const { reads } = filesServer({ [ROOT]: [row("n1", "file", "revenue.png")] });

    await cloudChatFiles().resolveUrl(CHAT, "revenue.png");

    // The grant IS the authorization; sending the session cookie with it would
    // make the read succeed for reasons the grant does not cover.
    expect(reads[0].init).toMatchObject({ mode: "cors", credentials: "omit", cache: "no-store" });
  });

  it("never reads the cookie-authorized content route, and never hands its URL to the caller", async () => {
    stubObjectUrls();
    const { fetchImpl } = filesServer({ [ROOT]: [row("n1", "file", "revenue.png")] });

    const url = await cloudChatFiles().resolveUrl(CHAT, "revenue.png");

    const asked = fetchImpl.mock.calls.map(([input]) => String(input));
    expect(asked.filter((each) => each.includes("/content?") || each.endsWith("/content"))).toEqual([]);
    // What the transcript renders carries nothing anyone else could read with.
    expect(url).toMatch(/^blob:/);
    expect(url).not.toContain(CONTENT);
    expect(url).not.toContain("token-");
  });

  it("finds a file one folder down — where the server lands what the chat is handed — without naming the folder", async () => {
    stubObjectUrls();
    const { listed, minted } = filesServer({
      [ROOT]: [row("f-out", "folder", "outputs"), row("f-work", "folder", "scratch")],
      "f-work": [row("n2", "file", "paste-1-ab12.png")],
    });
    const port = cloudChatFiles();

    await expect(port.resolveUrl(CHAT, "paste-1-ab12.png")).resolves.toBe("blob:made-1");

    expect(minted.map((call) => call.itemId)).toEqual(["n2"]);
    // The chat node once, then its folders, in order; the cached root listing
    // is not re-read for the second look.
    expect(listed).toEqual([ROOT, "f-out", "f-work"]);
  });

  it("walks a nested path segment by segment", async () => {
    stubObjectUrls();
    const { minted } = filesServer({
      [ROOT]: [row("f-charts", "folder", "charts")],
      "f-charts": [row("n3", "file", "q3.png")],
    });

    await expect(cloudChatFiles().resolveUrl(CHAT, "charts/q3.png")).resolves.toBe("blob:made-1");
    expect(minted.map((call) => call.itemId)).toEqual(["n3"]);
  });

  it("lists a folder once per distinct path; a second image in the same folder reuses the listing", async () => {
    stubObjectUrls();
    const { listed } = filesServer({ [ROOT]: [row("a", "file", "a.png"), row("b", "file", "b.png")] });
    const port = cloudChatFiles();
    await port.resolveUrl(CHAT, "a.png");
    await port.resolveUrl(CHAT, "b.png");
    expect(listed).toEqual([ROOT]);
  });

  it("buys one version's bytes once: the same image asked for twice mints once and answers the same URL", async () => {
    stubObjectUrls();
    const { minted, reads } = filesServer({ [ROOT]: [row("n1", "file", "revenue.png", "v1")] });
    const port = cloudChatFiles();

    const first = await port.resolveUrl(CHAT, "revenue.png");
    const second = await port.resolveUrl(CHAT, "revenue.png");

    expect(second).toBe(first);
    expect(minted).toHaveLength(1);
    expect(reads).toHaveLength(1);
  });

  it("re-buys and releases when the file changed under it", async () => {
    const { revoked } = stubObjectUrls();
    const tree = { [ROOT]: [row("n1", "file", "chart.png", "v1")] };
    let clock = 0;
    const { minted } = filesServer(tree);
    const port = cloudChatFiles({ now: () => clock });

    const first = await port.resolveUrl(CHAT, "chart.png");
    // The agent rewrote it: a new version, so a new etag on the row.
    tree[ROOT] = [row("n1", "file", "chart.png", "v2")];
    clock += 60_000;
    const second = await port.resolveUrl(CHAT, "chart.png");

    expect(second).not.toBe(first);
    expect(minted).toHaveLength(2);
    // The bytes of the version nobody is looking at any more are let go.
    expect(revoked).toEqual([first]);
  });

  it("re-buys a row that carries no etag rather than serving bytes it cannot tell apart", async () => {
    stubObjectUrls();
    let clock = 0;
    const { minted } = filesServer({ [ROOT]: [row("n1", "file", "live.png", "")] });
    const port = cloudChatFiles({ now: () => clock });

    await port.resolveUrl(CHAT, "live.png");
    clock += 60_000;
    await port.resolveUrl(CHAT, "live.png");

    expect(minted).toHaveLength(2);
  });

  it("rejects with ChatFileNotReady while the bytes are still on the machine, and reads nothing", async () => {
    // "Wait", not "nothing": the image shows a loading state and asks again
    // when the drive says the folder changed, instead of the missing box.
    stubObjectUrls();
    const { reads } = filesServer(
      { [ROOT]: [row("n1", "file", "revenue.png")] },
      { files_node_id: ROOT },
      undefined,
      () =>
        new Response(
          JSON.stringify({ code: "files.live_pending", message: "on its way", detail: { holder: "box" } }),
          { status: 409, headers: { "retry-after": "2" } },
        ),
    );

    await expect(cloudChatFiles().resolveUrl(CHAT, "revenue.png")).rejects.toBeInstanceOf(ChatFileNotReady);
    expect(reads).toEqual([]);
  });

  it.each([
    ["a 404", 404, { code: "not_found" }],
    ["a 403", 403, { code: "files.forbidden", message: "no" }],
    ["a 409 that is not live_pending", 409, { code: "files.conflict", message: "no" }],
    ["a live_pending code on a status other than 409", 503, { code: "files.live_pending" }],
    ["a 500 with no body code", 500, {}],
  ])("answers null when the grant is refused with %s, and reads nothing", async (_label, status, body) => {
    stubObjectUrls();
    const { reads } = filesServer(
      { [ROOT]: [row("n1", "file", "revenue.png")] },
      { files_node_id: ROOT },
      undefined,
      () => new Response(JSON.stringify(body), { status }),
    );

    await expect(cloudChatFiles().resolveUrl(CHAT, "revenue.png")).resolves.toBeNull();
    expect(reads).toEqual([]);
  });

  it("a pending image that has since landed resolves on the next ask", async () => {
    stubObjectUrls();
    let landed = false;
    filesServer(
      { [ROOT]: [row("n1", "file", "revenue.png")] },
      { files_node_id: ROOT },
      undefined,
      () => (landed ? null : new Response(JSON.stringify({ code: "files.live_pending" }), { status: 409 })),
    );
    const port = cloudChatFiles();

    await expect(port.resolveUrl(CHAT, "revenue.png")).rejects.toBeInstanceOf(ChatFileNotReady);
    landed = true;
    await expect(port.resolveUrl(CHAT, "revenue.png")).resolves.toBe("blob:made-1");
  });

  it("answers null when the bytes do not come back", async () => {
    stubObjectUrls();
    filesServer({ [ROOT]: [row("n1", "file", "revenue.png")] });
    // The grant's own read is refused — a spent, expired or revoked token.
    const server = globalThis.fetch as unknown as Mock<(input: string | URL | Request, init?: RequestInit) => Promise<Response>>;
    const inner = server.getMockImplementation()!;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const href = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        if (href.startsWith(`${CONTENT}/c/`)) return new Response("", { status: 404 });
        return (await inner(input, init)) as Response;
      }),
    );

    await expect(cloudChatFiles().resolveUrl(CHAT, "revenue.png")).resolves.toBeNull();
  });

  it("re-reads once before giving up on a path that is not there, then answers null", async () => {
    const { listed, minted } = filesServer({ [ROOT]: [] });
    const port = cloudChatFiles();
    await expect(port.resolveUrl(CHAT, "gone.png")).resolves.toBeNull();
    expect(listed).toEqual([ROOT, ROOT]);
    expect(minted).toEqual([]);
  });

  it("answers null for a folder, and for a chat with no Files node — and buys nothing for either", async () => {
    const folder = filesServer({ [ROOT]: [row("f", "folder", "charts")] });
    await expect(cloudChatFiles().resolveUrl(CHAT, "charts")).resolves.toBeNull();
    expect(folder.minted).toEqual([]);
    const none = filesServer({}, { files_node_id: null });
    await expect(cloudChatFiles().resolveUrl(CHAT, "x.png")).resolves.toBeNull();
    expect(none.minted).toEqual([]);
  });

  it("openPath opens the resolved file's viewer URL", async () => {
    filesServer({ [ROOT]: [row("n1", "file", "report.csv")] });
    const openUrl = vi.fn();
    const port = cloudChatFiles({ openUrl });
    port.openPath?.(CHAT, "report.csv");
    await vi.waitFor(() => expect(openUrl).toHaveBeenCalledWith(`/api/v1/files/drives/${DRIVE}/items/n1/content?disposition=inline`));
  });
});

describe("cloudChatFiles.locate", () => {
  it("answers the node directly under the chat, the folder it sits in and the path it was asked for", async () => {
    filesServer({ [ROOT]: [row("n1", "file", "revenue.png")] });
    await expect(cloudChatFiles().locate?.(CHAT, "revenue.png")).resolves.toEqual({
      nodeId: "n1",
      driveId: DRIVE,
      parentId: ROOT,
      name: "revenue.png",
      path: "revenue.png",
      kind: "file",
    });
  });

  it("answers a node one folder down naming THAT folder as its parent, so a caller can open it and show where it lives", async () => {
    filesServer({
      [ROOT]: [row("f-work", "folder", "scratch")],
      "f-work": [row("n2", "file", "paste-1-ab12.png")],
    });
    await expect(cloudChatFiles().locate?.(CHAT, "paste-1-ab12.png")).resolves.toMatchObject({
      nodeId: "n2",
      parentId: "f-work",
      name: "paste-1-ab12.png",
      path: "paste-1-ab12.png",
    });
  });

  it("names the containing folder as the parent of a nested path, not the chat node", async () => {
    filesServer({
      [ROOT]: [row("f-charts", "folder", "charts")],
      "f-charts": [row("n3", "file", "q3.png")],
    });
    await expect(cloudChatFiles().locate?.(CHAT, "charts/q3.png")).resolves.toMatchObject({
      nodeId: "n3",
      parentId: "f-charts",
      name: "q3.png",
      path: "charts/q3.png",
    });
  });

  it("answers a folder as a folder — what it found, so the caller decides what may be opened", async () => {
    filesServer({ [ROOT]: [row("f-charts", "folder", "charts")] });
    const port = cloudChatFiles();
    await expect(port.locate?.(CHAT, "charts")).resolves.toMatchObject({ nodeId: "f-charts", kind: "folder" });
    // The content URL stays a file's business.
    await expect(port.resolveUrl(CHAT, "charts")).resolves.toBeNull();
  });

  it("answers null for a path that is not there, and for a chat with no folder", async () => {
    filesServer({ [ROOT]: [] });
    await expect(cloudChatFiles().locate?.(CHAT, "gone.png")).resolves.toBeNull();
    filesServer({}, { files_node_id: null });
    await expect(cloudChatFiles().locate?.(CHAT, "x.png")).resolves.toBeNull();
  });
});

/** An uploader that lands each file in `tree` under the folder it was sent to,
 *  a moment after the transfer finishes (the commit is queued server-side). */
function landingUploads(tree: Record<string, Row[]>, started: Array<{ name: string; parentId: string }> = []) {
  return () =>
    ({
      start: async (file: File, parentId: string) => {
        started.push({ name: file.name, parentId });
        setTimeout(() => (tree[parentId] ??= []).push(row(`n-${file.name}`, "file", file.name)), 2);
        return { done: async () => ({ state: "done" as const, uploadId: "u", name: file.name, sent: 1, total: 1, partsDone: 1, partsTotal: 1 }) };
      },
    }) as never;
}

function apiJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function memoryStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  const cells = new Map<string, string>();
  return {
    getItem: (key) => cells.get(key) ?? null,
    setItem: (key, value) => void cells.set(key, value),
    removeItem: (key) => void cells.delete(key),
  };
}

/** Where a racing writer takes the name a completion is about to commit: before
 *  the call (so the call is refused, 409), or between the call and the queued
 *  commit (so the call is accepted and the OPERATION is refused). */
type Race = "on-the-call" | "in-the-commit";

/**
 * The Files upload-session API over `tree`, driven by the REAL upload client.
 *
 * It holds the one rule the port leans on, the server's: a `fail` completion
 * creates a node and refuses a name the folder already holds — on the call when
 * the name is there already, inside the queued commit when another writer took
 * it after the call. `rename` and `replace` do what they do on the server
 * (a `name (2)` copy; new bytes on the node holding the name), so a port that
 * sent either could not pass by accident. `race(k)` puts another writer's file
 * at the k-th completion's name, which is how a drawn name "collides" without
 * the test steering the draw.
 */
function uploadSessions(tree: Record<string, Row[]>, race: (completion: number) => Race | null = () => null) {
  const sessions = new Map<string, { name: string; parentId: string; accepted: boolean }>();
  const operations = new Map<string, { id: string; state: string; errors?: Array<{ code: string; message: string }>; resultNodeId?: string }>();
  const theirs: Row[] = [];
  const released: string[] = [];
  const behaviours: string[] = [];
  let completions = 0;
  const fetchImpl = vi.fn(async (input: string | URL | Request, init: RequestInit = {}) => {
    const path = new URL(typeof input === "string" || input instanceof URL ? input : input.url, "http://localhost").pathname;
    const method = (init.method ?? "GET").toUpperCase();
    if (method === "POST" && path === "/api/v1/files/uploads") {
      const opened = JSON.parse(String(init.body)) as { name: string; parentId: string };
      const uploadId = `s-${sessions.size + 1}`;
      sessions.set(uploadId, { name: opened.name, parentId: opened.parentId, accepted: false });
      return apiJson(201, { uploadId, partSize: 1024, partsTotal: 1, expiresAt: "" });
    }
    const part = /^\/api\/v1\/files\/uploads\/([^/]+)\/parts\/(\d+)$/.exec(path);
    if (method === "PUT" && part) {
      sessions.get(part[1])!.accepted = true;
      return apiJson(200, { partNo: Number(part[2]), size: 1, duplicate: false });
    }
    const complete = /^\/api\/v1\/files\/uploads\/([^/]+)\/complete$/.exec(path);
    if (method === "POST" && complete) {
      completions += 1;
      const session = sessions.get(complete[1])!;
      const { conflictBehavior } = JSON.parse(String(init.body)) as { conflictBehavior: string };
      behaviours.push(conflictBehavior);
      const folder = (tree[session.parentId] ??= []);
      const racing = race(completions);
      const takeTheName = () => {
        const other = row(`theirs-${theirs.length + 1}`, "file", session.name, `theirs-v${theirs.length + 1}`);
        folder.push(other);
        theirs.push({ ...other });
      };
      if (racing === "on-the-call") takeTheName();
      const holder = folder.find((candidate) => candidate.name === session.name);
      if (conflictBehavior === "fail" && holder) {
        return apiJson(409, { code: "files.exists", message: "that name is taken in this folder" });
      }
      const operationId = `op-${operations.size + 1}`;
      if (racing === "in-the-commit") {
        takeTheName();
        operations.set(operationId, {
          id: operationId,
          state: "failed",
          errors: [{ code: "files.exists", message: "that name is taken in this folder" }],
        });
      } else if (holder && conflictBehavior === "replace") {
        holder.etag = `mine-${complete[1]}`;
        operations.set(operationId, { id: operationId, state: "done", resultNodeId: holder.id });
      } else {
        const name = holder ? session.name.replace(/(\.[^.]*)?$/, " (2)$1") : session.name;
        const mine = row(`n-${complete[1]}`, "file", name, `mine-${complete[1]}`);
        folder.push(mine);
        operations.set(operationId, { id: operationId, state: "done", resultNodeId: mine.id });
      }
      return apiJson(202, { id: operationId, kind: "upload", state: "queued", done: 0, total: 1 });
    }
    const operation = /^\/api\/v1\/files\/drives\/[^/]+\/operations\/([^/]+)$/.exec(path);
    if (method === "GET" && operation) return apiJson(200, operations.get(operation[1]));
    const session = /^\/api\/v1\/files\/uploads\/([^/]+)$/.exec(path);
    if (session && method === "GET") {
      const held = sessions.get(session[1])!;
      const parts = held.accepted ? [1] : [];
      return apiJson(200, {
        uploadId: session[1],
        state: "uploading",
        offset: parts.length,
        length: 1,
        complete: held.accepted,
        partsDone: parts.length,
        partsTotal: 1,
        acceptedParts: parts,
      });
    }
    if (session && method === "DELETE") {
      released.push(session[1]);
      return new Response(null, { status: 204 });
    }
    return apiJson(500, { code: "unexpected", message: `${method} ${path}` });
  });
  const uploads = (options: UploadClientOptions) =>
    new UploadClient({ ...options, fetchImpl, storage: memoryStorage(), digest: async (part) => `d${part.size}` });
  return { uploads, theirs, released, behaviours };
}

/** The folder a chat's uploads land in, already there. */
function chatWithUploads(): Record<string, Row[]> {
  return { [ROOT]: [row("f-up", "folder", "uploads")], "f-up": [] };
}

describe("cloudChatFiles.upload never lands on a name another file holds", () => {
  it.each([
    ["the call is refused (the name was already taken)", "on-the-call"],
    ["the queued commit is refused (the name was taken after the call)", "in-the-commit"],
  ] as const)("when %s, the other file survives and the message names the new one", async (_label, where) => {
    stubObjectUrls();
    const tree = chatWithUploads();
    const { minted } = filesServer(tree);
    const server = uploadSessions(tree, (completion) => (completion === 1 ? where : null));
    const port = cloudChatFiles({ uploads: server.uploads, settle: { attempts: 5, delayMs: 1 } });

    const landed = await port.upload(CHAT, new File(["mine"], "image.png", { type: "image/png" }), { kind: "image", n: 1 });

    // The file that held the name is untouched, and the only one of that name.
    const [other] = server.theirs;
    expect(tree["f-up"].filter((candidate) => candidate.name === other.name)).toEqual([other]);
    // The message carries a name of its own, in the staged shape, and it
    // resolves to the bytes this upload sent — not the other writer's.
    expect(landed.path).not.toBe(`uploads/${other.name}`);
    expect(landed.path).toMatch(/^uploads\/paste-1-[0-9a-f]{4}\.png$/);
    await expect(port.resolveUrl(CHAT, landed.path)).resolves.toBe("blob:made-1");
    const mine = tree["f-up"].find((candidate) => `uploads/${candidate.name}` === landed.path);
    expect(mine?.etag).toMatch(/^mine-/);
    expect(minted.map((call) => call.itemId)).toEqual([mine?.id]);
    // Create-only every time: never "keep both", never "replace".
    expect(server.behaviours).toEqual(["fail", "fail"]);
  });

  it("gives the refused session back rather than leaving its bytes held", async () => {
    const tree = chatWithUploads();
    filesServer(tree);
    const server = uploadSessions(tree, (completion) => (completion === 1 ? "on-the-call" : null));
    const port = cloudChatFiles({ uploads: server.uploads, settle: { attempts: 5, delayMs: 1 } });

    await port.upload(CHAT, new File(["mine"], "q.csv"), { kind: "file", n: 1 });

    expect(server.released).toEqual(["s-1"]);
  });

  it("rejects once every name it draws is taken, and has written over none of them", async () => {
    const tree = chatWithUploads();
    filesServer(tree);
    const server = uploadSessions(tree, () => "on-the-call");
    const port = cloudChatFiles({ uploads: server.uploads, settle: { attempts: 1, delayMs: 1 } });

    await expect(port.upload(CHAT, new File(["mine"], "q.csv"), { kind: "file", n: 1 })).rejects.toThrow(
      `no free name for this upload after ${STAGE_NAME_ATTEMPTS} tries`,
    );
    expect(server.behaviours).toHaveLength(STAGE_NAME_ATTEMPTS);
    expect(tree["f-up"]).toEqual(server.theirs);
  });
});

describe("cloudChatFiles.upload", () => {
  it("makes uploads/ in the chat's working directory, sends the file there, and answers the uploads/ path once it is listed", async () => {
    const tree: Record<string, Row[]> = { [ROOT]: [row("f-work", "folder", "scratch")], "f-work": [] };
    const { created } = filesServer(tree, { files_node_id: ROOT }, undefined, undefined, { [ROOT]: "f-work" });
    const started: Array<{ name: string; parentId: string }> = [];
    const port = cloudChatFiles({ uploads: landingUploads(tree, started), settle: { attempts: 20, delayMs: 2 } });

    const landed = await port.upload(CHAT, new File(["x"], "Screenshot.PNG", { type: "image/png" }), { kind: "image", n: 1 });

    expect(created).toEqual([{ parentId: "f-work", name: "uploads", kind: "folder", conflictBehavior: "fail" }]);
    const uploadsId = tree["f-work"].find((candidate) => candidate.name === "uploads")?.id;
    expect(started).toEqual([{ name: expect.stringMatching(/^paste-1-[0-9a-f]{4}\.png$/) as string, parentId: uploadsId }]);
    expect(landed.path).toBe(`uploads/${started[0].name}`);
  });

  it("finds an existing uploads/ with one listing and creates nothing", async () => {
    const tree: Record<string, Row[]> = {
      [ROOT]: [row("f-work", "folder", "scratch")],
      "f-work": [row("f-up", "folder", "uploads")],
      "f-up": [],
    };
    const { created, listed } = filesServer(tree, { files_node_id: ROOT }, undefined, undefined, { [ROOT]: "f-work" });
    const started: Array<{ name: string; parentId: string }> = [];
    const port = cloudChatFiles({ uploads: landingUploads(tree, started), settle: { attempts: 20, delayMs: 2 } });

    await port.upload(CHAT, new File(["x"], "q.csv"), { kind: "file", n: 1 });

    expect(created).toEqual([]);
    expect(started[0].parentId).toBe("f-up");
    // The working directory is listed once to find uploads/ before the send.
    expect(listed[0]).toBe("f-work");
  });

  it("a second upload into the same chat goes straight into the folder it already found", async () => {
    const tree: Record<string, Row[]> = { [ROOT]: [row("f-work", "folder", "scratch")], "f-work": [] };
    const { created, itemReads } = filesServer(tree, { files_node_id: ROOT }, undefined, undefined, { [ROOT]: "f-work" });
    const started: Array<{ name: string; parentId: string }> = [];
    const port = cloudChatFiles({ uploads: landingUploads(tree, started), settle: { attempts: 20, delayMs: 2 } });

    await port.upload(CHAT, new File(["a"], "a.png", { type: "image/png" }), { kind: "image", n: 1 });
    await port.upload(CHAT, new File(["b"], "b.png", { type: "image/png" }), { kind: "image", n: 2 });

    expect(created).toHaveLength(1);
    expect(itemReads).toEqual([ROOT]);
    expect(new Set(started.map((each) => each.parentId)).size).toBe(1);
  });

  it("a folder another upload made first is taken, not a second one", async () => {
    const tree: Record<string, Row[]> = { [ROOT]: [row("f-work", "folder", "scratch")], "f-work": [] };
    let first = true;
    // The first listing predates the other writer's folder; the create then
    // meets it and is refused on the name.
    const { created } = filesServer(
      tree,
      { files_node_id: ROOT },
      (parentId) => {
        if (parentId === "f-work" && first) {
          first = false;
          tree["f-work"].push(row("f-theirs", "folder", "uploads"));
          tree["f-theirs"] = [];
          return childrenPage([]);
        }
        return childrenPage(tree[parentId] ?? []);
      },
      undefined,
      { [ROOT]: "f-work" },
    );
    const started: Array<{ name: string; parentId: string }> = [];
    const port = cloudChatFiles({ uploads: landingUploads(tree, started), settle: { attempts: 20, delayMs: 2 } });

    await port.upload(CHAT, new File(["x"], "q.csv"), { kind: "file", n: 1 });

    expect(created).toHaveLength(1);
    expect(started[0].parentId).toBe("f-theirs");
    expect(tree["f-work"].filter((candidate) => candidate.name === "uploads")).toHaveLength(1);
  });

  it("a chat whose row names no working directory keeps uploads/ directly under its node", async () => {
    const tree: Record<string, Row[]> = { [ROOT]: [] };
    const { created } = filesServer(tree);
    const port = cloudChatFiles({ uploads: landingUploads(tree), settle: { attempts: 20, delayMs: 2 } });

    const landed = await port.upload(CHAT, new File(["x"], "q.csv"), { kind: "file", n: 1 });

    expect(created[0].parentId).toBe(ROOT);
    expect(landed.path).toMatch(/^uploads\/file-1-[0-9a-f]{4}\.csv$/);
  });

  it("rejects with the server's reason when uploads/ cannot be made, and tries again on the next attempt", async () => {
    const tree: Record<string, Row[]> = { [ROOT]: [row("f-work", "folder", "scratch")], "f-work": [] };
    const server = filesServer(tree, { files_node_id: ROOT }, undefined, undefined, { [ROOT]: "f-work" });
    const real = server.fetchImpl.getMockImplementation()!;
    let refuse = true;
    server.fetchImpl.mockImplementation(async (input, init = {}) => {
      if (refuse && init.method === "POST" && String(input).endsWith("/children")) {
        refuse = false;
        return new Response(JSON.stringify({ message: "this folder is being written by a running chat" }), { status: 423 });
      }
      return real(input, init);
    });
    const port = cloudChatFiles({ uploads: landingUploads(tree), settle: { attempts: 20, delayMs: 2 } });
    const file = new File(["x"], "q.csv");

    await expect(port.upload(CHAT, file, { kind: "file", n: 1 })).rejects.toMatchObject({ status: 423 });
    await expect(port.upload(CHAT, file, { kind: "file", n: 1 })).resolves.toMatchObject({ path: expect.stringMatching(/^uploads\//) as string });
  });

  it("rejects with the server's reason when the bytes did not land, so the composer retries", async () => {
    filesServer({ [ROOT]: [] });
    const uploads = () =>
      ({
        start: async () => ({
          done: async () => ({ state: "failed" as const, error: { message: "quota exceeded" }, uploadId: "u", name: "x", sent: 0, total: 1, partsDone: 0, partsTotal: 1 }),
        }),
      }) as never;
    const port = cloudChatFiles({ uploads });
    await expect(port.upload(CHAT, new File(["x"], "q.csv"), { kind: "file", n: 1 })).rejects.toThrow("quota exceeded");
  });

  it("lands a pasted image against the real listing wire and resolves it back by its uploads/ path", async () => {
    stubObjectUrls();
    const tree: Record<string, Row[]> = { [ROOT]: [row("f-work", "folder", "scratch")], "f-work": [] };
    const { minted } = filesServer(tree, { files_node_id: ROOT }, undefined, undefined, { [ROOT]: "f-work" });
    const port = cloudChatFiles({ uploads: landingUploads(tree), settle: { attempts: 20, delayMs: 2 } });
    const landed = await port.upload(CHAT, new File(["x"], "image.png", { type: "image/png" }), { kind: "image", n: 1 });
    expect(landed.path).toMatch(/^uploads\/paste-1-[0-9a-f]{4}\.png$/);
    await expect(port.resolveUrl(CHAT, landed.path)).resolves.toBe("blob:made-1");
    expect(minted.map((call) => call.itemId)).toEqual([`n-${landed.path.slice("uploads/".length)}`]);
  });

  it("an upload from before uploads/ existed — a bare name in the working directory — still resolves", async () => {
    stubObjectUrls();
    const { minted } = filesServer({
      [ROOT]: [row("f-work", "folder", "scratch")],
      "f-work": [row("old", "file", "paste-1-ab12.png"), row("f-up", "folder", "uploads")],
      "f-up": [],
    });
    await expect(cloudChatFiles().resolveUrl(CHAT, "paste-1-ab12.png")).resolves.toBe("blob:made-1");
    expect(minted.map((call) => call.itemId)).toEqual(["old"]);
  });

  it("a listing that comes back without its rows fails the upload with a stated reason, never a TypeError", async () => {
    filesServer({ [ROOT]: [] }, { files_node_id: ROOT }, () => ({ nextMarker: null }));
    const port = cloudChatFiles({ uploads: landingUploads({}), settle: { attempts: 2, delayMs: 1 } });
    const outcome = port.upload(CHAT, new File(["x"], "image.png", { type: "image/png" }), { kind: "image", n: 1 });
    await expect(outcome).rejects.not.toBeInstanceOf(TypeError);
    await expect(outcome).rejects.toThrow(/listing/);
  });

  it("refuses to upload into a chat that has no folder", async () => {
    filesServer({}, { files_node_id: null });
    await expect(cloudChatFiles().upload(CHAT, new File(["x"], "q.csv"), { kind: "file", n: 1 })).rejects.toThrow(/no folder/);
  });
});

describe("stagedFileName", () => {
  it.each([
    ["image", 1, "Screenshot 2026.PNG", /^uploads\/paste-1-[0-9a-f]{4}\.png$/],
    ["file", 2, "quarterly report.csv", /^uploads\/file-2-[0-9a-f]{4}\.csv$/],
    ["file", 1, "Makefile", /^uploads\/file-1-[0-9a-f]{4}$/],
  ] as const)("%s %d %s lands under uploads/ in the deterministic shape", (kind, n, original, shape) => {
    expect(stagedFileName(kind, n, original)).toMatch(shape);
  });

  // Two random bytes as four hex digits, zero-padded: the box's
  // `secrets.token_hex(2)`, so a name has one shape whichever shell drew it.
  // Two draws CAN match — that is the upload's to survive, not the name's.
  it.each([
    [[0xab, 0x01], "uploads/paste-1-ab01.png"],
    [[0x00, 0x0f], "uploads/paste-1-000f.png"],
  ] as const)("draws its id from %j as four hex digits", (bytes, expected) => {
    const draw = vi.spyOn(crypto, "getRandomValues").mockImplementation(<T extends ArrayBufferView | null>(array: T): T => {
      (array as unknown as Uint8Array).set(bytes);
      return array;
    });
    try {
      expect(stagedFileName("image", 1, "a.png")).toBe(expected);
    } finally {
      draw.mockRestore();
    }
  });
});
