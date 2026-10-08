// The upload client against a scripted session API: the part sizing, the
// resume, the cancel, and the 507 at open.
//
// `fetch` is the seam — every assertion is on the REQUESTS the client made and
// on what it did with the answers, never on a stub echoing its own input.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { isNameTaken, sessionIsSpent, uploadStorageKey, UploadClient } from "@/api/filesUpload";

/** Who the uploads are remembered for. */
const ACCOUNT = { userId: "usr_dana", orgId: "org_a" };

const PARENT = "33333333-3333-3333-3333-333333333333";
const SESSION = "55555555-5555-5555-5555-555555555555";
const PART_SIZE = 8;

// jsdom's Blob has no `arrayBuffer()` (browsers have had it for years), and the
// client slices the File to build each part. Read the slice through FileReader
// so the test exercises the real slicing rather than a stubbed body.
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

/** A File whose bytes are real, so `slice().arrayBuffer()` is real work. */
function makeFile(bytes: number, name = "big.bin"): File {
  const file = new File([new Uint8Array(bytes).fill(65)], name, { type: "application/octet-stream" });
  Object.defineProperty(file, "lastModified", { value: 1_700_000_000_000 });
  return file;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** An in-memory storage the test can inspect and hand to a second client, which
 *  is what "a reload" means here. */
function memoryStorage() {
  const cells = new Map<string, string>();
  return {
    cells,
    getItem: (key: string) => cells.get(key) ?? null,
    setItem: (key: string, value: string) => void cells.set(key, value),
    removeItem: (key: string) => void cells.delete(key),
  };
}

/** A digest the test can predict, so the assertions read the client's slicing
 *  rather than a hash. Real runs use SubtleCrypto SHA-256. */
const digest = async (part: Blob): Promise<string> =>
  `${part.size.toString(16).padStart(4, "0")}`;

/**
 * A scripted server: open → status → parts → complete, tracking what landed so
 * a resume is answered with the truth rather than with a canned page.
 */
function scriptedServer(options: { total: number; alreadyDone?: number[] }) {
  const accepted = new Set<number>(options.alreadyDone ?? []);
  const calls: { method: string; url: string; headers: Record<string, string>; body?: unknown }[] =
    [];
  const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((value, key) => (headers[key] = value));
    calls.push({ method, url, headers, body: init?.body });

    if (method === "POST" && url.endsWith("/api/v1/files/uploads")) {
      return json(201, {
        uploadId: SESSION,
        partSize: PART_SIZE,
        partsTotal: options.total,
        limits: { maxPartBytes: 1024, maxParts: 10 },
        expiresAt: "2026-01-01T00:00:00Z",
      });
    }
    if (method === "GET" && url.endsWith(`/uploads/${SESSION}`)) {
      const done = [...accepted].sort((a, b) => a - b);
      return json(200, {
        uploadId: SESSION,
        state: "open",
        offset: done.length * PART_SIZE,
        length: options.total * PART_SIZE,
        complete: false,
        partsDone: done.length,
        partsTotal: options.total,
        acceptedParts: done,
      });
    }
    const part = /\/parts\/(\d+)$/.exec(url);
    if (method === "PUT" && part) {
      accepted.add(Number(part[1]));
      return json(200, { partNo: Number(part[1]), size: PART_SIZE, duplicate: false });
    }
    if (method === "POST" && url.endsWith("/complete")) {
      return json(202, { id: "op", kind: "upload", state: "queued", done: 0, total: 1 });
    }
    if (method === "DELETE") return new Response(null, { status: 204 });
    return json(500, { error: { code: "unexpected", message: url } });
  });
  return { impl, calls, accepted };
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("part sizing", () => {
  it("sends one part per partSize, in order, each with its checksum", async () => {
    const server = scriptedServer({ total: 3 });
    const client = new UploadClient({
      account: ACCOUNT,
      fetchImpl: server.impl as unknown as typeof fetch,
      storage: memoryStorage(),
      digest,
    });

    const handle = await client.start(makeFile(PART_SIZE * 3 - 2), PARENT);
    const finished = await handle.done();
    expect([finished.state, finished.error?.message]).toEqual(["done", undefined]);

    const parts = server.calls.filter((call) => call.method === "PUT");
    expect(parts.map((call) => call.url.split("/parts/")[1])).toEqual(["1", "2", "3"]);
    // The last part is the remainder, not a padded full part. The body is the
    // file's own slice rather than a copy of its bytes, so the browser streams
    // it off disk and a 128 MiB part is never resident.
    expect((parts[0].body as Blob).size).toBe(PART_SIZE);
    expect((parts[2].body as Blob).size).toBe(PART_SIZE - 2);
    for (const call of parts) {
      expect(call.headers["x-part-checksum"]).toBeTruthy();
      expect(call.headers["idempotency-key"]).toBeTruthy();
    }
  });
});

