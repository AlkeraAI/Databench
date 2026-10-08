// The one thing the mocked tiers could not see: WHICH digest the browser puts
// in `X-Part-Checksum`.
//
// The part route does not merely record that header — the object-store driver
// re-hashes the streamed bytes with BLAKE3 and answers
// `422 files.part_checksum_mismatch` when the two disagree. Every other suite
// here injects a predictable digest so its assertions read the slicing; that is
// exactly why a client shipping the wrong algorithm reached a live stack. So
// this file uses the client's REAL default digest, and the fake it drives
// verifies the way the route does: against BLAKE3 values produced by an
// independent implementation (the Rust-backed `blake3` Python package, whose
// answers below match the published BLAKE3 test vectors).

import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { UploadClient, blake3Hex } from "@/api/filesUpload";

const PARENT = "33333333-3333-3333-3333-333333333333";
const SESSION = "55555555-5555-5555-5555-555555555555";

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

/** The reference corpus: byte `i` is `i % 251`, the pattern the published
 *  BLAKE3 vectors are defined over. */
function pattern(length: number): Uint8Array<ArrayBuffer> {
  const bytes = new Uint8Array(new ArrayBuffer(length));
  for (let i = 0; i < length; i += 1) bytes[i] = i % 251;
  return bytes;
}

/** Hex digests of that corpus, from an implementation that is not this one.
 *  The 0, 1, 1024, 2048 and 3072 rows are the published BLAKE3 test vectors. */
const VECTORS: Record<number, string> = {
  0: "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262",
  1: "2d3adedff11b61f14c886e35afa036736dcd87a74d27b5c1510225d0f592e213",
  63: "e9bc37a594daad83be9470df7f7b3798297c3d834ce80ba85d6e207627b7db7b",
  64: "4eed7141ea4a5cd4b788606bd23f46e212af9cacebacdc7d1f4c6dc7f2511b98",
  65: "de1e5fa0be70df6d2be8fffd0e99ceaa8eb6e8c93a63f2d8d1c30ecb6b263dee",
  1023: "10108970eeda3eb932baac1428c7a2163b0e924c9a9e25b35bba72b28f70bd11",
  1024: "42214739f095a406f3fc83deb889744ac00df831c10daa55189b5d121c855af7",
  1025: "d00278ae47eb27b34faecf67b4fe263f82d5412916c1ffd97c8cb7fb814b8444",
  2048: "e776b6028c7cd22a4d0ba182a8bf62205d2ef576467e838ed6f2529b85fba24a",
  3072: "b98cb0ff3623be03326b373de6b9095218513e64f1ee2edd2525c7ad1e5cffd2",
  4096: "015094013f57a5277b59d8475c0501042c0b642e531b0a1c8f58d2163229e969",
  6144: "3e2e5b74e048f3add6d21faab3f83aa44d3b2278afb83b80b3c35164ebeca205",
  8192: "aae792484c8efe4f19e2ca7d371d8c467ffb10748d8a5a1ae579948f718a2a63",
};

describe("the part digest", () => {
  it.each(Object.keys(VECTORS).map(Number))(
    "matches the reference BLAKE3 of %i bytes",
    (length) => {
      expect(blake3Hex(pattern(length).buffer)).toBe(VECTORS[length]);
    },
  );
});

/** A file whose bytes are the reference corpus, so the digest of every slice
 *  the client takes is a value this test already knows. */
