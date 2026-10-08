import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { uploadStorageKey, UploadClient, type ResumableUpload } from "@/api/filesUpload";

/** Who the uploads are remembered for. */
const ACCOUNT = { userId: "usr_dana", orgId: "org_a" };

// Two files a resume cannot tell apart.
//
// A session is remembered by the folder, the name, the size and the
// modification time. Two DIFFERENT files agreeing on all four is ordinary — two
// exports written in the same second, a template copied into two folders, a
// note corrected without changing its length — and none of those four facts is
// about the bytes. When the client adopted such a session it did not merely
// share it: the pump skips every part the server reports as accepted, so the
// second file sent nothing at all and the commit landed the FIRST file's bytes
// under the second file's row, reading `done`.
//
// The records live in `localStorage`, which survives a reload and is shared
// across tabs, so the misidentification outlives any one page. Every assertion
// here is therefore on the bytes the server COMMITTED, never on a session id.

/** What the server holds for one part: the bytes, and the size and digest it
 *  was handed them under. A commit has to declare all three back. */
interface HeldPart {
  body: string;
  size: number;
  checksum: string;
}

interface Wire {
  opens: number;
  /** What each session holds, so a commit can say what it actually landed. */
  parts: Map<string, Map<number, HeldPart>>;
  /** How many parts each session was opened for, from its declared size. */
  partsTotal: Map<string, number>;
  /** `[sessionId, body]` per completed commit. */
  committed: [string, string][];
  /** Commits the server refused, and why. */
  refused: string[];
  /** A part number the server never answers, so a client can be held mid-flight. */
  hangPart: number | null;
  /** Set once that part has actually been asked for, which is the only moment
   *  the client is provably inside a request that will never come back. */
  hung: boolean;
}

const PART_SIZE = 4;

/** One part of a commit's declared list, as the session API takes it. */
interface PartRef {
  partNo: number;
  size: number;
  checksum: string;
}

/**
 * The server's own agreement rule (`_agree`, `alkera_core/files/uploads.py`).
 *
 * A commit declares a size and a digest for EVERY part the server holds, and a
 * list that is not exactly what it holds is refused. That matters here because
 * the client derives the digest of any part it did not itself send from the
 * LOCAL file — so a session adopted by the wrong file declares that file's
 * digests over the other file's bytes, and the disagreement is what the real
 * server catches. A fake that accepts any list makes a spliced commit look like
 * a silent loss, which is not what production does.
 */
function agree(declared: readonly PartRef[], held: ReadonlyMap<number, HeldPart>): string | null {
  if (declared.length !== held.size) return "files.parts_mismatch";
  for (const part of declared) {
    const mine = held.get(part.partNo);
    if (!mine || mine.size !== part.size || mine.checksum !== part.checksum) {
      return "files.parts_mismatch";
    }
  }
  return null;
}

function memoryStorage() {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
}

type Storage = ReturnType<typeof memoryStorage>;

/** jsdom's `Blob` is not the one `Response` consumes, so bytes are read the way
 *  the platform reads a picked file. */
function blobText(part: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("unreadable"));
    reader.readAsText(part);
  });
}

/** A digest of the bytes themselves — the only kind that can tell two files
 *  apart, and what the real BLAKE3 part digest is. */
async function textDigest(part: Blob): Promise<string> {
  const text = await blobText(part);
  let hash = 2166136261;
  for (let at = 0; at < text.length; at += 1) {
    hash ^= text.charCodeAt(at);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16);
}

function stub(): Wire {
  const wire: Wire = {
    opens: 0,
    parts: new Map(),
    partsTotal: new Map(),
    committed: [],
    refused: [],
    hangPart: null,
    hung: false,
  };
  const json = (payload: unknown, status = 200): Response =>
    new Response(JSON.stringify(payload), {
      status,
      headers: { "content-type": "application/json" },
    });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const path = new URL(url, "http://localhost").pathname;
      const method = (init?.method ?? "GET").toUpperCase();

      if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
        wire.opens += 1;
        const id = `sess-${wire.opens}`;
        const asked = JSON.parse(String(init?.body ?? "{}")) as { declaredSize?: number };
        const total = Math.max(1, Math.ceil((asked.declaredSize ?? PART_SIZE) / PART_SIZE));
        wire.parts.set(id, new Map());
        wire.partsTotal.set(id, total);
        return json({ uploadId: id, partSize: PART_SIZE, partsTotal: total, expiresAt: "" });
      }
      const session = /\/uploads\/(sess-\d+)(\/|$)/.exec(path)?.[1] ?? "";
      const held = wire.parts.get(session) ?? new Map<number, HeldPart>();

      if (path.endsWith("/complete")) {
        const declared = (JSON.parse(String(init?.body ?? "{}")) as { parts?: PartRef[] }).parts;
        const refusal = agree(declared ?? [], held);
        if (refusal !== null) {
          wire.refused.push(refusal);
          return json({ code: refusal, message: "the completing part list disagrees" }, 409);
        }
        const body = [...held.entries()]
          .sort((a, b) => a[0] - b[0])
          .map(([, part]) => part.body)
          .join("");
        wire.committed.push([session, body]);
        return json({ id: `op-${session}`, state: "done", resultNodeId: `node-${session}` }, 202);
      }
      if (method === "PUT") {
        const part = Number(path.slice(path.lastIndexOf("/") + 1));
        if (wire.hangPart === part) {
          wire.hung = true;
          return new Promise<Response>(() => undefined);
        }
        const body = init?.body instanceof Blob ? await blobText(init.body) : "";
        const checksum = new Headers(init?.headers ?? {}).get("x-part-checksum") ?? "";
        held.set(part, { body, size: body.length, checksum });
        wire.parts.set(session, held);
        return json({ ok: true });
      }
      if (method === "GET" && session !== "") {
        const accepted = [...held.keys()].sort((a, b) => a - b);
        const total = wire.partsTotal.get(session) ?? 1;
        return json({
          uploadId: session,
          state: "open",
          offset: accepted.length * PART_SIZE,
          length: PART_SIZE * total,
          complete: false,
          partsDone: accepted.length,
          partsTotal: total,
          acceptedParts: accepted,
        });
      }
      return json({ code: "unexpected", message: url }, 500);
    }),
  );
  return wire;
}

