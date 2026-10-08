// A file that stops being readable mid-upload must end as a row, not as a hang.
//
// The client reads the file twice per part — once to hash, once to send — off a
// `File` that is a HANDLE, not a copy. Between the drop and the last part the
// file can be moved, renamed, deleted, or sitting on a drive that was unplugged,
// and every read of it then answers `NotReadableError`. Over the multi-hour
// upload this client exists for, that is an ordinary event.
//
// What has to be true, and is pinned here:
//
//  * the digest REJECTS rather than never settling — a promise with no reject
//    path is a row frozen at its last percentage for the life of the tab;
//  * the row says what happened in words the person can act on, and stays
//    retryable (the same `File` may read again once the drive is back);
//  * the queue slot is handed on, so the files behind it in the same drop still
//    upload;
//  * nothing escapes to `unhandledrejection`;
//  * `cancel()` resolves even when the hash is on a worker that has stopped
//    answering and even when the server refuses the DELETE.

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { blake3Hex } from "@/api/blake3";
import { answer, type HashReply, type HashRequest } from "@/api/blake3.worker";
import {
  createWorkerDigest,
  UploadClient,
  UploadReadError,
  type HashWorker,
  type UploadProgress,
} from "@/api/filesUpload";
import { runPool } from "@/pages/workspace/files/dragDrop";
import { filesErrorCopy } from "@/lib/files/errors";
import { stopsBatch } from "@/pages/workspace/files/useUploads";

const PARENT = "33333333-3333-3333-3333-333333333333";
const PART_SIZE = 1024;

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

function pattern(length: number): Uint8Array<ArrayBuffer> {
  const bytes = new Uint8Array(new ArrayBuffer(length));
  for (let i = 0; i < length; i += 1) bytes[i] = i % 251;
  return bytes;
}

/** What the platform throws when the bytes behind a `File` are gone. */
function notReadable(): DOMException {
  return new DOMException("The requested file could not be read", "NotReadableError");
}

/** A Blob-shaped handle onto bytes that cannot be read — the state a slice of a
 *  moved or deleted file is in. Slicing it further (which is how the hasher
 *  walks a part, one window at a time) stays unreadable. */
function unreadableBlob(size: number): Blob {
  const blob = {
    size,
    type: "application/octet-stream",
    slice: (from = 0, to = size): Blob => unreadableBlob(Math.max(0, to - from)),
    arrayBuffer: (): Promise<ArrayBuffer> => Promise.reject(notReadable()),
    text: (): Promise<string> => Promise.reject(notReadable()),
  };
  return blob as unknown as Blob;
}

/** A real file that reads fine until `badPart`, and from there answers every
 *  read with `NotReadableError` — a drive unplugged partway through. */
function unreadableFrom(name: string, bytes: number, badPart: number): File {
  const file = new File([pattern(bytes)], name, { type: "application/octet-stream" });
  Object.defineProperty(file, "lastModified", { value: 1_700_000_000_000 });
  const whole = File.prototype.slice.bind(file);
  Object.defineProperty(file, "slice", {
    value: (start = 0, end = bytes): Blob => {
      const partNo = Math.floor(start / PART_SIZE) + 1;
      return partNo >= badPart ? unreadableBlob(Math.max(0, end - start)) : whole(start, end);
    },
  });
  return file;
}

function readableFile(name: string, bytes: number): File {
  const file = new File([pattern(bytes)], name, { type: "application/octet-stream" });
  Object.defineProperty(file, "lastModified", { value: 1_700_000_000_000 });
  return file;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

interface Session {
  accepted: Map<number, string>;
}

/** The session API, enough of it: one session per open, parts verified against
 *  the checksum they declare, a synchronous completion. */
function server(options: { total: number; deleteStatus?: number }): {
  impl: typeof fetch;
  sessions: Map<string, Session>;
  deletes: string[];
} {
  const sessions = new Map<string, Session>();
  const deletes: string[] = [];
  let next = 0;
  const impl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const method = init?.method ?? "GET";

    if (method === "POST" && url.endsWith("/api/v1/files/uploads")) {
      next += 1;
      const uploadId = `session-${next}`;
      sessions.set(uploadId, { accepted: new Map() });
      return json(201, {
        uploadId,
        partSize: PART_SIZE,
        partsTotal: options.total,
        expiresAt: "2026-01-01T00:00:00+00:00",
      });
    }
    const part = /\/uploads\/([^/]+)\/parts\/(\d+)$/.exec(url);
    if (method === "PUT" && part) {
      const session = sessions.get(part[1] ?? "");
      const body = await (init?.body as Blob).arrayBuffer();
      const sent = new Headers(init?.headers).get("X-Part-Checksum") ?? "";
      if (!session || sent !== blake3Hex(body)) {
        return json(422, { code: "files.part_checksum_mismatch", message: "bad part" });
      }
      session.accepted.set(Number(part[2]), sent);
      return json(200, { partNo: Number(part[2]), size: body.byteLength, duplicate: false });
    }
    const read = /\/uploads\/([^/]+)$/.exec(url);
    if (method === "GET" && read) {
      const session = sessions.get(read[1] ?? "");
      if (!session) return json(404, { code: "files.not_found", message: "gone" });
      const done = [...session.accepted.keys()].sort((a, b) => a - b);
      return json(200, {
        uploadId: read[1],
        state: "open",
        offset: done.length * PART_SIZE,
        length: 0,
        complete: false,
        partsDone: done.length,
        partsTotal: options.total,
        acceptedParts: done,
      });
    }
    if (method === "DELETE" && read) {
      deletes.push(read[1] ?? "");
      const status = options.deleteStatus ?? 204;
      return status === 204 ? new Response(null, { status: 204 }) : json(status, { code: "boom" });
    }
    if (method === "POST" && url.endsWith("/complete")) {
      return json(200, { item: { id: "node-1", etag: "v1" }, unchanged: false });
    }
    return json(404, { code: "not_found", message: url });
  };
  return { impl: impl as unknown as typeof fetch, sessions, deletes };
}

