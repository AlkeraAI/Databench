// Hashing a part must not freeze the tab.
//
// BLAKE3 in JS costs ~1 s per 32 MiB, so a 1 GB drop hashed on the main thread
// is ~30 s of a dead page — nothing scrolls, the tray does not paint, cancel
// does not answer. The client therefore hands each part to a pool of module
// workers. What has to stay true, and is pinned here:
//
//  * the digest that comes back through a worker is the SAME digest the
//    in-thread hasher produces — the store re-hashes the part and answers
//    `422 files.part_checksum_mismatch` on any disagreement, so a worker path
//    that drifted by a byte would break every upload;
//  * the caller's frame is free while a part hashes (a timer scheduled before
//    the call runs before the digest arrives), which an in-thread digest
//    cannot do;
//  * a worker that never loads, or dies mid-part, costs speed and nothing else:
//    the part is hashed in-thread and the upload still completes.
//
// jsdom has no `Worker`, so the pool is driven through an injected factory
// whose fake runs the worker module's OWN `answer()` — the same code the real
// worker runs on a message.

import { describe, expect, it, vi } from "vitest";

import { blake3Hex } from "@/api/blake3";
import { answer, type HashReply, type HashRequest } from "@/api/blake3.worker";
import { UploadClient, createWorkerDigest, type HashWorker } from "@/api/filesUpload";

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

/** The reference corpus the published BLAKE3 vectors are defined over. */
function pattern(length: number): Uint8Array<ArrayBuffer> {
  const bytes = new Uint8Array(new ArrayBuffer(length));
  for (let i = 0; i < length; i += 1) bytes[i] = i % 251;
  return bytes;
}

/** Published BLAKE3 test vectors of that corpus — the same rows the in-thread
 *  suite pins, restated here so the WORKER path is checked against the
 *  standard and not merely against this repo's other function. */
const VECTORS: Record<number, string> = {
  0: "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262",
  1: "2d3adedff11b61f14c886e35afa036736dcd87a74d27b5c1510225d0f592e213",
  1023: "10108970eeda3eb932baac1428c7a2163b0e924c9a9e25b35bba72b28f70bd11",
  1024: "42214739f095a406f3fc83deb889744ac00df831c10daa55189b5d121c855af7",
  1025: "d00278ae47eb27b34faecf67b4fe263f82d5412916c1ffd97c8cb7fb814b8444",
  2048: "e776b6028c7cd22a4d0ba182a8bf62205d2ef576467e838ed6f2529b85fba24a",
  3072: "b98cb0ff3623be03326b373de6b9095218513e64f1ee2edd2525c7ad1e5cffd2",
};

/**
 * A stand-in for the real dedicated worker: it answers on a MACROTASK, the way
 * a `postMessage` round-trip does, running the worker module's own handler.
 * That is what makes the "the caller's frame is free" assertion meaningful —
 * an implementation that hashed in the calling frame could not satisfy it.
 */
