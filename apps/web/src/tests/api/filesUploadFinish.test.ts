// What finishing an upload costs the file it is finishing.
//
// The bytes are read ONCE per part. Each part is hashed as it is sent, and the
// completion assembles the digests it already has rather than reading the whole
// file again. On a 10 GB file that second pass is a second 10 GB disk read and
// a second multi-minute hash, all of it after the last byte has landed and with
// the row stuck on "completing" — so this counts the reads the `File` itself is
// asked for, which is the cost, rather than any call of a private helper.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { UploadClient } from "@/api/filesUpload";

const PARENT = "33333333-3333-3333-3333-333333333333";
const SESSION = "55555555-5555-5555-5555-555555555555";

// jsdom's Blob has no `arrayBuffer()`; the digest below reads each slice.
if (typeof Blob !== "undefined" && typeof Blob.prototype.arrayBuffer !== "function") {
  Blob.prototype.arrayBuffer = function readSlice(this: Blob): Promise<ArrayBuffer> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = () => reject(reader.error);
      reader.readAsArrayBuffer(this);
    });
  };
}

const PART_SIZE = 8;
const PARTS = 4;

/** Part `n` is `PART_SIZE` copies of `n`, so every part's digest differs and a
 *  client that named the wrong slice cannot pass by accident. */
function corpus(): Uint8Array<ArrayBuffer> {
  const bytes = new Uint8Array(new ArrayBuffer(PART_SIZE * PARTS));
  for (let part = 1; part <= PARTS; part += 1) {
    bytes.fill(part, (part - 1) * PART_SIZE, part * PART_SIZE);
  }
  return bytes;
}

/** A content-dependent digest, so the completion body says WHICH bytes were
 *  hashed and not merely that something was. */
async function digest(part: Blob): Promise<string> {
  const bytes = new Uint8Array(await part.arrayBuffer());
  let rolled = 7;
  for (const byte of bytes) rolled = (rolled * 31 + byte) % 1_000_003;
  return `d${part.size}-${rolled}`;
}

/** What the completion should say about part `n`, derived here rather than read
 *  back from the client. */
async function partOf(partNo: number): Promise<{
  partNo: number;
  size: number;
  checksum: string;
}> {
  const slice = new Blob([corpus().slice((partNo - 1) * PART_SIZE, partNo * PART_SIZE)]);
  return { partNo, size: PART_SIZE, checksum: await digest(slice) };
}

/**
 * The corpus as a `File` that records every read of its bytes.
 *
 * `slice` is the only way the client gets at the file — the request body and
 * the hash are both taken from the Blob it hands back — so the windows recorded
 * here ARE the passes made over the file.
 */
function countedFile(): { file: File; reads: number[] } {
  const file = new File([corpus()], "corpus.bin", { type: "application/octet-stream" });
  Object.defineProperty(file, "lastModified", { value: 1_700_000_000_000 });
  const reads: number[] = [];
  const underlying = file.slice.bind(file);
  Object.defineProperty(file, "slice", {
    value: (start?: number, end?: number): Blob => {
      reads.push(start ?? 0);
      return underlying(start, end);
    },
  });
  return { file, reads };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

interface CompletionBody {
  parts: { partNo: number; size: number; checksum: string }[];
  conflictBehavior: string;
}

/** The session API, holding whatever parts a previous run already landed. */
function server(held: number[] = []) {
  const accepted = new Set(held);
  const sent = new Map<number, string>();
  const completions: CompletionBody[] = [];

  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const path = new URL(String(input), "http://localhost").pathname;
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
      return json(200, {
        uploadId: SESSION,
        partSize: PART_SIZE,
        partsTotal: PARTS,
        expiresAt: "",
      });
    }
    const part = /\/parts\/(\d+)$/.exec(path);
    if (method === "PUT" && part) {
      const partNo = Number(part[1]);
      sent.set(partNo, new Headers(init?.headers).get("X-Part-Checksum") ?? "");
      accepted.add(partNo);
      return json(200, { partNo, size: PART_SIZE, duplicate: false });
    }
    if (method === "GET" && path.endsWith(`/uploads/${SESSION}`)) {
      const done = [...accepted].sort((a, b) => a - b);
      return json(200, {
        uploadId: SESSION,
        state: "open",
        offset: done.length * PART_SIZE,
        length: PART_SIZE * PARTS,
        complete: done.length === PARTS,
        partsDone: done.length,
        partsTotal: PARTS,
        acceptedParts: done,
      });
    }
    if (method === "POST" && path.endsWith("/complete")) {
      completions.push(JSON.parse(String(init?.body)) as CompletionBody);
      return json(200, { item: { id: "node-1", etag: "etag-1" } });
    }
    return json(500, { code: "unexpected", message: path });
  });

  return { impl, completions, sentChecksums: sent };
}

function memoryStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  const cells = new Map<string, string>();
  return {
    getItem: (key: string): string | null => cells.get(key) ?? null,
    setItem: (key: string, value: string): void => void cells.set(key, value),
    removeItem: (key: string): void => void cells.delete(key),
  };
}

function client(impl: typeof fetch): UploadClient {
  return new UploadClient({ fetchImpl: impl, storage: memoryStorage(), digest });
}

const EVERY_WINDOW = [0, PART_SIZE, PART_SIZE * 2, PART_SIZE * 3];

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("completing an upload this run sent every part of", () => {
  it("reads the file once per part, not twice", async () => {
    const { impl } = server();
    const { file, reads } = countedFile();

    const handle = await client(impl as unknown as typeof fetch).start(file, PARENT);
    const progress = await handle.done();

    expect(progress.state).toBe("done");
    // A completion that re-hashed what it had just sent would ask the file for
    // the same four windows a second time.
    expect(reads).toEqual(EVERY_WINDOW);
  });

  it("names every accepted part, with the digest that part travelled under", async () => {
    const answer = server();
    const { file } = countedFile();

    const handle = await client(answer.impl as unknown as typeof fetch).start(file, PARENT);
    await handle.done();

    const expected = await Promise.all([1, 2, 3, 4].map(partOf));
    expect(answer.completions).toHaveLength(1);
    expect(answer.completions[0]?.parts).toEqual(expected);
    expect([...answer.sentChecksums.entries()].sort((a, b) => a[0] - b[0])).toEqual(
      expected.map((part) => [part.partNo, part.checksum]),
    );
  });
});

describe("completing an upload a previous run had started", () => {
  it("hashes only the parts it did not send, and still names them all", async () => {
    // Parts 1 and 2 are already on the server; this run sends 3 and 4. The two
    // it did not send have no remembered digest, so those alone are read.
    const answer = server([1, 2]);
    const { file, reads } = countedFile();

    const handle = await client(answer.impl as unknown as typeof fetch).start(file, PARENT);
    const progress = await handle.done();

    expect(progress.state).toBe("done");
    expect(answer.completions[0]?.parts).toEqual(await Promise.all([1, 2, 3, 4].map(partOf)));
    // Four windows in all: 3 and 4 as they were sent, 1 and 2 to learn what this
    // run never computed. Never the same window twice.
    expect([...reads].sort((a, b) => a - b)).toEqual(EVERY_WINDOW);
  });
});