const noStorage = {
  getItem: (): string | null => null,
  setItem: (): void => {},
  removeItem: (): void => {},
};

/** A worker that answers on a macrotask by running the worker module's own
 *  handler — which reports `{error}` for a part it cannot read, exactly as the
 *  real one does. */
class FakeWorker implements HashWorker {
  onmessage: ((event: { data: HashReply }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onmessageerror: ((event: unknown) => void) | null = null;
  terminated = false;

  postMessage(message: HashRequest): void {
    setTimeout(() => {
      if (this.terminated) return;
      void answer(message).then((reply) => this.onmessage?.({ data: reply }));
    }, 0);
  }

  terminate(): void {
    this.terminated = true;
  }
}

/** A worker that loaded, accepted the part, and then said nothing ever again —
 *  a hung hasher, the case no timeout in this client covers. */
class SilentWorker implements HashWorker {
  onmessage: ((event: { data: HashReply }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onmessageerror: ((event: unknown) => void) | null = null;
  terminated = false;

  postMessage(): void {}

  terminate(): void {
    this.terminated = true;
  }
}

/** A worker whose module never loaded: it fires `error` and answers nothing. */
class DeadWorker implements HashWorker {
  onmessage: ((event: { data: HashReply }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onmessageerror: ((event: unknown) => void) | null = null;

  constructor() {
    setTimeout(() => this.onerror?.({ message: "Failed to fetch the worker module" }), 0);
  }

  postMessage(): void {}
  terminate(): void {}
}

/** A promise that must settle, said as a failing assertion rather than as the
 *  runner's timeout — the bug under test IS "never settles". */
async function settles<T>(promise: Promise<T>, ms = 2000): Promise<T> {
  const guard = { timer: undefined as ReturnType<typeof setTimeout> | undefined };
  const deadline = new Promise<never>((_, reject) => {
    guard.timer = setTimeout(() => reject(new Error(`still pending after ${ms}ms`)), ms);
  });
  try {
    return await Promise.race([promise, deadline]);
  } finally {
    clearTimeout(guard.timer);
  }
}

/** Let Node decide which rejections nobody handled. */
async function drainMicrotasks(): Promise<void> {
  for (let turn = 0; turn < 4; turn += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

/** Wait for something the run does on its own, rather than guessing at hops. */
async function until(condition: () => boolean, ms = 2000): Promise<void> {
  const started = Date.now();
  while (!condition()) {
    if (Date.now() - started > ms) throw new Error("the run never got there");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

let escaped: unknown[] = [];
const catchEscaped = (reason: unknown): void => {
  escaped.push(reason);
};

beforeEach(() => {
  escaped = [];
  process.on("unhandledRejection", catchEscaped);
});

afterEach(() => {
  process.off("unhandledRejection", catchEscaped);
});

describe("the digest of a part that cannot be read", () => {
  it("rejects instead of never settling when the in-thread fallback cannot read it", async () => {
    // No `Worker` in this runtime at all, so the pool is born broken and every
    // part is hashed in-thread — the path the finding's fallback lands on.
    const digest = createWorkerDigest(() => {
      throw new Error("this runtime has no Worker");
    });

    await expect(settles(digest(unreadableBlob(PART_SIZE)))).rejects.toMatchObject({
      name: "NotReadableError",
    });
  });

  it("rejects when the worker reports it could not read and the fallback cannot either", async () => {
    const digest = createWorkerDigest(() => new FakeWorker(), 1);

    await expect(settles(digest(unreadableBlob(PART_SIZE)))).rejects.toMatchObject({
      name: "NotReadableError",
    });
  });

  it("rejects when the worker module never loaded and the file is gone", async () => {
    const digest = createWorkerDigest(() => new DeadWorker(), 1);

    await expect(settles(digest(unreadableBlob(PART_SIZE)))).rejects.toMatchObject({
      name: "NotReadableError",
    });
  });

  it("keeps serving the parts beside the one it could not read", async () => {
    // One pool, two parts, and in a real drop they belong to two different
    // files: whatever the unreadable one does to the pool, the readable one
    // still has to come back with its digest.
    const digest = createWorkerDigest(() => new FakeWorker(), 1);
    const good = pattern(PART_SIZE);

    const [bad, hex] = await settles(
      Promise.all([
        digest(unreadableBlob(PART_SIZE)).then(
          () => "resolved",
          () => "rejected",
        ),
        digest(new Blob([good])),
      ]),
    );

    expect(bad).toBe("rejected");
    expect(hex).toBe(blake3Hex(good.buffer));
  });

  it("rejects an aborted hash and lets go of the worker holding it", async () => {
    const built: SilentWorker[] = [];
    const digest = createWorkerDigest(() => {
      const worker = new SilentWorker();
      built.push(worker);
      return worker;
    }, 1);
    const aborter = new AbortController();

    const hashing = digest(new Blob([pattern(PART_SIZE)]), aborter.signal);
    aborter.abort();

    await expect(settles(hashing)).rejects.toMatchObject({ name: "AbortError" });
    expect(built[0]?.terminated).toBe(true);
  });
});

describe("an upload whose file stops being readable", () => {
  it("fails the row with the file's name and what to do about it", async () => {
    const fake = server({ total: 3 });
    const seen: UploadProgress[] = [];
    const client = new UploadClient({
      fetchImpl: fake.impl,
      storage: noStorage,
      workerFactory: () => new FakeWorker(),
      onProgress: (progress) => seen.push({ ...progress }),
    });

    const handle = await client.start(unreadableFrom("quarterly.csv", 3072, 2), PARENT);
    const settled = await settles(handle.done());
    await drainMicrotasks();

    expect(settled.state).toBe("failed");
    expect(settled.error).toBeInstanceOf(UploadReadError);
    expect(settled.error?.message).toBe(
      "quarterly.csv could not be read. It may have been moved, deleted, or on a drive that is no longer connected.",
    );
    // Part one was readable and really went; the run stopped at the part that
    // was not, rather than at the end of the file.
    expect([...(fake.sessions.get("session-1")?.accepted.keys() ?? [])]).toEqual([1]);
    expect(seen.at(-1)?.state).toBe("failed");
    expect(escaped).toEqual([]);
  });

  it("leaves the failure retryable rather than retrying the read itself", async () => {
    // The refusal is about the file, not about the moment, so the client must
    // not spend its eight part-retries on it — a `File` that reads again does so
    // because the person plugged the drive back in, which is the row's Retry.
    const fake = server({ total: 2 });
    const requests: string[] = [];
    const counting: typeof fetch = (input, init) => {
      requests.push(`${init?.method ?? "GET"} ${String(input)}`);
      return fake.impl(input, init);
    };
    const client = new UploadClient({
      fetchImpl: counting,
      storage: noStorage,
      workerFactory: () => new FakeWorker(),
    });

    const settled = await settles(
      (await client.start(unreadableFrom("gone.bin", 2048, 1), PARENT)).done(),
    );

    expect(settled.state).toBe("failed");
    expect(requests.filter((line) => line.includes("/parts/"))).toEqual([]);
    expect(escaped).toEqual([]);
  });

  it("hands its queue slot on, so the files behind it in the drop still land", async () => {
    const fake = server({ total: 2 });
    const client = new UploadClient({
      fetchImpl: fake.impl,
      storage: noStorage,
      workerFactory: () => new FakeWorker(),
    });
    const dropped = [
      unreadableFrom("unplugged.bin", 2048, 1),
      readableFile("kept-a.bin", 2048),
      readableFile("kept-b.bin", 2048),
    ];
    const outcomes = new Map<string, UploadProgress>();

    await settles(
      runPool(dropped, 1, async (file) => {
        const handle = await client.start(file, PARENT);
        outcomes.set(file.name, await handle.done());
      }),
      3000,
    );
    await drainMicrotasks();

    expect(outcomes.get("unplugged.bin")?.state).toBe("failed");
    expect(outcomes.get("kept-a.bin")?.state).toBe("done");
    expect(outcomes.get("kept-b.bin")?.state).toBe("done");
    expect(escaped).toEqual([]);
  });
});

describe("cancelling an upload", () => {
  it("resolves while the hash is on a worker that has stopped answering", async () => {
    const built: SilentWorker[] = [];
    const fake = server({ total: 2 });
    const client = new UploadClient({
      fetchImpl: fake.impl,
      storage: noStorage,
      workerFactory: () => {
        const worker = new SilentWorker();
        built.push(worker);
        return worker;
      },
    });

    const handle = await client.start(readableFile("big.bin", 2048), PARENT);
    // Let the run reach the hash of part one, where it will now sit forever.
    await until(() => built.length > 0);

    await settles(handle.cancel());
    const settled = await settles(handle.done());
    await drainMicrotasks();

    expect(settled.state).toBe("cancelled");
    expect(fake.deletes).toEqual(["session-1"]);
    // The hung worker is let go rather than held for the life of the tab.
    expect(built[0]?.terminated).toBe(true);
    expect(escaped).toEqual([]);
  });

  it("resolves even when the server refuses to release the session", async () => {
    const fake = server({ total: 2, deleteStatus: 500 });
    const client = new UploadClient({
      fetchImpl: fake.impl,
      storage: noStorage,
      workerFactory: () => new SilentWorker(),
    });

    const handle = await client.start(readableFile("big.bin", 2048), PARENT);
    await drainMicrotasks();

    await expect(settles(handle.cancel())).resolves.toBeUndefined();
    expect((await settles(handle.done())).state).toBe("cancelled");
    await drainMicrotasks();
    expect(escaped).toEqual([]);
  });

  it("does not turn a cancelled row back into a failed one", async () => {
    const fake = server({ total: 2 });
    const states: string[] = [];
    const client = new UploadClient({
      fetchImpl: fake.impl,
      storage: noStorage,
      workerFactory: () => new SilentWorker(),
      onProgress: (progress) => states.push(progress.state),
    });

    const handle = await client.start(readableFile("big.bin", 2048), PARENT);
    await drainMicrotasks();
    await settles(handle.cancel());
    await drainMicrotasks();

    expect(states.at(-1)).toBe("cancelled");
    expect(states).not.toContain("failed");
    expect(escaped).toEqual([]);
  });
});

describe("a worker that answers with something that is not a digest", () => {
  it("settles the part in-thread when the reply cannot be deserialised", async () => {
    // `messageerror` is a separate handler from `error`; a worker that only
    // wired the latter never answers the part this one belongs to.
    class GarbledWorker implements HashWorker {
      onmessage: ((event: { data: HashReply }) => void) | null = null;
      onerror: ((event: unknown) => void) | null = null;
      onmessageerror: ((event: unknown) => void) | null = null;
      terminate(): void {}
      postMessage(): void {
        setTimeout(() => this.onmessageerror?.({ type: "messageerror" }), 0);
      }
    }
    const bytes = pattern(PART_SIZE);
    const digest = createWorkerDigest(() => new GarbledWorker(), 1);

    await expect(settles(digest(new Blob([bytes])))).resolves.toBe(blake3Hex(bytes.buffer));
  });

  it("still rejects when the file behind that part is gone too", async () => {
    class GarbledWorker implements HashWorker {
      onmessage: ((event: { data: HashReply }) => void) | null = null;
      onerror: ((event: unknown) => void) | null = null;
      onmessageerror: ((event: unknown) => void) | null = null;
      terminate(): void {}
      postMessage(): void {
        setTimeout(() => this.onmessageerror?.({ type: "messageerror" }), 0);
      }
    }
    const digest = createWorkerDigest(() => new GarbledWorker(), 1);

    await expect(settles(digest(unreadableBlob(PART_SIZE)))).rejects.toMatchObject({
      name: "NotReadableError",
    });
  });
});

describe("the read failure the tray shows", () => {
  it.each([
    [
      "report.pdf",
      "report.pdf could not be read. It may have been moved, deleted, or on a drive that is no longer connected.",
    ],
    [
      "a b.csv",
      "a b.csv could not be read. It may have been moved, deleted, or on a drive that is no longer connected.",
    ],
  ])("names %s in a sentence the reader can act on", (name, expected) => {
    expect(new UploadReadError(name).message).toBe(expected);
  });

  it("does not stop the rest of the drop", () => {
    expect(stopsBatch(new UploadReadError("x.bin"))).toBe(false);
  });

  it("is copy from the one table, and one the person may try again", () => {
    // The browser raises this one, so the code stays out of the `files.`
    // namespace the server spends — and it still resolves to real copy rather
    // than falling through to the "retryable only if 5xx" default, which would
    // call a file that is merely unplugged permanently refused.
    const copy = filesErrorCopy(new UploadReadError("x.bin"));

    expect(copy.code).toBe("upload.unreadable");
    expect(copy.title).toBe("That file could not be read.");
    expect(copy.retryable).toBe(true);
  });

  it("keeps what the platform said as the cause", () => {
    const cause = notReadable();

    expect(new UploadReadError("x.bin", cause).cause).toBe(cause);
  });
});
