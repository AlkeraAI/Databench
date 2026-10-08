// The machinery a cancel runs on, and the pool's slots underneath it.
//
// The upload client's "never hangs" property rests on several small mechanisms
// that are each invisible from the row: the pool gives a terminated worker's
// slot back, a per-file read failure does not kill the pool, the operation poll
// notices an abort, the backoff is cancellable, and the signal actually reaches
// `fetch`. Every one of them can be deleted with the row-level suite still
// green — and every one of them, deleted, reintroduces the hang or the spin the
// client exists to avoid. `pageDigest()` is a module singleton shared by the
// whole tab, so a pool that leaks its slots does not fail one upload: it fails
// every upload made for the rest of the session.

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { blake3Hex, blake3HexOfBlob } from "@/api/blake3";
import { answer, type HashReply, type HashRequest } from "@/api/blake3.worker";
import {
  createWorkerDigest,
  UploadClient,
  type HashWorker,
  type UploadProgress,
} from "@/api/filesUpload";

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

function notReadable(): DOMException {
  return new DOMException("The requested file could not be read", "NotReadableError");
}

/** Bytes that cannot be read, however they are sliced. */
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

function file(name: string, bytes: number): File {
  const made = new File([pattern(bytes)], name, { type: "application/octet-stream" });
  Object.defineProperty(made, "lastModified", { value: 1_700_000_000_000 });
  return made;
}

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

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

