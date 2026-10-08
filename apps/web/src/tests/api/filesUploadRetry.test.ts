// A part the server has no room for right now is a wait, not a failure.
//
// The process bounds the bytes it will hold in flight and sheds what does not
// fit: `503` + `Retry-After`, from `apps/backend/backend/api/body_limit.py`.
// The budget admits only a couple of max-size parts per process, so a third
// person uploading while two others are mid-part is the ORDINARY case, not an
// outage — and a client that gives up on the first 503 turns it into a failed
// file that would have landed by waiting a second.
//
// Every assertion here is on the requests the client made and when it made
// them; nothing reads a stub echoing its own input.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { PART_RETRY_ATTEMPTS, UploadClient } from "@/api/filesUpload";

const PARENT = "33333333-3333-3333-3333-333333333333";
const SESSION = "55555555-5555-5555-5555-555555555555";
const DRIVE = "44444444-4444-4444-4444-444444444444";
const OPERATION = "66666666-6666-6666-6666-666666666666";

// jsdom's Blob has no `arrayBuffer()`; the client slices the File for each part.
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

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function memoryStorage() {
  const cells = new Map<string, string>();
  return {
    getItem: (key: string) => cells.get(key) ?? null,
    setItem: (key: string, value: string) => void cells.set(key, value),
    removeItem: (key: string) => void cells.delete(key),
  };
}

/** The shed the body budget answers with, verbatim from `_shed_busy`. */
const BUSY = {
  error: {
    code: "unavailable",
    message: "The server is busy handling uploads; retry shortly",
    trace_id: "",
  },
};

interface Call {
  method: string;
  path: string;
}

type Answer = () => Response | Promise<Response>;

/** The scripted refusals for the two requests that come after the last part. */
interface Finish {
  /** Consumed one entry per `POST …/complete`; then the commit is accepted. */
  complete?: Answer[];
  /** Consumed one entry per operation read; then the operation reads `done`. */
  poll?: Answer[];
}

/**
 * The session API, with a scripted answer for each PUT of the one part.
 *
 * `partAnswers` is consumed one entry per PUT; once it runs out the part is
 * accepted, so "503, 503, then it lands" is written as two entries. `finish`
 * scripts the same way for the two requests the client makes after the bytes
 * are up.
 */
function scriptedServer(partAnswers: Answer[], finish: Finish = {}) {
  const calls: Call[] = [];
  const answers = [...partAnswers];
  const completes = [...(finish.complete ?? [])];
  const polls = [...(finish.poll ?? [])];
  const accepted: number[] = [];
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const path = new URL(url, "http://localhost").pathname;
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ method, path });

    if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
      return json(200, { uploadId: SESSION, partSize: 8, partsTotal: 1, expiresAt: "" });
    }
    if (method === "PUT" && /\/parts\/1$/.test(path)) {
      const scripted = answers.shift();
      if (scripted) return await scripted();
      accepted.push(1);
      return json(200, { ok: true });
    }
    if (method === "GET" && path.endsWith(`/uploads/${SESSION}`)) {
      return json(200, {
        uploadId: SESSION,
        state: "open",
        offset: accepted.length * 8,
        length: 8,
        complete: accepted.length > 0,
        partsDone: accepted.length,
        partsTotal: 1,
        acceptedParts: [...accepted],
      });
    }
    if (method === "POST" && path.endsWith("/complete")) {
      const scripted = completes.shift();
      if (scripted) return await scripted();
      if (finish.poll) return json(202, { id: OPERATION, state: "queued" });
      return json(200, { item: { id: "node-1", etag: "etag-1" } });
    }
    if (method === "GET" && path.endsWith(`/operations/${OPERATION}`)) {
      const scripted = polls.shift();
      if (scripted) return await scripted();
      return json(200, { id: OPERATION, state: "done", resultNodeId: "node-1" });
    }
    return json(500, { code: "unexpected", message: url });
  });
  return { impl, calls };
}

