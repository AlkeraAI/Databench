import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import {
  useCreateFolder,
  useGrant,
  useMoveItem,
  useRenameItem,
  useRestoreTrash,
  useStar,
  useTrashItem,
  useUndoOperation,
} from "@/api/files";
import { UploadClient } from "@/api/filesUpload";
import { createQueryClient } from "@/api/queryClient";

// A live run found EVERY portal upload refused 428. The mocked tiers never saw
// it because their stubs answer any request at all. This one refuses like the
// server does: no `Idempotency-Key` on a non-GET Files request, no answer —
// so a client that stops sending one goes red here instead of in production.

const IDEMPOTENCY = "idempotency-key";

interface Refusal {
  /** Every non-GET Files path the fake server was asked for, in order. */
  readonly seen: string[];
  readonly keys: string[];
  /** The `If-Match` each write carried, where it carried one. */
  readonly matches: string[];
}

/** The server's contract, as a `fetch`: 428 unless a non-GET Files request
 *  carries a key, and 409 if it carries one that was already spent on a
 *  DIFFERENT path (a real store would replay, never re-run). */
function refusingFetch(
  answer: (method: string, path: string) => unknown,
  record: Refusal,
): typeof fetch {
  const spent = new Map<string, string>();
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    // The typed client hands `fetch` a built `Request`; the upload client hands
    // it a url plus an init. Both carry the header, and both are read here.
    const asRequest = input instanceof Request ? input : null;
    const url = asRequest ? asRequest.url : String(input);
    const path = url.replace(/^https?:\/\/[^/]+/, "");
    const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();
    const headers = new Headers(
      (init?.headers as HeadersInit | undefined) ?? asRequest?.headers,
    );
    const json = (body: unknown, status = 200) =>
      new Response(status === 204 ? null : JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      });

    if (method !== "GET" && path.startsWith("/api/v1/files/")) {
      const key = headers.get(IDEMPOTENCY);
      if (key === null || key === "") {
        return json(
          { code: "files.idempotency_key_required", message: "files.idempotency_key_required" },
          428,
        );
      }
      const before = spent.get(key);
      if (before !== undefined && before !== `${method} ${path}`) {
        return json({ code: "files.idempotency_key_reused" }, 409);
      }
      spent.set(key, `${method} ${path}`);
      record.seen.push(`${method} ${path}`);
      record.keys.push(key);
      const match = headers.get("if-match");
      if (match !== null) record.matches.push(match);
    }
    return json(answer(method, path));
  }) as typeof fetch;
}

function record(): Refusal {
  return { seen: [], keys: [], matches: [] };
}

/** The upload session API, as the server answers it. */
function uploadAnswers(size: number) {
  const accepted: number[] = [];
  return (method: string, path: string): unknown => {
    if (method === "POST" && path === "/api/v1/files/uploads") {
      return { uploadId: "up_1", partSize: 1024, partsTotal: 1, expiresAt: "" };
    }
    if (method === "PUT" && /\/parts\/\d+$/.test(path)) {
      accepted.push(Number(path.split("/").pop()));
      return {};
    }
    if (method === "GET") {
      return {
        uploadId: "up_1",
        state: "open",
        offset: accepted.length * size,
        length: size,
        complete: false,
        partsDone: accepted.length,
        partsTotal: 1,
        acceptedParts: [...accepted],
      };
    }
    return {};
  };
}

const memoryStorage = () => {
  const held = new Map<string, string>();
  return {
    getItem: (key: string) => held.get(key) ?? null,
    setItem: (key: string, value: string) => void held.set(key, value),
    removeItem: (key: string) => void held.delete(key),
  };
};

afterEach(() => vi.unstubAllGlobals());