describe("resume", () => {
  it("after a reload continues from the server's partsDone", async () => {
    const storage = memoryStorage();
    const file = makeFile(PART_SIZE * 4);

    // First load: open the session, then die after two parts landed.
    const first = scriptedServer({ total: 4 });
    const opener = new UploadClient({
      account: ACCOUNT,
      fetchImpl: first.impl as unknown as typeof fetch,
      storage,
      digest,
    });
    await (await opener.start(file, PARENT)).done();
    // Forgotten once the session completed: nothing to resume.
    expect(JSON.parse(storage.getItem(uploadStorageKey(ACCOUNT)) ?? "{}")).toEqual({});

    // Re-open a session and simulate the reload before it finished — after a
    // part has landed, which is what "the server already holds 1-2" below
    // means. A session with nothing on it is not resumable and has nothing to
    // resume.
    const second = scriptedServer({ total: 4 });
    const holder: { handle?: { pause(): void } } = {};
    const before = new UploadClient({
      account: ACCOUNT,
      fetchImpl: second.impl as unknown as typeof fetch,
      storage,
      digest,
      onProgress: (progress) => {
        // Two parts, because the "reload" below meets a server holding both:
        // a resume may only skip a part this browser recorded a digest for.
        if (progress.partsDone >= 2) holder.handle?.pause();
      },
    });
    holder.handle = await before.start(file, PARENT);
    const remembered = await vi.waitFor(() => {
      const held = Object.values(
        JSON.parse(storage.getItem(uploadStorageKey(ACCOUNT)) ?? "{}") as Record<string, unknown>,
      );
      // The record identifies the file by what its parts hash to, and a part
      // the record cannot name is one a resume may not skip.
      expect(Object.keys((held[0] as { partDigests?: object }).partDigests ?? {})).toEqual(
        ["1", "2"],
      );
      return held[0];
    });
    expect(remembered).toMatchObject({
      uploadId: SESSION,
      name: file.name,
      size: file.size,
      lastModified: file.lastModified,
    });

    // "Reload": a fresh client, the same storage, a server that already holds 1-2.
    const resumed = scriptedServer({ total: 4, alreadyDone: [1, 2] });
    const after = new UploadClient({
      account: ACCOUNT,
      fetchImpl: resumed.impl as unknown as typeof fetch,
      storage,
      digest,
    });
    await (await after.start(file, PARENT)).done();

    // It rejoined instead of opening a second session, and re-sent nothing.
    expect(resumed.calls.some((call) => call.url.endsWith("/api/v1/files/uploads"))).toBe(false);
    const sent = resumed.calls
      .filter((call) => call.method === "PUT")
      .map((call) => call.url.split("/parts/")[1]);
    expect(sent).toEqual(["3", "4"]);
  });

  it("never offers or rejoins a session left by the same person in another org", async () => {
    const storage = memoryStorage();
    const file = makeFile(PART_SIZE * 4);
    const holder: { handle?: { pause(): void } } = {};
    const inA = new UploadClient({
      account: ACCOUNT,
      fetchImpl: scriptedServer({ total: 4 }).impl as unknown as typeof fetch,
      storage,
      digest,
      onProgress: (progress) => {
        if (progress.partsDone >= 2) holder.handle?.pause();
      },
    });
    holder.handle = await inA.start(file, PARENT);
    await vi.waitFor(() => expect(inA.resumable()).toHaveLength(1));

    // The same person, the same browser, switched to another org.
    const elsewhere = scriptedServer({ total: 4, alreadyDone: [1, 2] });
    const inB = new UploadClient({
      account: { userId: ACCOUNT.userId, orgId: "org_b" },
      fetchImpl: elsewhere.impl as unknown as typeof fetch,
      storage,
      digest,
    });
    expect(inB.resumable()).toEqual([]);
    await (await inB.start(file, PARENT)).done();
    // It opened a session of its own rather than sending into org A's.
    expect(elsewhere.calls.some((call) => call.url.endsWith("/api/v1/files/uploads"))).toBe(true);
    // And org A's record is untouched, still there to resume back in org A.
    expect(inA.resumable()).toHaveLength(1);
  });

  it("remembers nothing for a client that names no account", async () => {
    const storage = memoryStorage();
    const holder: { handle?: { pause(): void }; sent: number } = { sent: 0 };
    const anonymous = new UploadClient({
      fetchImpl: scriptedServer({ total: 4 }).impl as unknown as typeof fetch,
      storage,
      digest,
      onProgress: (progress) => {
        holder.sent = progress.partsDone;
        if (progress.partsDone >= 2) holder.handle?.pause();
      },
    });
    holder.handle = await anonymous.start(makeFile(PART_SIZE * 4), PARENT);
    // Mid-upload, where a client with an account holds a record to resume from.
    await vi.waitFor(() => expect(holder.sent).toBeGreaterThanOrEqual(2));
    expect(storage.cells.size).toBe(0);
    expect(anonymous.resumable()).toEqual([]);
  });
});