class FakeWorker implements HashWorker {
  onmessage: ((event: { data: HashReply }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  terminated = false;
  readonly seen: number[] = [];

  postMessage(message: HashRequest): void {
    this.seen.push(message.part.size);
    setTimeout(() => {
      if (this.terminated) return;
      void answer(message).then((reply) => this.onmessage?.({ data: reply }));
    }, 0);
  }

  terminate(): void {
    this.terminated = true;
  }
}

/** A worker whose chunk never loads: the browser fires `error` on it and it
 *  answers nothing, ever. */
class DeadWorker implements HashWorker {
  onmessage: ((event: { data: HashReply }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;

  constructor() {
    setTimeout(() => this.onerror?.({ message: "Failed to fetch the worker module" }), 0);
  }

  postMessage(): void {
    // A worker that never loaded never answers.
  }

  terminate(): void {}
}

describe("the digest the pool hands back", () => {
  it.each(Object.keys(VECTORS).map(Number))(
    "matches the reference BLAKE3 of %i bytes through a worker",
    async (length) => {
      const digest = createWorkerDigest(() => new FakeWorker());
      await expect(digest(new Blob([pattern(length)]))).resolves.toBe(VECTORS[length]);
    },
  );

  it("agrees with the in-thread hasher on a part-sized buffer", async () => {
    const bytes = pattern(5_000);
    const digest = createWorkerDigest(() => new FakeWorker());

    await expect(digest(new Blob([bytes]))).resolves.toBe(blake3Hex(bytes.buffer));
  });

  it("keeps the caller's frame free while a big part hashes", async () => {
    // 64 MiB — a real part. The pin is ORDER, not duration: a timer queued
    // before the call must run BEFORE the digest lands. Hashing in the calling
    // frame (`async (bytes) => blake3Hex(bytes)`) resolves on a microtask and
    // beats every timer, so it fails this.
    const part = new Blob([new ArrayBuffer(64 * 1024 * 1024)]);
    const digest = createWorkerDigest(() => new FakeWorker());
    const order: string[] = [];

    setTimeout(() => order.push("timer"), 0);
    await digest(part).then(() => order.push("digest"));

    expect(order).toEqual(["timer", "digest"]);
  });

  it("hands parts to a bounded pool, reusing its workers", async () => {
    const built: FakeWorker[] = [];
    const digest = createWorkerDigest(() => {
      const worker = new FakeWorker();
      built.push(worker);
      return worker;
    }, 2);

    const digests = await Promise.all(
      [1024, 2048, 3072, 1023].map((size) => digest(new Blob([pattern(size)]))),
    );

    expect(digests).toEqual([VECTORS[1024], VECTORS[2048], VECTORS[3072], VECTORS[1023]]);
    expect(built).toHaveLength(2);
    // Four parts over two workers: the queue drained through them, it did not
    // spawn one per part.
    expect(built.reduce((total, worker) => total + worker.seen.length, 0)).toBe(4);
  });
});

describe("when the workers are not there", () => {
  it("falls back in-thread when the runtime has no Worker at all", async () => {
    const digest = createWorkerDigest(() => {
      throw new Error("this runtime has no Worker");
    });

    await expect(digest(new Blob([pattern(1024)]))).resolves.toBe(VECTORS[1024]);
  });

  it("falls back in-thread when the worker module fails to load", async () => {
    const digest = createWorkerDigest(() => new DeadWorker());

    await expect(digest(new Blob([pattern(2048)]))).resolves.toBe(VECTORS[2048]);
  });

  it("does not strand a part queued behind the worker that died", async () => {
    const digest = createWorkerDigest(() => new DeadWorker(), 1);

    const both = await Promise.all([digest(new Blob([pattern(1024)])), digest(new Blob([pattern(3072)]))]);

    expect(both).toEqual([VECTORS[1024], VECTORS[3072]]);
  });
});

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** The session API on the one point at issue: a part whose `X-Part-Checksum` is
 *  not the BLAKE3 of its bytes is refused, exactly as the store refuses it. */
function verifyingServer(options: { partSize: number; total: number }): {
  impl: ReturnType<typeof vi.fn>;
  accepted: Map<number, string>;
  refused: number[];
} {
  const accepted = new Map<number, string>();
  const refused: number[] = [];
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const headers = new Headers(init?.headers);

    if (method === "POST" && url.endsWith("/api/v1/files/uploads")) {
      return json(201, {
        uploadId: SESSION,
        partSize: options.partSize,
        partsTotal: options.total,
        expiresAt: "2026-01-01T00:00:00+00:00",
      });
    }
    const part = /\/parts\/(\d+)$/.exec(url);
    if (method === "PUT" && part) {
      const partNo = Number(part[1]);
      const sent = headers.get("X-Part-Checksum") ?? "";
      // The body is the file's own slice; the store hashes the bytes it reads
      // off the wire, so this reads them the same way.
      const body = await (init?.body as Blob).arrayBuffer();
      if (sent !== blake3Hex(body)) {
        refused.push(partNo);
        return json(422, {
          code: "files.part_checksum_mismatch",
          message: `part ${partNo} does not match the checksum it declared`,
        });
      }
      accepted.set(partNo, sent);
      return json(200, { partNo, size: body.byteLength, duplicate: false });
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

function corpusFile(bytes: number): File {
  const file = new File([pattern(bytes)], "corpus.bin", { type: "application/octet-stream" });
  Object.defineProperty(file, "lastModified", { value: 1_700_000_000_000 });
  return file;
}

describe("an upload whose parts are hashed off-thread", () => {
  it("sends checksums the store accepts for every part", async () => {
    const built: FakeWorker[] = [];
    const server = verifyingServer({ partSize: 1024, total: 3 });
    const client = new UploadClient({
      fetchImpl: server.impl as unknown as typeof fetch,
      storage: noStorage,
      workerFactory: () => {
        const worker = new FakeWorker();
        built.push(worker);
        return worker;
      },
    });

    const done = await (await client.start(corpusFile(3072), PARENT)).done();

    expect(server.refused).toEqual([]);
    expect([...server.accepted.keys()].sort()).toEqual([1, 2, 3]);
    expect(done.state).toBe("done");
    // The parts really went through a worker rather than the fallback.
    expect(built.length).toBeGreaterThan(0);
    expect(built.reduce((total, worker) => total + worker.seen.length, 0)).toBeGreaterThanOrEqual(
      3,
    );
  });

  it("still completes when every worker fails to load", async () => {
    const server = verifyingServer({ partSize: 1024, total: 2 });
    const client = new UploadClient({
      fetchImpl: server.impl as unknown as typeof fetch,
      storage: noStorage,
      workerFactory: () => new DeadWorker(),
    });

    const done = await (await client.start(corpusFile(2048), PARENT)).done();

    expect(server.refused).toEqual([]);
    expect([...server.accepted.keys()].sort()).toEqual([1, 2]);
    expect(done.state).toBe("done");
  });
});