function client(impl: typeof fetch, driveId?: string): UploadClient {
  return new UploadClient({
    driveId,
    fetchImpl: impl,
    storage: memoryStorage(),
    digest: async () => "beef",
  });
}

function makeFile(): File {
  return new File([new Uint8Array(8).fill(65)], "one.bin", { type: "application/octet-stream" });
}

const partPuts = (calls: Call[]): Call[] =>
  calls.filter((call) => call.method === "PUT" && /\/parts\/1$/.test(call.path));

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("a part the server is too busy for is waited out", () => {
  it("re-sends a part the byte budget shed and finishes the upload", async () => {
    const { impl, calls } = scriptedServer([
      () => json(503, BUSY, { "retry-after": "1" }),
      () => json(503, BUSY, { "retry-after": "1" }),
    ]);
    vi.useFakeTimers();
    const handle = client(impl as unknown as typeof fetch).start(makeFile(), PARENT);
    const settled = handle.then((h) => h.done());
    await vi.runAllTimersAsync();

    const progress = await settled;
    expect(progress.state).toBe("done");
    // Three PUTs: the two the server shed, and the one it took.
    expect(partPuts(calls)).toHaveLength(3);
  });

  it("waits at least as long as the server asked before trying again", async () => {
    vi.useFakeTimers();
    const seenAt: number[] = [];
    const { impl } = scriptedServer([
      () => {
        seenAt.push(Date.now());
        // Two seconds, which is longer than the client's own first backoff —
        // so a client that ignored the header would come back early.
        return json(503, BUSY, { "retry-after": "2" });
      },
      () => {
        seenAt.push(Date.now());
        return json(200, { ok: true });
      },
    ]);
    const started = Date.now();
    const handle = client(impl as unknown as typeof fetch).start(makeFile(), PARENT);
    const settled = handle.then((h) => h.done());

    await vi.advanceTimersByTimeAsync(1_900);
    expect(seenAt).toHaveLength(1);

    await vi.runAllTimersAsync();
    await settled;
    expect(seenAt).toHaveLength(2);
    expect(seenAt[1]! - started).toBeGreaterThanOrEqual(2_000);
  });

  it("re-sends a part whose connection dropped with nothing answered", async () => {
    const { impl, calls } = scriptedServer([
      () => Promise.reject(new TypeError("Failed to fetch")),
    ]);
    vi.useFakeTimers();
    const handle = client(impl as unknown as typeof fetch).start(makeFile(), PARENT);
    const settled = handle.then((h) => h.done());
    await vi.runAllTimersAsync();

    expect((await settled).state).toBe("done");
    expect(partPuts(calls)).toHaveLength(2);
  });

  it("gives up once the shed outlasts the attempts it is given", async () => {
    const { impl, calls } = scriptedServer(
      Array.from({ length: PART_RETRY_ATTEMPTS + 2 }, () => () =>
        json(503, BUSY, { "retry-after": "1" }),
      ),
    );
    vi.useFakeTimers();
    const handle = client(impl as unknown as typeof fetch).start(makeFile(), PARENT);
    const settled = handle.then((h) => h.done());
    await vi.runAllTimersAsync();

    const progress = await settled;
    expect(progress.state).toBe("failed");
    expect(progress.error?.status).toBe(503);
    expect(partPuts(calls)).toHaveLength(PART_RETRY_ATTEMPTS);
  });

  it("does not re-send a part the server refused on its own terms", async () => {
    // 413: the part is too large for this deployment. Waiting changes nothing,
    // and eight more sends of a 128 MiB body is the expensive way to learn it.
    const { impl, calls } = scriptedServer([
      () => json(413, { code: "files.part_too_large", message: "that part is too large" }),
    ]);
    vi.useFakeTimers();
    const handle = client(impl as unknown as typeof fetch).start(makeFile(), PARENT);
    const settled = handle.then((h) => h.done());
    await vi.runAllTimersAsync();

    const progress = await settled;
    expect(progress.state).toBe("failed");
    expect(progress.error).toBeInstanceOf(ApiError);
    expect(progress.error?.status).toBe(413);
    expect(partPuts(calls)).toHaveLength(1);
  });

  it("re-sends a part the server throttled", async () => {
    const { impl, calls } = scriptedServer([
      () => json(429, { code: "rate_limited", message: "too many requests" }),
    ]);
    vi.useFakeTimers();
    const handle = client(impl as unknown as typeof fetch).start(makeFile(), PARENT);
    const settled = handle.then((h) => h.done());
    await vi.runAllTimersAsync();

    expect((await settled).state).toBe("done");
    expect(partPuts(calls)).toHaveLength(2);
  });
});