describe("the upload client against a server that refuses an unkeyed write", () => {
  function client(seen: Refusal, fetchImpl?: typeof fetch) {
    const bytes = new File([new Uint8Array(12)], "panel-01.json", { type: "application/json" });
    return {
      bytes,
      client: new UploadClient({
        fetchImpl: fetchImpl ?? refusingFetch(uploadAnswers(12), seen),
        storage: memoryStorage(),
        digest: async () => "sha",
      }),
    };
  }

  it("opens, sends its part and completes", async () => {
    const seen = record();
    const { bytes, client: uploads } = client(seen);

    const handle = await uploads.start(bytes, "nd_home");
    const done = await handle.done();

    expect(done.state).toBe("done");
    // The three writes the live run never got past the first of.
    expect(seen.seen).toEqual([
      "POST /api/v1/files/uploads",
      "PUT /api/v1/files/uploads/up_1/parts/1",
      "POST /api/v1/files/uploads/up_1/complete",
    ]);
    // Each write spends its OWN key: one key across all three would make a
    // retried part replay the open's stored answer.
    expect(new Set(seen.keys).size).toBe(3);
  });

  it("cancels through a keyed DELETE", async () => {
    const seen = record();
    const { bytes, client: uploads } = client(seen);

    const handle = await uploads.start(bytes, "nd_home");
    await handle.cancel();

    expect(seen.seen).toContain("DELETE /api/v1/files/uploads/up_1");
  });

  it("fails the way production did when the key is stripped on the wire", async () => {
    const seen = record();
    const stripped = refusingFetch(uploadAnswers(12), seen);
    const { bytes, client: uploads } = client(
      seen,
      ((input: RequestInfo | URL, init?: RequestInit) => {
        const headers = new Headers(init?.headers as HeadersInit | undefined);
        headers.delete(IDEMPOTENCY);
        return stripped(input, { ...init, headers });
      }) as typeof fetch,
    );

    // Proves the refusal above is the mechanism and not a stub that says yes:
    // take the header away and the very first call is the 428 the live run saw.
    await expect(uploads.start(bytes, "nd_home")).rejects.toMatchObject({
      status: 428,
    });
  });
});

describe("every portal Files mutation carries a key", () => {
  function harness(seen: Refusal) {
    vi.stubGlobal("fetch", refusingFetch(() => ({ id: "nd_1", etag: "et_2" }), seen));
    const client = createQueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    return wrapper;
  }

  const families = [
    ["create", useCreateFolder, { driveId: "dr_1", parentId: "nd_home", name: "reports" }],
    ["rename", useRenameItem, { driveId: "dr_1", itemId: "nd_1", etag: "et_1", name: "b" }],
    ["move", useMoveItem, { driveId: "dr_1", itemId: "nd_1", etag: "et_1", parentId: "nd_2" }],
    ["trash", useTrashItem, { driveId: "dr_1", itemId: "nd_1", etag: "et_1" }],
    ["restore", useRestoreTrash, { driveId: "dr_1", opId: "op_1" }],
    ["star", useStar, { driveId: "dr_1", itemId: "nd_1", etag: "et_1", starred: true }],
    ["undo", useUndoOperation, { driveId: "dr_1", operationId: "op_1" }],
    [
      "share",
      useGrant,
      {
        driveId: "dr_1",
        itemId: "nd_1",
        etag: "et_1",
        principal: { kind: "user", id: "us_1" },
        role: "viewer",
      },
    ],
  ] as const;

  for (const [name, hook, vars] of families) {
    it(`${name} is accepted rather than refused 428`, async () => {
      const seen = record();
      const wrapper = harness(seen);
      const { result } = renderHook(() => (hook as () => ReturnType<typeof useCreateFolder>)(), {
        wrapper,
      });

      result.current.mutate(vars as never);

      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(result.current.error).toBeNull();
      expect(seen.keys).toHaveLength(1);
      expect(seen.keys[0]).not.toBe("");
    });
  }

  it("carries the caller's If-Match beside the key, and no more than one write per attempt", async () => {
    const seen = record();
    const wrapper = harness(seen);
    const { result } = renderHook(() => useRenameItem(), { wrapper });

    result.current.mutate({ driveId: "dr_1", itemId: "nd_1", etag: "et_1", name: "b" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(seen.seen).toEqual([
      "PATCH /api/v1/files/drives/dr_1/items/nd_1?conflict_behavior=fail",
    ]);
    expect(seen.matches).toEqual(["et_1"]);
  });

  it("refuses the same request without a key, so the harness is the server and not a yes-man", async () => {
    const seen = record();
    vi.stubGlobal("fetch", refusingFetch(() => ({}), seen));
    const response = await fetch("http://localhost/api/v1/files/drives/dr_1/items/nd_1", {
      method: "DELETE",
    });
    const body = (await response.json()) as { code?: string };

    expect(response.status).toBe(428);
    expect(body.code).toBe("files.idempotency_key_required");
    expect(new ApiError(response.status, body).status).toBe(428);
    expect(seen.keys).toHaveLength(0);
  });
});
