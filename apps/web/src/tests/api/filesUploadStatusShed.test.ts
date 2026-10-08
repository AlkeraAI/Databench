// A status read the server has no room for right now is a wait, not a failure.
//
// `GET /uploads/{id}` is read twice on the way through an upload: once at the
// start of a run, to learn what the server already holds, and once at the
// completion, to learn which parts to name. Both meet the same shed every other
// request meets (`apps/backend/backend/api/body_limit.py` sheds with a 503, the
// throttle with a 429), and the completion's read happens when every byte is
// already on the server — the most expensive moment there is to give up at.
//
// So both are waited out, and each assertion below is on the requests the
// client made, not on a stub repeating its own input.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PART_RETRY_ATTEMPTS, UploadClient } from "@/api/filesUpload";

const PARENT = "33333333-3333-3333-3333-333333333333";
const SESSION = "55555555-5555-5555-5555-555555555555";

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

/** The shed the body budget answers with, verbatim from `_shed_busy`. */
const BUSY = {
  error: { code: "unavailable", message: "The server is busy handling uploads; retry shortly" },
};

const THROTTLED = { code: "rate_limited", message: "too many requests" };

type Answer = () => Response;

/**
 * The session API over a one-part file, with a scripted answer per status read.
 *
 * `statusAnswers` is consumed one entry per `GET /uploads/{id}`; a `null` entry
 * or an exhausted script means "answer it normally". The run makes exactly two
 * of those reads, so `[null, refusal]` refuses the completion's read and lets
 * the run's own through.
 */
function scriptedServer(statusAnswers: (Answer | null)[] = []) {
  const scripted = [...statusAnswers];
  const statusReads: string[] = [];
  const completions: unknown[] = [];
  let landed = false;

  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const path = new URL(String(input), "http://localhost").pathname;
    const method = (init?.method ?? "GET").toUpperCase();

    if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
      return json(200, { uploadId: SESSION, partSize: 8, partsTotal: 1, expiresAt: "" });
    }
    if (method === "PUT" && /\/parts\/1$/.test(path)) {
      landed = true;
      return json(200, { partNo: 1, size: 8, duplicate: false });
    }
    if (method === "GET" && path.endsWith(`/uploads/${SESSION}`)) {
      const answer = scripted.length > 0 ? scripted.shift() : null;
      statusReads.push(answer ? "refused" : "read");
      if (answer) return answer();
      return json(200, {
        uploadId: SESSION,
        state: "open",
        offset: landed ? 8 : 0,
        length: 8,
        complete: landed,
        partsDone: landed ? 1 : 0,
        partsTotal: 1,
        acceptedParts: landed ? [1] : [],
      });
    }
    if (method === "POST" && path.endsWith("/complete")) {
      completions.push(JSON.parse(String(init?.body)));
      return json(200, { item: { id: "node-1", etag: "etag-1" } });
    }
    return json(500, { code: "unexpected", message: path });
  });

  return { impl, statusReads, completions };
}

function memoryStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  const cells = new Map<string, string>();
  return {
    getItem: (key: string): string | null => cells.get(key) ?? null,
    setItem: (key: string, value: string): void => void cells.set(key, value),
    removeItem: (key: string): void => void cells.delete(key),
  };
}

function makeFile(): File {
  const file = new File([new Uint8Array(8).fill(65)], "one.bin", {
    type: "application/octet-stream",
  });
  Object.defineProperty(file, "lastModified", { value: 1_700_000_000_000 });
  return file;
}

/** Drive one upload to its settled progress, with every backoff fast-forwarded. */
async function upload(impl: typeof fetch, states: string[] = []) {
  const client = new UploadClient({
    fetchImpl: impl,
    storage: memoryStorage(),
    digest: async () => "beef",
    onProgress: (progress) => {
      if (states[states.length - 1] !== progress.state) states.push(progress.state);
    },
  });
  vi.useFakeTimers();
  const settled = client.start(makeFile(), PARENT).then((handle) => handle.done());
  await vi.runAllTimersAsync();
  return await settled;
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("the status read that opens a run", () => {
  it("is re-read after a shed rather than failing the file", async () => {
    const server = scriptedServer([() => json(503, BUSY, { "retry-after": "1" })]);

    const progress = await upload(server.impl as unknown as typeof fetch);

    expect(progress.state).toBe("done");
    expect(server.statusReads).toEqual(["refused", "read", "read"]);
  });

  it("is re-read after a throttle", async () => {
    const server = scriptedServer([() => json(429, THROTTLED, { "retry-after": "1" })]);

    const progress = await upload(server.impl as unknown as typeof fetch);

    expect(progress.state).toBe("done");
    expect(server.completions).toHaveLength(1);
  });
});

describe("the status read the completion makes", () => {
  it("is re-read after a throttle rather than losing a file already landed", async () => {
    const server = scriptedServer([null, () => json(429, THROTTLED)]);

    const progress = await upload(server.impl as unknown as typeof fetch);

    expect(progress.state).toBe("done");
    expect(server.statusReads).toEqual(["read", "refused", "read"]);
    // The completion still names the part, so the wait cost a request and not
    // the agreement about what was uploaded.
    expect(server.completions).toEqual([
      { parts: [{ partNo: 1, size: 8, checksum: "beef" }], conflictBehavior: "fail" },
    ]);
  });

  it("says the row is waiting while it backs off", async () => {
    const server = scriptedServer([null, () => json(503, BUSY, { "retry-after": "1" })]);
    const states: string[] = [];

    await upload(server.impl as unknown as typeof fetch, states);

    // "completing" is reached, then the shed is announced as a wait rather than
    // as a stall, and the row still finishes.
    expect(states).toContain("completing");
    expect(states.indexOf("waiting")).toBeGreaterThan(states.indexOf("completing"));
    expect(states[states.length - 1]).toBe("done");
  });
});

describe("a status read the server keeps refusing", () => {
  it("fails the upload once the attempts run out, and not before", async () => {
    const server = scriptedServer(
      Array.from(
        { length: PART_RETRY_ATTEMPTS + 2 },
        () => () => json(503, BUSY, { "retry-after": "1" }),
      ),
    );

    const progress = await upload(server.impl as unknown as typeof fetch);

    expect(progress.state).toBe("failed");
    expect(progress.error?.status).toBe(503);
    expect(server.statusReads).toHaveLength(PART_RETRY_ATTEMPTS);
  });

  it("does not re-read one the server refused on its own terms", async () => {
    // A 403 is about the session, not the moment; re-asking eight times gets
    // the same word eight times.
    const server = scriptedServer([
      () => json(403, { code: "forbidden", message: "not yours" }),
    ]);

    const progress = await upload(server.impl as unknown as typeof fetch);

    expect(progress.state).toBe("failed");
    expect(progress.error?.status).toBe(403);
    expect(server.statusReads).toEqual(["refused"]);
  });
});