describe("the finish line is waited out the same way the parts are", () => {
  const completes = (calls: Call[]): Call[] =>
    calls.filter((call) => call.method === "POST" && call.path.endsWith("/complete"));
  const reads = (calls: Call[]): Call[] =>
    calls.filter((call) => call.method === "GET" && call.path.endsWith(`/operations/${OPERATION}`));

  it("re-sends a completion the server was too busy for", async () => {
    // Every byte is already on the server. Giving up here loses a file that
    // is entirely uploaded, for a refusal about the moment.
    const { impl, calls } = scriptedServer([], {
      complete: [() => json(503, BUSY, { "retry-after": "1" })],
      poll: [],
    });
    vi.useFakeTimers();
    const handle = client(impl as unknown as typeof fetch, DRIVE).start(makeFile(), PARENT);
    const settled = handle.then((h) => h.done());
    await vi.runAllTimersAsync();

    const progress = await settled;
    expect(progress.state).toBe("done");
    expect(completes(calls)).toHaveLength(2);
  });

  it("keeps polling an operation read the server refused for the moment", async () => {
    const { impl, calls } = scriptedServer([], {
      poll: [
        () => json(503, BUSY, { "retry-after": "1" }),
        () => json(200, { id: OPERATION, state: "running" }),
      ],
    });
    vi.useFakeTimers();
    const handle = client(impl as unknown as typeof fetch, DRIVE).start(makeFile(), PARENT);
    const settled = handle.then((h) => h.done());
    await vi.runAllTimersAsync();

    const progress = await settled;
    expect(progress.state).toBe("done");
    expect(progress.result?.nodeId).toBe("node-1");
    // The shed read, the running one, and the done one it settled on.
    expect(reads(calls)).toHaveLength(3);
  });

  it("does not re-send a completion the server refused on its own terms", async () => {
    const { impl, calls } = scriptedServer([], {
      complete: [
        () => json(413, { code: "files.too_large", message: "that file is too large" }),
        () => json(413, { code: "files.too_large", message: "that file is too large" }),
      ],
    });
    vi.useFakeTimers();
    const handle = client(impl as unknown as typeof fetch, DRIVE).start(makeFile(), PARENT);
    const settled = handle.then((h) => h.done());
    await vi.runAllTimersAsync();

    const progress = await settled;
    expect(progress.state).toBe("failed");
    expect(progress.error?.status).toBe(413);
    expect(completes(calls)).toHaveLength(1);
  });

  it("says the row is waiting while it waits out a shed completion", async () => {
    const seen: string[] = [];
    const { impl } = scriptedServer([], {
      complete: [() => json(503, BUSY, { "retry-after": "1" })],
      poll: [],
    });
    vi.useFakeTimers();
    const handle = new UploadClient({
      driveId: DRIVE,
      fetchImpl: impl as unknown as typeof fetch,
      storage: memoryStorage(),
      digest: async () => "beef",
      onProgress: (progress) => void seen.push(progress.state),
    }).start(makeFile(), PARENT);
    const settled = handle.then((h) => h.done());
    await vi.runAllTimersAsync();

    expect((await settled).state).toBe("done");
    expect(seen).toContain("waiting");
    expect(seen.indexOf("waiting")).toBeGreaterThan(seen.indexOf("completing"));
  });
});
