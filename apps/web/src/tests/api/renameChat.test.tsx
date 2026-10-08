// Renaming a chat from the web.
//
// A chat IS a workspace object, so the rename is `PUT /api/v1/objects/{id}` with
// the version that was just read — the object policy's WRITE gate, its version
// check and its decision row, with no second rename endpoint to keep in step.
// What is pinned here is the contract every entry point stands on: the request
// that goes out, the version it names, the title on screen before the echo, the
// rollback when the server refuses, and the re-read after a conflict.

import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { keys } from "@/api/keys";
import { EMPTY_TITLE_REFUSAL, renameRefusal, useRenameChat } from "@/api/objects";
import { createQueryClient } from "@/api/queryClient";
import { ApiError } from "@/api/errors";

const CHAT = "3952c9e2-4d6a-4a01-9f53-000000000001";

interface Call {
  readonly method: string;
  readonly url: string;
  readonly body: Record<string, unknown> | null;
}

/** Every request the hook made, in order. */
let calls: Call[];

/** What the next `GET /objects/{id}` answers. */
let objectVersion: number;

/** When set, the next PUT answers this status instead of 200. */
let putRefusal: { status: number; body: unknown } | null;

function stubFetch(): void {
  calls = [];
  objectVersion = 7;
  putRefusal = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
      calls.push({ method, url, body });
      if (method === "PUT" && putRefusal) {
        const refusal = putRefusal;
        return new Response(JSON.stringify(refusal.body), {
          status: refusal.status,
          headers: { "content-type": "application/json" },
        });
      }
      const title = method === "PUT" ? String(body?.title ?? "") : "Warehouse spike";
      return new Response(
        JSON.stringify({
          id: CHAT,
          type: "chat",
          title,
          version: method === "PUT" ? objectVersion + 1 : objectVersion,
          status: "ready",
          spec: {},
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }),
  );
}

beforeEach(stubFetch);
afterEach(() => vi.unstubAllGlobals());