async function until(condition: () => boolean, ms = 2000): Promise<void> {
  const started = Date.now();
  while (!condition()) {
    if (Date.now() - started > ms) throw new Error("the run never got there");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function pause(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const noStorage = {
  getItem: (): string | null => null,
  setItem: (): void => {},
  removeItem: (): void => {},
};

/** Answers on a macrotask by running the worker module's own handler, and keeps
 *  the sizes it was asked for so a test can see WHICH machine did the hashing. */
class FakeWorker implements HashWorker {
  onmessage: ((event: { data: HashReply }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onmessageerror: ((event: unknown) => void) | null = null;
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

/** Accepts a part and never answers — a hasher that has hung. */
class SilentWorker implements HashWorker {
  onmessage: ((event: { data: HashReply }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onmessageerror: ((event: unknown) => void) | null = null;
  terminated = false;
  handed = 0;

  postMessage(): void {
    this.handed += 1;
  }

  terminate(): void {
    this.terminated = true;
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

describe("the hash pool's slots", () => {
  it("gives a cancelled part's worker slot back, so later parts still hash off-thread", async () => {
    // `pageDigest()` is one pool for the whole tab. A slot the pool forgets to
    // reclaim is not one lost upload: `pump()` then sees a full pool of
    // terminated workers and EVERY later part waits forever.
    const built: HashWorker[] = [];
    const digest = createWorkerDigest(() => {
      const worker = built.length < 5 ? new SilentWorker() : new FakeWorker();
      built.push(worker);
      return worker;
    }, 2);

    for (let hung = 0; hung < 5; hung += 1) {
      const aborter = new AbortController();
      const hashing = digest(new Blob([pattern(PART_SIZE)]), aborter.signal);
      await until(() => built.length === hung + 1);
      aborter.abort();
      await expect(settles(hashing)).rejects.toMatchObject({ name: "AbortError" });
    }

    const bytes = pattern(PART_SIZE);
    await expect(settles(digest(new Blob([bytes])))).resolves.toBe(blake3Hex(bytes.buffer));
    // Six parts, six workers: each abort really handed the slot back. Without
    // that the pool stops at its size and the sixth part never starts.
    expect(built).toHaveLength(6);
    expect(built.slice(0, 5).every((worker) => (worker as SilentWorker).terminated)).toBe(true);
  });

  it("keeps the worker that could not read one part, rather than losing the pool", async () => {
    // A worker's `{error}` is what a `NotReadableError` inside it produces, and
    // an unreadable file is the ordinary case this client was built for. Taking
    // the pool down for it drops the whole tab to main-thread hashing for the
    // session — ~1 s per 32 MiB, the exact cost the pool exists to avoid.
    const built: FakeWorker[] = [];
    const digest = createWorkerDigest(() => {
      const worker = new FakeWorker();
      built.push(worker);
      return worker;
    }, 1);

    await expect(settles(digest(unreadableBlob(PART_SIZE)))).rejects.toMatchObject({
      name: "NotReadableError",
    });
    const after = pattern(2048);
    await expect(settles(digest(new Blob([after])))).resolves.toBe(blake3Hex(after.buffer));

    expect(built).toHaveLength(1);
    expect(built[0]?.terminated).toBe(false);
    // The part after the unreadable one really went through the worker: two
    // sizes handed over, not one followed by a silent in-thread fallback.
    expect(built[0]?.seen).toEqual([PART_SIZE, 2048]);
  });

  it("still gives up the pool when the worker fails as a worker", async () => {
    // The distinction being drawn: `{error}` is about a part, `error` is about
    // the worker. The second one must still collapse.
    class BrokenWorker implements HashWorker {
      onmessage: ((event: { data: HashReply }) => void) | null = null;
      onerror: ((event: unknown) => void) | null = null;
      onmessageerror: ((event: unknown) => void) | null = null;
      terminated = false;
      constructor() {
        setTimeout(() => this.onerror?.({ message: "chunk did not load" }), 0);
      }
      postMessage(): void {}
      terminate(): void {
        this.terminated = true;
      }
    }
    const built: BrokenWorker[] = [];
    const bytes = pattern(PART_SIZE);
    const digest = createWorkerDigest(() => {
      const worker = new BrokenWorker();
      built.push(worker);
      return worker;
    }, 1);

    await expect(settles(digest(new Blob([bytes])))).resolves.toBe(blake3Hex(bytes.buffer));
    await expect(settles(digest(new Blob([bytes])))).resolves.toBe(blake3Hex(bytes.buffer));

    expect(built).toHaveLength(1);
    expect(built[0]?.terminated).toBe(true);
  });
});

describe("the in-thread hasher under a cancel", () => {
  it("stops between two windows rather than reading the rest of the part", async () => {
    const bytes = pattern(256);
    const source = new Blob([bytes]);
    const reads: number[] = [];
    const aborter = new AbortController();
    const watched = {
      size: bytes.length,
      slice: (from: number, to: number): Blob => {
        reads.push(from);
        // The cancel lands after the first window has been handed over.
        if (reads.length === 1) aborter.abort();
        return source.slice(from, to);
      },
    } as unknown as Blob;

    await expect(settles(blake3HexOfBlob(watched, 32, aborter.signal))).rejects.toMatchObject({
      name: "AbortError",
    });
    // Eight windows would have been read to the end; it stopped at the first.
    expect(reads).toEqual([0]);
  });
});

/** The session API plus the knobs each case needs: a part that hangs until it is
 *  released, a part the server sheds, and an operation that never settles. */
function server(options: {
  total: number;
  shedParts?: number;
  stuckOperation?: boolean;
  holdPart?: number;
}): {
  impl: typeof fetch;
  accepted: Map<number, string>;
  puts: number[];
  deletes: string[];
  operationReads: number;
  signals: Map<string, AbortSignal | null>;
  releaseHeld: () => void;
} {
  const accepted = new Map<number, string>();
  const puts: number[] = [];
  const deletes: string[] = [];
  const signals = new Map<string, AbortSignal | null>();
  let shedLeft = options.shedParts ?? 0;
  const state = { operationReads: 0 };
  let release = (): void => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });

  const impl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const method = init?.method ?? "GET";
    signals.set(`${method} ${url.replace(/^.*\/api/, "/api")}`, init?.signal ?? null);

    if (method === "POST" && url.endsWith("/api/v1/files/uploads")) {
      return json(201, {
        uploadId: "session-1",
        partSize: PART_SIZE,
        partsTotal: options.total,
        expiresAt: "2026-01-01T00:00:00+00:00",
      });
    }
    const part = /\/parts\/(\d+)$/.exec(url);
    if (method === "PUT" && part) {
      const partNo = Number(part[1]);
      puts.push(partNo);
      if (shedLeft > 0) {
        shedLeft -= 1;
        return json(503, { code: "files.busy", message: "no room" }, { "retry-after": "0" });
      }
      if (options.holdPart === partNo) {
        // A real upload in flight: it lands only if nothing aborts it, and the
        // signal the client attached is the only thing that can.
        await Promise.race([
          held,
          new Promise<never>((_, reject) => {
            init?.signal?.addEventListener(
              "abort",
              () => reject(new DOMException("aborted", "AbortError")),
              { once: true },
            );
          }),
        ]);
      }
      const body = await (init?.body as Blob).arrayBuffer();
      accepted.set(partNo, blake3Hex(body));
      return json(200, { partNo, size: body.byteLength, duplicate: false });
    }
    if (method === "GET" && url.endsWith("/uploads/session-1")) {
      const done = [...accepted.keys()].sort((a, b) => a - b);
      return json(200, {
        uploadId: "session-1",
        state: "open",
        offset: done.length * PART_SIZE,
        length: 0,
        complete: false,
        partsDone: done.length,
        partsTotal: options.total,
        acceptedParts: done,
      });
    }
    if (method === "DELETE" && url.endsWith("/uploads/session-1")) {
      deletes.push("session-1");
      return new Response(null, { status: 204 });
    }
    if (method === "POST" && url.endsWith("/complete")) {
      return options.stuckOperation
        ? json(202, { id: "op-1", state: "queued" })
        : json(200, { item: { id: "node-1", etag: "v1" }, unchanged: false });
    }
    if (method === "GET" && url.includes("/operations/op-1")) {
      state.operationReads += 1;
      // A backstop so a client that spins cannot spin without end here: past
      // this the operation settles, and the count is what the test reads.
      return json(200, { id: "op-1", state: state.operationReads > 200 ? "done" : "running" });
    }
    return json(404, { code: "not_found", message: url });
  };
  return {
    impl: impl as unknown as typeof fetch,
    accepted,
    puts,
    deletes,
    signals,
    get operationReads() {
      return state.operationReads;
    },
    releaseHeld: () => release(),
  };
}

describe("a cancel reaching the work it cancels", () => {
  it("aborts the part in flight, so the bytes never land", async () => {
    const fake = server({ total: 2, holdPart: 1 });
    const client = new UploadClient({
      fetchImpl: fake.impl,
      storage: noStorage,
      digest: (part) => blake3HexOfBlob(part),
    });

    const handle = await client.start(file("big.bin", 2048), PARENT);
    await until(() => fake.puts.length > 0);
    await settles(handle.cancel());
    fake.releaseHeld();
    const settled = await settles(handle.done());
    await pause(50);

    expect(settled.state).toBe("cancelled");
    // The PUT was refused by the signal rather than allowed to complete.
    expect([...fake.accepted.keys()]).toEqual([]);
    // The DELETE is the one request a cancel exists to make, so it is never
    // aborted along with the rest.
    expect(fake.signals.get("PUT /api/v1/files/uploads/session-1/parts/1")).toBeInstanceOf(
      AbortSignal,
    );
    expect(fake.signals.get("DELETE /api/v1/files/uploads/session-1")).toBeNull();
    expect(escaped).toEqual([]);
  });

  it("ends the backoff instead of sending the wait out", async () => {
    // A shed part is retried after a wait. Cancelled mid-wait, the client must
    // not wake up later and send a part against a session it has already given
    // back — the row is gone, so the only evidence is the request.
    const fake = server({ total: 1, shedParts: 3 });
    const client = new UploadClient({
      fetchImpl: fake.impl,
      storage: noStorage,
      digest: (part) => blake3HexOfBlob(part),
      onProgress: () => {},
    });

    const handle = await client.start(file("shed.bin", 1024), PARENT);
    await until(() => fake.puts.length === 1);
    await settles(handle.cancel());
    // Comfortably past the first backoff (500 ms), where the next try would be.
    await pause(900);

    expect(fake.puts).toEqual([1]);
    expect((await settles(handle.done())).state).toBe("cancelled");
    expect(escaped).toEqual([]);
  });

  it("stops following a queued commit instead of spinning on it", async () => {
    // `delay` resolves on abort, so a poll that does not re-check the signal
    // becomes a hot loop running to the ten-minute backstop with no wait at all.
    const fake = server({ total: 1, stuckOperation: true });
    const client = new UploadClient({
      driveId: "44444444-4444-4444-4444-444444444444",
      fetchImpl: fake.impl,
      storage: noStorage,
      digest: (part) => blake3HexOfBlob(part),
    });

    const handle = await client.start(file("queued.bin", 1024), PARENT);
    await until(() => fake.operationReads > 0);
    await settles(handle.cancel());
    await pause(300);

    expect((await settles(handle.done())).state).toBe("cancelled");
    // A handful of polls at the client's own widening intervals, not a spin.
    expect(fake.operationReads).toBeLessThan(20);
    expect(escaped).toEqual([]);
  });
});

describe("a row that has already settled", () => {
  it("keeps saying Uploaded when Cancel arrives a frame late", async () => {
    const fake = server({ total: 1 });
    const states: UploadProgress["state"][] = [];
    const client = new UploadClient({
      fetchImpl: fake.impl,
      storage: noStorage,
      digest: (part) => blake3HexOfBlob(part),
      onProgress: (progress) => states.push(progress.state),
    });

    const handle = await client.start(file("done.bin", 1024), PARENT);
    expect((await settles(handle.done())).state).toBe("done");
    await settles(handle.cancel());
    await settles(handle.resume());

    expect(states.at(-1)).toBe("done");
    expect(states).not.toContain("cancelled");
    // Nothing is released either: the session is long gone, and a DELETE for it
    // is a refusal this row would have to explain.
    expect(fake.deletes).toEqual([]);
  });

  it("keeps a failed row's sentence and its Retry", async () => {
    // Cancel is reachable on a failed row for the frame between the failure and
    // the re-render that hides the button. Repainting it "cancelled" takes away
    // the only place the person is told the file is not in Files.
    const fake = server({ total: 1 });
    const bad: typeof fetch = (input, init) =>
      String(input).includes("/parts/")
        ? Promise.resolve(json(422, { code: "files.part_mismatch", message: "that part is wrong" }))
        : fake.impl(input, init);
    const states: UploadProgress["state"][] = [];
    const client = new UploadClient({
      fetchImpl: bad,
      storage: noStorage,
      digest: (part) => blake3HexOfBlob(part),
      onProgress: (progress) => states.push(progress.state),
    });

    const handle = await client.start(file("bad.bin", 1024), PARENT);
    const failed = await settles(handle.done());
    await settles(handle.cancel());

    expect(failed.state).toBe("failed");
    expect(states.at(-1)).toBe("failed");
    expect(failed.error?.message).toBe("that part is wrong");
    expect(fake.deletes).toEqual([]);
  });
});

describe("a name collision nobody answers", () => {
  it("fails the row when the page refuses the question", async () => {
    // `onConflict` is a promise the PAGE owns: the tray unmounting, a torn-down
    // prompt, a host that throws. The upload is parked inside `await` on it, so
    // a rejection that nothing catches is a row that never settles and an
    // unhandled rejection with it.
    const fake = server({ total: 1 });
    const taken: typeof fetch = (input, init) =>
      String(input).endsWith("/complete")
        ? Promise.resolve(json(409, { code: "files.exists", message: "that name is used" }))
        : fake.impl(input, init);
    const client = new UploadClient({
      fetchImpl: taken,
      storage: noStorage,
      digest: (part) => blake3HexOfBlob(part),
      onConflict: () =>
        Promise.reject(new Error("the upload was abandoned before the name was settled")),
    });

    const settled = await settles((await client.start(file("taken.bin", 1024), PARENT)).done());
    await pause(50);

    expect(settled.state).toBe("failed");
    expect(escaped).toEqual([]);
  });

  it("fails the row when answering it throws outright", async () => {
    const fake = server({ total: 1 });
    const taken: typeof fetch = (input, init) =>
      String(input).endsWith("/complete")
        ? Promise.resolve(json(409, { code: "files.exists", message: "that name is used" }))
        : fake.impl(input, init);
    const client = new UploadClient({
      fetchImpl: taken,
      storage: noStorage,
      digest: (part) => blake3HexOfBlob(part),
      onConflict: () => {
        throw new Error("the prompt is gone");
      },
    });

    const settled = await settles((await client.start(file("taken.bin", 1024), PARENT)).done());
    await pause(50);

    expect(settled.state).toBe("failed");
    expect(escaped).toEqual([]);
  });
});

describe("resuming an upload", () => {
  it("runs one pump however many times Resume is pressed", async () => {
    // Two pumps on one session send the same parts twice and settle the row
    // twice — the tray would count one file as two.
    const fake = server({ total: 3 });
    const states: UploadProgress["state"][] = [];
    let handle: { pause(): void } | null = null;
    let pausedOnce = false;
    const client = new UploadClient({
      fetchImpl: fake.impl,
      storage: noStorage,
      digest: (part) => blake3HexOfBlob(part),
      onProgress: (progress) => {
        states.push(progress.state);
        if (!pausedOnce && progress.partsDone === 1 && progress.state === "uploading") {
          pausedOnce = true;
          handle?.pause();
        }
      },
    });

    const started = await client.start(file("resume.bin", 3072), PARENT);
    handle = started;
    await until(() => states.includes("paused"));
    await settles(started.resume());
    await settles(started.resume());
    const settled = await settles(started.done(), 3000);
    await pause(100);

    expect(settled.state).toBe("done");
    // One completion, and one "done" on the row: a second pump would have
    // produced both a second time.
    expect(states.filter((state) => state === "done")).toHaveLength(1);
    expect([...fake.accepted.keys()].sort((a, b) => a - b)).toEqual([1, 2, 3]);
    expect(escaped).toEqual([]);
  });
});