const STAMP = Date.UTC(2020, 0, 1);

/** Eight bytes, so the upload is two parts and can be held after the first. */
function eightBytes(body: string): File {
  return new File([body], "twin.txt", { lastModified: STAMP });
}

/** Twelve bytes: three parts, so a session can hold two of them and a file
 *  offered later can agree on the first and disagree after it. */
function twelveBytes(body: string): File {
  return new File([body], "twin.txt", { lastModified: STAMP });
}

function client(storage: Storage): UploadClient {
  return new UploadClient({ account: ACCOUNT, driveId: "drive-1", storage, digest: textDigest });
}

/** The records this browser is holding, as the client wrote them. */
function records(storage: Storage): ResumableUpload[] {
  return Object.values(
    JSON.parse(storage.getItem(uploadStorageKey(ACCOUNT)) ?? "{}") as Record<string, ResumableUpload>,
  );
}

/** Drive one file until the server holds its first part, and leave it there —
 *  the state a reload, a closed tab or a navigation away leaves behind. */
async function heldAfterPartOne(storage: Storage, wire: Wire, body: string): Promise<void> {
  await parkedAt(2, () => client(storage).start(eightBytes(body), "folder-1"), wire);
}

/** The same, one part later: the server holds parts 1 and 2 of a three-part
 *  file, which is the state a reload leaves a large upload in. */
async function heldAfterTwoParts(storage: Storage, wire: Wire, body: string): Promise<void> {
  await parkedAt(3, () => client(storage).start(twelveBytes(body), "folder-1"), wire);
}

/**
 * Start an upload and leave it stopped dead inside part `hangPart`.
 *
 * The wait is for the server to have BEEN ASKED for that part, not for the one
 * before it to have landed: until the request is in flight the client is still
 * free to send it, and clearing the hang a moment too early lets the upload run
 * to completion — in a later test, against a later fake, committing bytes
 * nothing in that test asked for.
 */
async function parkedAt(hangPart: number, run: () => Promise<unknown>, wire: Wire): Promise<void> {
  wire.hangPart = hangPart;
  wire.hung = false;
  void run();
  await vi.waitFor(() => expect(wire.hung).toBe(true));
  wire.hangPart = null;
}

let wire: Wire;
let storage: Storage;