function harness() {
  const qc = createQueryClient({ retry: false });
  qc.setQueryData(keys.chats.one(CHAT), { id: CHAT, title: "Warehouse spike" });
  qc.setQueryData(keys.objects.one(CHAT), { id: CHAT, title: "Warehouse spike", version: 7 });
  qc.setQueryData(keys.chats.all, {
    items: [
      { id: CHAT, title: "Warehouse spike" },
      { id: "other", title: "Untouched" },
    ],
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(() => useRenameChat(), { wrapper });
  return { qc, result };
}

const put = (): Call | undefined => calls.find((call) => call.method === "PUT");

describe("useRenameChat", () => {
  it("writes the title through the object route, naming the version it just read", async () => {
    const { result } = harness();

    await act(async () => {
      await result.current.mutateAsync({ chatId: CHAT, title: "Q3 warehouse spike" });
    });

    // The read comes first and the write names what it found: a PUT that
    // guessed the version is a 409 the reader did nothing to deserve.
    expect(calls.map((call) => call.method)).toEqual(["GET", "PUT"]);
    expect(calls[0].url).toContain(`/api/v1/objects/${CHAT}`);
    expect(put()?.url).toContain(`/api/v1/objects/${CHAT}`);
    expect(put()?.body).toEqual({ title: "Q3 warehouse spike", expected_version: 7 });
  });

  it("shows the new title before the echo, in the chat, the object and the rail", async () => {
    const { qc, result } = harness();
    // A PUT that never settles: whatever is on screen at this point is the
    // optimistic patch alone.
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const real = globalThis.fetch as typeof fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if ((init?.method ?? "GET") === "PUT") await gate;
        return real(input, init);
      }),
    );

    act(() => {
      result.current.mutate({ chatId: CHAT, title: "Q3 warehouse spike" });
    });

    await waitFor(() => {
      expect((qc.getQueryData(keys.chats.one(CHAT)) as { title: string }).title).toBe(
        "Q3 warehouse spike",
      );
    });
    expect((qc.getQueryData(keys.objects.one(CHAT)) as { title: string }).title).toBe(
      "Q3 warehouse spike",
    );
    const list = qc.getQueryData(keys.chats.all) as { items: { id: string; title: string }[] };
    expect(list.items.map((item) => item.title)).toEqual(["Q3 warehouse spike", "Untouched"]);
    release?.();
  });

  it("puts every cached title back when the server refuses", async () => {
    const { qc, result } = harness();
    putRefusal = { status: 403, body: { detail: { message: "You cannot rename this chat" } } };

    await act(async () => {
      await result.current
        .mutateAsync({ chatId: CHAT, title: "Q3 warehouse spike" })
        .catch(() => undefined);
    });

    expect((qc.getQueryData(keys.chats.one(CHAT)) as { title: string }).title).toBe(
      "Warehouse spike",
    );
    expect((qc.getQueryData(keys.objects.one(CHAT)) as { title: string }).title).toBe(
      "Warehouse spike",
    );
    const list = qc.getQueryData(keys.chats.all) as { items: { title: string }[] };
    expect(list.items.map((item) => item.title)).toEqual(["Warehouse spike", "Untouched"]);
  });

  it("re-reads the chat after a version conflict, so the next try starts from the live title", async () => {
    const qc = createQueryClient({ retry: false });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    // A live read, so a refetch is something that can be observed happening.
    const { result } = renderHook(
      () => ({
        object: useQuery({
          queryKey: keys.objects.one(CHAT),
          queryFn: async () => {
            const response = await fetch(`/api/v1/objects/${CHAT}`);
            return (await response.json()) as { title: string };
          },
        }),
        rename: useRenameChat(),
      }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.object.data?.title).toBe("Warehouse spike"));
    const before = calls.filter((call) => call.method === "GET").length;
    putRefusal = {
      status: 409,
      body: { detail: { code: "version_conflict", message: "This object moved on" } },
    };

    await act(async () => {
      await result.current.rename
        .mutateAsync({ chatId: CHAT, title: "Q3 warehouse spike" })
        .catch(() => undefined);
    });

    // The conflict's own re-read, on top of the one the mutation makes.
    await waitFor(() => {
      expect(calls.filter((call) => call.method === "GET").length).toBeGreaterThan(before + 1);
    });
  });

  it("refreshes the Files listing, the chat list and the objects — and nothing else", async () => {
    // The MutationCache policy in createQueryClient owns the refresh; the hook's
    // only job is to name what a rename changes. Named too narrowly, the Files
    // row keeps the old title; named not at all, every read in the tab refetches.
    const qc = createQueryClient({ retry: false });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const counts = { files: 0, chats: 0, objects: 0, unrelated: 0 };
    const counting = (slot: keyof typeof counts) => async () => {
      counts[slot] += 1;
      return counts[slot];
    };
    const { result } = renderHook(
      () => ({
        files: useQuery({
          queryKey: keys.files.children("d", "p", {}, 50),
          queryFn: counting("files"),
        }),
        chats: useQuery({ queryKey: keys.chats.all, queryFn: counting("chats") }),
        objects: useQuery({ queryKey: keys.objects.all, queryFn: counting("objects") }),
        unrelated: useQuery({ queryKey: keys.me.credits, queryFn: counting("unrelated") }),
        rename: useRenameChat(),
      }),
      { wrapper },
    );
    await waitFor(() => expect(counts).toEqual({ files: 1, chats: 1, objects: 1, unrelated: 1 }));

    await act(async () => {
      await result.current.rename.mutateAsync({ chatId: CHAT, title: "Q3 warehouse spike" });
    });

    await waitFor(() => expect(counts.files).toBe(2));
    expect(counts.chats).toBe(2);
    expect(counts.objects).toBe(2);
    expect(counts.unrelated).toBe(1);
  });
});

describe("renameRefusal", () => {
  it("tells a conflicted reader the chat moved and has been re-read", () => {
    const copy = renameRefusal(
      new ApiError(409, { detail: { code: "version_conflict", message: "This object moved on" } }),
    );
    expect(copy).toMatch(/changed underneath you/);
  });

  it("keeps the server's own sentence for a refusal that named one", () => {
    expect(
      renameRefusal(new ApiError(403, { code: "forbidden", message: "Ask the owner for access" })),
    ).toBe("Ask the owner for access");
  });

  it("falls back to a sentence rather than a status code when the server said nothing", () => {
    expect(renameRefusal(new ApiError(500, null))).toBe("This chat could not be renamed.");
  });

  it("reuses the server's own wording for a title with nothing in it", () => {
    expect(EMPTY_TITLE_REFUSAL).toBe("String should have at least 1 character");
  });
});