describe("cancel", () => {
  it("aborts the session with a DELETE and forgets it", async () => {
    const storage = memoryStorage();
    const server = scriptedServer({ total: 4 });
    const client = new UploadClient({
      account: ACCOUNT,
      fetchImpl: server.impl as unknown as typeof fetch,
      storage,
      digest,
    });
    const handle = await client.start(makeFile(PART_SIZE * 4), PARENT);
    await handle.cancel();

    const deletes = server.calls.filter((call) => call.method === "DELETE");
    expect(deletes).toHaveLength(1);
    expect(deletes[0].url).toContain(`/uploads/${SESSION}`);
    expect(deletes[0].headers["idempotency-key"]).toBeTruthy();
    expect(storage.getItem(uploadStorageKey(ACCOUNT))).toBe("{}");
    expect((await handle.done()).state).toBe("cancelled");
  });
});

describe("quota", () => {
  it("a 507 at open is a typed error, and no part is ever sent", async () => {
    const impl = vi.fn(async () =>
      json(507, { error: { code: "files.quota_exceeded", message: "the drive is full" } }),
    );
    const client = new UploadClient({
      account: ACCOUNT,
      fetchImpl: impl as unknown as typeof fetch,
      storage: memoryStorage(),
      digest,
    });

    const failure = await client.start(makeFile(PART_SIZE), PARENT).catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(507);
    expect((failure as ApiError).code).toBe("files.quota_exceeded");
    expect((failure as ApiError).message).toBe("the drive is full");
    expect(impl).toHaveBeenCalledTimes(1);
  });
});

describe("conflicts", () => {
  it("asks the page, and retries the completion with rename on 'keep both'", async () => {
    let completes = 0;
    const impl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method === "POST" && url.endsWith("/api/v1/files/uploads")) {
        return json(201, {
          uploadId: SESSION,
          partSize: PART_SIZE,
          partsTotal: 1,
          limits: { maxPartBytes: 1024, maxParts: 10 },
          expiresAt: "",
        });
      }
      if (method === "GET") {
        return json(200, {
          uploadId: SESSION,
          state: "open",
          offset: 0,
          length: PART_SIZE,
          complete: false,
          partsDone: 0,
          partsTotal: 1,
          acceptedParts: [1],
        });
      }
      if (method === "PUT") return json(200, { partNo: 1, size: PART_SIZE, duplicate: false });
      if (url.endsWith("/complete")) {
        completes += 1;
        const body = JSON.parse(String(init?.body)) as { conflictBehavior: string };
        if (body.conflictBehavior === "fail") {
          return json(409, { error: { code: "files.name_taken", message: "already there" } });
        }
        return json(202, { id: "op", kind: "upload", state: "queued", done: 0, total: 1 });
      }
      return json(500, { error: { code: "unexpected", message: url } });
    });

    const asked: string[] = [];
    const client = new UploadClient({
      account: ACCOUNT,
      fetchImpl: impl as unknown as typeof fetch,
      storage: memoryStorage(),
      digest,
      onConflict: (name) => {
        asked.push(name);
        return "keep-both";
      },
    });

    const handle = await client.start(makeFile(PART_SIZE, "taken.bin"), PARENT);
    expect((await handle.done()).state).toBe("done");
    expect(asked).toEqual(["taken.bin"]);
    expect(completes).toBe(2);
  });
});

describe("what a collision is recognised by", () => {
  it.each([
    ["the 409 the session API refuses a completion with", new ApiError(409, { code: "files.exists", message: "Refused." }), true],
    ["the code a queued commit records", new ApiError(0, { code: "files.exists", message: "Refused." }), true],
    // The code decides, never the prose: matching prose would turn any refusal
    // that happens to quote the sentence into a "keep both or replace?"
    // question the person cannot answer.
    [
      "prose alone, which is never enough",
      new ApiError(0, {
        code: "files.storage_unavailable",
        message: "that name is taken in this folder",
      }),
      false,
    ],
    ["an unrelated refusal", new ApiError(0, { code: "files.quota_bytes", message: "Full." }), false],
    // Also a 409, but about the session: no answer to a name question can
    // complete a session a refused commit already ended.
    [
      "a session that can no longer complete",
      new ApiError(409, { code: "files.session_state", message: "session s is aborted and cannot complete" }),
      false,
    ],
  ])("%s", (_case, error, expected) => {
    expect(isNameTaken(error)).toBe(expected);
  });
});

describe("what ends a session rather than an attempt", () => {
  it.each([
    ["parts the server will not agree to", new ApiError(409, { code: "files.parts_mismatch", message: "x" }), true],
    ["a session that can no longer complete", new ApiError(409, { code: "files.session_state", message: "x" }), true],
    ["a name collision, which an answer settles", new ApiError(409, { code: "files.exists", message: "x" }), false],
    ["a dropped connection", new ApiError(0, { code: "files.error", message: "x" }), false],
  ])("%s", (_case, error, expected) => {
    expect(sessionIsSpent(error)).toBe(expected);
  });
});