beforeEach(() => {
  wire = stub();
  storage = memoryStorage();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a remembered session and a file that is not the one it was opened for", () => {
  it("commits the bytes the person actually dropped", async () => {
    await heldAfterPartOne(storage, wire, "MINEMINE");

    // A reload, or a second tab: a fresh client over the same storage, handed a
    // DIFFERENT file that agrees on folder, name, size and modification time.
    const after = await client(storage).start(eightBytes("OURSOURS"), "folder-1");
    await after.done();

    const landed = wire.committed.map(([, body]) => body);
    expect(landed).toContain("OURSOURS");
    expect(landed).not.toContain("MINEMINE");
  });

  it("opens a session of its own rather than sending into the remembered one", async () => {
    await heldAfterPartOne(storage, wire, "MINEMINE");

    const after = await client(storage).start(eightBytes("OURSOURS"), "folder-1");
    expect(after.uploadId).not.toBe("sess-1");
    expect(wire.opens).toBe(2);
    // Let it finish before the test ends: an upload still running is an upload
    // that commits into the NEXT test's server.
    await after.done();
  });

  it("does not rejoin a session that agrees only on its first part", async () => {
    // The case a single remembered digest cannot see: three parts, the server
    // holding the first two of `MINEMINEMINE`, and a file offered later that
    // opens with the same `MINE` and differs after it. Skipping the accepted
    // parts would splice `MINEMINEOURS` out of two files — which the server's
    // own part agreement refuses, so what the person actually got was a
    // `files.parts_mismatch` they have no way to act on.
    await heldAfterTwoParts(storage, wire, "MINEMINEMINE");

    const after = await client(storage).start(twelveBytes("MINEOURSOURS"), "folder-1");
    await after.done();

    expect(wire.committed.map(([, body]) => body)).toEqual(["MINEOURSOURS"]);
    // And not by way of a refusal: the bytes go up under a session of their own.
    expect(wire.refused).toEqual([]);
    expect(wire.opens).toBe(2);
  });

  it("still resumes a session that agrees on every part it holds", async () => {
    await heldAfterTwoParts(storage, wire, "MINEMINEMINE");

    const again = await client(storage).start(twelveBytes("MINEMINEMINE"), "folder-1");
    expect(again.uploadId).toBe("sess-1");
    await again.done();

    expect(wire.opens).toBe(1);
    expect(wire.committed).toEqual([["sess-1", "MINEMINEMINE"]]);
  });

  it("refuses a record from before the digest was kept rather than guessing", async () => {
    await heldAfterPartOne(storage, wire, "MINEMINE");
    // An older build wrote the four facts and nothing about the bytes, so this
    // record cannot identify anything. One re-upload is the honest price.
    const stored = JSON.parse(storage.getItem(uploadStorageKey(ACCOUNT)) ?? "{}") as Record<
      string,
      ResumableUpload
    >;
    for (const entry of Object.values(stored)) delete entry.partDigests;
    storage.setItem(uploadStorageKey(ACCOUNT), JSON.stringify(stored));

    const after = await client(storage).start(eightBytes("MINEMINE"), "folder-1");
    expect(after.uploadId).not.toBe("sess-1");
  });
});

describe("a session the server will no longer complete", () => {
  it("is forgotten, so sending the file again starts a new one", async () => {
    // The session is legitimately this file's — the remembered digest is what
    // its first part hashes to, so the resume is the right decision — but the
    // server's copy of that part carries a digest the completion will not
    // declare. A sweep, a rewritten part, a version skew: the commit can never
    // be agreed, whatever the client does next.
    const file = eightBytes("MINEMINE");
    const mine = await textDigest(new Blob(["MINE"]));
    wire.opens += 1;
    wire.parts.set("sess-1", new Map([[1, { body: "MINE", size: 4, checksum: "not-mine" }]]));
    wire.partsTotal.set("sess-1", 2);
    storage.setItem(
      uploadStorageKey(ACCOUNT),
      JSON.stringify({
        [`folder-1:twin.txt:${file.size}:${STAMP}`]: {
          uploadId: "sess-1",
          name: file.name,
          size: file.size,
          lastModified: STAMP,
          parentId: "folder-1",
          partSize: PART_SIZE,
          partDigests: { "1": mine },
        },
      }),
    );

    const refused = await client(storage).start(file, "folder-1");
    const settled = await refused.done();
    expect([settled.state, settled.error?.code]).toEqual(["failed", "files.parts_mismatch"]);

    // Retry. A refusal the session cannot recover from must not leave the
    // record behind: rejoining it fails the same way every time, and the row is
    // a dead end nothing on the page can clear.
    const again = await client(storage).start(file, "folder-1");
    expect(again.uploadId).not.toBe("sess-1");
    expect((await again.done()).state).toBe("done");
  });
});

describe("a remembered session and the file it was opened for", () => {
  it("resumes it, and sends only the part the server does not hold", async () => {
    await heldAfterPartOne(storage, wire, "MINEMINE");

    const again = await client(storage).start(eightBytes("MINEMINE"), "folder-1");
    expect(again.uploadId).toBe("sess-1");
    await again.done();

    expect(wire.opens).toBe(1);
    expect(wire.committed).toEqual([["sess-1", "MINEMINE"]]);
  });

  it("records what every part it sent hashes to, so the next visit can check", async () => {
    await heldAfterTwoParts(storage, wire, "MINEMINEMINE");

    const kept = records(storage);
    expect(kept).toHaveLength(1);
    // Both parts the server holds, not just the first: a file that opens the
    // same way and differs later is exactly what one digest cannot see.
    expect(kept[0]?.partDigests).toEqual({
      "1": await textDigest(new Blob(["MINE"])),
      "2": await textDigest(new Blob(["MINE"])),
    });
  });
});

describe("two files with the same four facts in one page load", () => {
  it("each get a session of their own while both are in flight", async () => {
    const one = client(storage);
    const first = await one.start(eightBytes("AAAAAAAA"), "folder-1");
    const second = await one.start(eightBytes("BBBBBBBB"), "folder-1");

    expect(first.uploadId).not.toBe(second.uploadId);
    await Promise.all([first.done(), second.done()]);
    const landed = wire.committed.map(([, body]) => body).sort();
    expect(landed).toEqual(["AAAAAAAA", "BBBBBBBB"]);
  });
});