function corpusFile(bytes: number): File {
  const file = new File([pattern(bytes)], "corpus.bin", { type: "application/octet-stream" });
  Object.defineProperty(file, "lastModified", { value: 1_700_000_000_000 });
  return file;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/**
 * The session API as the real one behaves on the point at issue: a part whose
 * `X-Part-Checksum` is not the BLAKE3 of the bytes it carries is refused with
 * the route's own envelope, and never lands.
 *
 * `expected` is keyed by part number and is stated from outside this module, so
 * a client that hashed with anything else is refused here for the same reason
 * it is refused on the wire.
 */
function verifyingServer(options: {
  partSize: number;
  total: number;
  expected: Record<number, string>;
}): {
  impl: ReturnType<typeof vi.fn>;
  accepted: Map<number, string>;
  refused: { partNo: number; sent: string }[];
} {
  const accepted = new Map<number, string>();
  const refused: { partNo: number; sent: string }[] = [];
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const headers = new Headers(init?.headers);

    if (method === "POST" && url.endsWith("/api/v1/files/uploads")) {
      return json(201, {
        uploadId: SESSION,
        partSize: options.partSize,
        partsTotal: options.total,
        limits: { maxPartBytes: options.partSize, maxParts: 10000 },
        expiresAt: "2026-01-01T00:00:00+00:00",
      });
    }
    const part = /\/parts\/(\d+)$/.exec(url);
    if (method === "PUT" && part) {
      const partNo = Number(part[1]);
      const sent = headers.get("X-Part-Checksum") ?? "";
      if (sent !== options.expected[partNo]) {
        refused.push({ partNo, sent });
        return json(422, {
          code: "files.part_checksum_mismatch",
          message: `part ${partNo} does not match the checksum it declared`,
        });
      }
      accepted.set(partNo, sent);
      return json(200, { partNo, size: 0, duplicate: false });
    }
    if (method === "GET" && url.endsWith(`/uploads/${SESSION}`)) {
      const done = [...accepted.keys()].sort((a, b) => a - b);
      return json(200, {
        uploadId: SESSION,
        state: "open",
        offset: 0,
        length: 0,
        complete: false,
        partsDone: done.length,
        partsTotal: options.total,
        acceptedParts: done,
        expiresAt: "2026-01-01T00:00:00+00:00",
      });
    }
    if (method === "POST" && url.endsWith("/complete")) {
      return json(202, { operationId: "op", state: "queued" });
    }
    return json(404, { code: "not_found", message: url });
  });
  return { impl, accepted, refused };
}

const noStorage = {
  getItem: (): string | null => null,
  setItem: (): void => {},
  removeItem: (): void => {},
};

describe("the browser's parts, against a route that verifies them", () => {
  it("sends a checksum the store accepts, for a one-part upload", async () => {
    const server = verifyingServer({
      partSize: 1024,
      total: 1,
      expected: { 1: VECTORS[1024]! },
    });
    const client = new UploadClient({
      fetchImpl: server.impl as unknown as typeof fetch,
      storage: noStorage,
    });
    const done = await (await client.start(corpusFile(1024), PARENT)).done();

    expect(server.refused).toEqual([]);
    expect(server.accepted.get(1)).toBe(VECTORS[1024]);
    expect(done.state).toBe("done");
  });

  it("sends a checksum the store accepts for every part of a multi-part upload", async () => {
    // Two parts of 1024 whose digests differ — so a client that hashed the
    // whole file, or hashed with the wrong algorithm, cannot pass by accident.
    const second = pattern(2048).slice(1024);
    const server = verifyingServer({
      partSize: 1024,
      total: 2,
      expected: { 1: VECTORS[1024]!, 2: blake3Hex(second.buffer) },
    });
    const client = new UploadClient({
      fetchImpl: server.impl as unknown as typeof fetch,
      storage: noStorage,
    });
    const done = await (await client.start(corpusFile(2048), PARENT)).done();

    expect(server.refused).toEqual([]);
    expect([...server.accepted.keys()].sort()).toEqual([1, 2]);
    expect(done.state).toBe("done");
  });

  it("fails the upload, holding the route's 422, when a part is refused", async () => {
    const server = verifyingServer({
      partSize: 1024,
      total: 1,
      expected: { 1: "00".repeat(32) },
    });
    const client = new UploadClient({
      fetchImpl: server.impl as unknown as typeof fetch,
      storage: noStorage,
    });
    const done = await (await client.start(corpusFile(1024), PARENT)).done();

    expect(done.state).toBe("failed");
    expect(done.error).toBeInstanceOf(ApiError);
    expect(done.error?.status).toBe(422);
    // Nothing landed: a refused part is not silently counted as progress.
    expect(server.accepted.size).toBe(0);
    expect(server.refused.map((entry) => entry.partNo)).toEqual([1]);
  });
});
