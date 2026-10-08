// The shared MutationCache invalidation policy (api/queryClient.ts) — the one
// mechanism that makes "mutation succeeded but the UI is stale" impossible by
// construction. These tests pin the policy's contract directly (real useQuery /
// useMutation against createQueryClient), including the awaited-refetch ordering
// the app's seeds and modal-closes rely on, so a react-query upgrade that stops
// awaiting MutationCache callbacks fails here instead of going stale in the UI.

import {
  QueryClientProvider,
  useMutation,
  useQuery,
  type QueryClient,
  type QueryKey,
} from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import {
  useDeleteChatTemplate,
  useRenameChatTemplate,
  useSaveAsTemplate,
  useUpdateChatTemplate,
} from "@/api/chatTemplates";
import { useEnsurePlaces } from "@/api/files";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { ApiError } from "@/api/errors";

const wrapperFor = (qc: QueryClient) =>
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };

/** A query fn that counts its calls and resolves `${key}:${count}`. */
const countingFn = (counter: { n: number }) => async () => {
  counter.n += 1;
  return `v${counter.n}`;
};

describe("createQueryClient — the mutation invalidation policy", () => {
  it("a mutation with NO meta refetches every active query", async () => {
    const qc = createQueryClient({ retry: false });
    const a = { n: 0 };
    const b = { n: 0 };
    const wrapper = wrapperFor(qc);

    const { result } = renderHook(
      () => {
        const qa = useQuery({ queryKey: ["a"], queryFn: countingFn(a) });
        const qb = useQuery({ queryKey: ["b", "nested"], queryFn: countingFn(b) });
        const mut = useMutation({ mutationFn: async () => "done" });
        return { qa, qb, mut };
      },
      { wrapper },
    );
    await waitFor(() => expect(result.current.qa.data).toBe("v1"));
    await waitFor(() => expect(result.current.qb.data).toBe("v1"));

    await result.current.mut.mutateAsync();

    await waitFor(() => expect(result.current.qa.data).toBe("v2"));
    await waitFor(() => expect(result.current.qb.data).toBe("v2"));
    expect(a.n).toBe(2);
    expect(b.n).toBe(2);
  });

  it("a mutation with NO meta marks an INACTIVE query stale without fetching it", async () => {
    const qc = createQueryClient({ retry: false });
    const inactive = { n: 0 };
    const wrapper = wrapperFor(qc);

    // Mount a query, let it resolve, then unmount it — the cache entry survives with no observer.
    const mounted = renderHook(
      () => useQuery({ queryKey: ["inactive"], queryFn: countingFn(inactive) }),
      {
        wrapper,
      },
    );
    await waitFor(() => expect(mounted.result.current.data).toBe("v1"));
    mounted.unmount();

    const { result } = renderHook(() => useMutation({ mutationFn: async () => "done" }), {
      wrapper,
    });
    await result.current.mutateAsync();

    // Stale-marked so the next mount refetches, but no eager background fetch was spent on it.
    expect(qc.getQueryState(["inactive"])?.isInvalidated).toBe(true);
    expect(inactive.n).toBe(1);
  });

  it("the mutation stays pending until the active refetch lands — a spinner can't outrun the refresh", async () => {
    const qc = createQueryClient({ retry: false });
    const wrapper = wrapperFor(qc);
    let release: (() => void) | null = null;
    let calls = 0;
    const gatedFn = async () => {
      calls += 1;
      if (calls === 1) return "v1";
      // Hold the REFETCH open so the test can observe the mutation waiting on it.
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return "v2";
    };

    const { result } = renderHook(
      () => {
        const q = useQuery({ queryKey: ["gated"], queryFn: gatedFn });
        const mut = useMutation({ mutationFn: async () => "done" });
        return { q, mut };
      },
      { wrapper },
    );
    await waitFor(() => expect(result.current.q.data).toBe("v1"));

    result.current.mut.mutate();
    // The refetch is in flight and held open — the mutation must still be pending.
    await waitFor(() => expect(release).not.toBeNull());
    expect(result.current.mut.isPending).toBe(true);

    release!();
    await waitFor(() => expect(result.current.mut.isSuccess).toBe(true));
    // ...and by the time the mutation settled, the fresh data had landed.
    expect(qc.getQueryData(["gated"])).toBe("v2");
  });

  it("a callsite onSuccess observes the ALREADY-refreshed cache (toasts/modal-closes run after the refetch)", async () => {
    const qc = createQueryClient({ retry: false });
    const a = { n: 0 };
    const wrapper = wrapperFor(qc);
    let seenAtSuccess: unknown = null;

    const { result } = renderHook(
      () => {
        const q = useQuery({ queryKey: ["a"], queryFn: countingFn(a) });
        const mut = useMutation({ mutationFn: async () => "done" });
        return { q, mut };
      },
      { wrapper },
    );
    await waitFor(() => expect(result.current.q.data).toBe("v1"));

    result.current.mut.mutate(undefined, {
      onSuccess: () => {
        seenAtSuccess = qc.getQueryData(["a"]);
      },
    });
    await waitFor(() => expect(result.current.mut.isSuccess).toBe(true));
    expect(seenAtSuccess).toBe("v2");
  });

  it("meta.invalidates narrows the refetch to the declared prefixes", async () => {
    const qc = createQueryClient({ retry: false });
    const a = { n: 0 };
    const b = { n: 0 };
    const wrapper = wrapperFor(qc);

    const { result } = renderHook(
      () => {
        const qa = useQuery({ queryKey: ["a", "child"], queryFn: countingFn(a) });
        const qb = useQuery({ queryKey: ["b"], queryFn: countingFn(b) });
        const mut = useMutation({ mutationFn: async () => "done", meta: { invalidates: [["a"]] } });
        return { qa, qb, mut };
      },
      { wrapper },
    );
    await waitFor(() => expect(result.current.qa.data).toBe("v1"));
    await waitFor(() => expect(result.current.qb.data).toBe("v1"));

    await result.current.mut.mutateAsync();

    await waitFor(() => expect(result.current.qa.data).toBe("v2"));
    expect(a.n).toBe(2);
    expect(b.n).toBe(1);
    expect(qc.getQueryState(["b"])?.isInvalidated).toBe(false);
  });

  it('meta.invalidates: "none" skips invalidation entirely', async () => {
    const qc = createQueryClient({ retry: false });
    const a = { n: 0 };
    const wrapper = wrapperFor(qc);

    const { result } = renderHook(
      () => {
        const q = useQuery({ queryKey: ["a"], queryFn: countingFn(a) });
        const mut = useMutation({ mutationFn: async () => "done", meta: { invalidates: "none" } });
        return { q, mut };
      },
      { wrapper },
    );
    await waitFor(() => expect(result.current.q.data).toBe("v1"));

    await result.current.mut.mutateAsync();

    expect(a.n).toBe(1);
    expect(qc.getQueryState(["a"])?.isInvalidated).toBe(false);
  });

  it("a refetch that REJECTS cannot flip the succeeded mutation into an error", async () => {
    const qc = createQueryClient({ retry: false });
    const wrapper = wrapperFor(qc);
    let calls = 0;
    const flaky = async () => {
      calls += 1;
      if (calls === 1) return "v1";
      throw new Error("refetch blew up");
    };

    const { result } = renderHook(
      () => {
        const q = useQuery({ queryKey: ["flaky"], queryFn: flaky });
        const mut = useMutation({ mutationFn: async () => "done" });
        return { q, mut };
      },
      { wrapper },
    );
    await waitFor(() => expect(result.current.q.data).toBe("v1"));

    await result.current.mut.mutateAsync();
    await waitFor(() => expect(result.current.mut.isSuccess).toBe(true));
    expect(result.current.mut.isError).toBe(false);
  });

  it("a failed mutation does not invalidate (the server refused — nothing moved)", async () => {
    const qc = createQueryClient({ retry: false });
    const a = { n: 0 };
    const wrapper = wrapperFor(qc);

    const { result } = renderHook(
      () => {
        const q = useQuery({ queryKey: ["a"], queryFn: countingFn(a) });
        const mut = useMutation({
          mutationFn: async () => {
            throw new Error("rejected");
          },
        });
        return { q, mut };
      },
      { wrapper },
    );
    await waitFor(() => expect(result.current.q.data).toBe("v1"));

    await result.current.mut.mutateAsync().catch(() => {});
    await waitFor(() => expect(result.current.mut.isError).toBe(true));
    expect(a.n).toBe(1);
  });

  // A 409 is the one refusal that says the client's READ was wrong: a form left on it offers the
  // same refused write again forever (the dedicated-compute card kept a box another org had taken).
  const conflicting = (qc: QueryClient, status: number, invalidates?: ReadonlyArray<QueryKey> | "none") => {
    const a = { n: 0 };
    const b = { n: 0 };
    const seenInOnError: unknown[] = [];
    const { result } = renderHook(
      () => {
        const qa = useQuery({ queryKey: ["a"], queryFn: countingFn(a) });
        const qb = useQuery({ queryKey: ["b"], queryFn: countingFn(b) });
        const mut = useMutation({
          mutationFn: async () => {
            throw new ApiError(status, { code: "conflict", message: "taken" });
          },
          meta: invalidates === undefined ? undefined : { invalidates },
        });
        return { qa, qb, mut };
      },
      { wrapper: wrapperFor(qc) },
    );
    const refuse = () =>
      result.current.mut
        .mutateAsync(undefined, { onError: () => void seenInOnError.push(qc.getQueryData(["a"])) })
        .catch(() => {});
    return { a, b, result, refuse, seenInOnError };
  };

  it("a 409 refreshes the declared slots before the callsite's onError runs", async () => {
    const qc = createQueryClient({ retry: false });
    const { a, b, result, refuse, seenInOnError } = conflicting(qc, 409, [["a"]]);
    await waitFor(() => expect(result.current.qa.data).toBe("v1"));
    await waitFor(() => expect(result.current.qb.data).toBe("v1"));

    await refuse();
    await waitFor(() => expect(result.current.mut.isError).toBe(true));
    expect(a.n).toBe(2);
    expect(b.n).toBe(1);
    expect(seenInOnError).toEqual(["v2"]);
  });

  it("a 409 on a mutation that declares nothing refreshes nothing (no blanket re-read on a refusal)", async () => {
    const qc = createQueryClient({ retry: false });
    const { a, b, result, refuse } = conflicting(qc, 409);
    await waitFor(() => expect(result.current.qb.data).toBe("v1"));
    await refuse();
    await waitFor(() => expect(result.current.mut.isError).toBe(true));
    expect([a.n, b.n]).toEqual([1, 1]);
  });

  it('a 409 on a mutation declaring "none" refreshes nothing', async () => {
    const qc = createQueryClient({ retry: false });
    const { a, b, result, refuse } = conflicting(qc, 409, "none");
    await waitFor(() => expect(result.current.qb.data).toBe("v1"));
    await refuse();
    await waitFor(() => expect(result.current.mut.isError).toBe(true));
    expect([a.n, b.n]).toEqual([1, 1]);
  });

  it.each([400, 403, 404, 422, 500])("a %s refusal refreshes nothing", async (status) => {
    const qc = createQueryClient({ retry: false });
    const { a, b, result, refuse } = conflicting(qc, status, [["a"]]);
    await waitFor(() => expect(result.current.qb.data).toBe("v1"));
    await refuse();
    await waitFor(() => expect(result.current.mut.isError).toBe(true));
    expect([a.n, b.n]).toEqual([1, 1]);
  });

  it("a 401 is an answer, not a flake — the default retry does not ladder it", async () => {
    // No retry override here: this pins createQueryClient's OWN retry default.
    const qc = createQueryClient();
    const wrapper = wrapperFor(qc);
    const fn = vi.fn(async () => {
      throw new ApiError(401, null, "unauthorized");
    });

    const { result } = renderHook(() => useQuery({ queryKey: ["me"], queryFn: fn }), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 5000 });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("a 404 is an answer too — the page renders its not-found instead of shimmering", async () => {
    // Same reasoning, the one that cost a visible page: an admin org address the
    // register 404s kept its skeletons on screen for the whole backoff ladder.
    const qc = createQueryClient();
    const wrapper = wrapperFor(qc);
    const fn = vi.fn(async () => {
      throw new ApiError(404, null, "not found");
    });

    const { result } = renderHook(() => useQuery({ queryKey: ["gone"], queryFn: fn }), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 5000 });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("still ladders a status that is not an answer", async () => {
    // The carve-out is for statuses the server has settled, not for failure in
    // general: a 500 or a dropped connection is exactly what a retry is for.
    const qc = createQueryClient();
    const wrapper = wrapperFor(qc);
    const fn = vi.fn(async () => {
      throw new ApiError(500, null, "server error");
    });

    // The hook's own result says nothing here: what is being pinned is that the
    // query fn was CALLED again, which is the ladder the carve-out must not swallow.
    renderHook(() => useQuery({ queryKey: ["flaky"], queryFn: fn }), { wrapper });
    await waitFor(() => expect(fn.mock.calls.length).toBeGreaterThan(1), { timeout: 5000 });
  });
});

// The policy's default is invalidate-EVERYTHING, so a write that declares
// nothing still leaves a correct screen — it just refetches the whole portal on
// a click. These cases are what tells the two apart for the writes behind
// templates and the named folders they are filed in: each write's own families
// come back stale, and the reads it cannot possibly have changed are left alone.

describe("the template and place writes narrow their refresh", () => {
  const TEMPLATE = "77777777-7777-7777-7777-777777777777";
  const CHAT = "88888888-8888-8888-8888-888888888888";
  const DRIVE = "8a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d";

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** A client holding one entry per family a write might touch, so the policy
   *  has something real to choose between. */
  function seeded() {
    const qc = createQueryClient({ retry: false });
    qc.setQueryData(keys.chatTemplates.all, { items: [] });
    qc.setQueryData(keys.files.drive, { id: DRIVE });
    qc.setQueryData(keys.chats.all, []);
    qc.setQueryData(keys.auth.me, { id: "u1" });
    return { qc, wrapper: wrapperFor(qc) };
  }

  type Wrapper = ReturnType<typeof wrapperFor>;

  const TEMPLATE_FAMILIES = [keys.chatTemplates.all, keys.files.drive];

  const cases: { name: string; refreshes: QueryKey[]; mutate: (w: Wrapper) => Promise<unknown> }[] =
    [
      {
        name: "useSaveAsTemplate",
        refreshes: TEMPLATE_FAMILIES,
        mutate: async (wrapper) => {
          const { result } = renderHook(() => useSaveAsTemplate(), { wrapper });
          return result.current.mutateAsync({ source_chat_id: CHAT });
        },
      },
      {
        name: "useUpdateChatTemplate",
        refreshes: TEMPLATE_FAMILIES,
        mutate: async (wrapper) => {
          const { result } = renderHook(() => useUpdateChatTemplate(), { wrapper });
          return result.current.mutateAsync({
            templateId: TEMPLATE,
            brief: "Ask for the quarter first.",
            expectedVersion: 1,
          });
        },
      },
      {
        name: "useRenameChatTemplate",
        refreshes: TEMPLATE_FAMILIES,
        mutate: async (wrapper) => {
          const { result } = renderHook(() => useRenameChatTemplate(), { wrapper });
          return result.current.mutateAsync({
            templateId: TEMPLATE,
            title: "Quarterly revenue",
            expectedVersion: 1,
          });
        },
      },
      {
        name: "useDeleteChatTemplate",
        refreshes: TEMPLATE_FAMILIES,
        mutate: async (wrapper) => {
          const { result } = renderHook(() => useDeleteChatTemplate(), { wrapper });
          return result.current.mutateAsync({ templateId: TEMPLATE });
        },
      },
      {
        // Making a place puts a folder in the caller's home: a Files change, and
        // nothing a list of templates reads.
        name: "useEnsurePlaces",
        refreshes: [keys.files.drive],
        mutate: async (wrapper) => {
          const { result } = renderHook(() => useEnsurePlaces(), { wrapper });
          return result.current.mutateAsync({ driveId: DRIVE, places: ["chatTemplates"] });
        },
      },
    ];

  function stubOk(): void {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ id: TEMPLATE, chatTemplatesId: "nd_1" }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
  }

  it.each(cases)(
    "$name refreshes its own families and leaves the rest",
    async ({ refreshes, mutate }) => {
      stubOk();
      const { qc, wrapper } = seeded();

      await mutate(wrapper);

      for (const key of refreshes) {
        expect(qc.getQueryState(key)?.isInvalidated, JSON.stringify(key)).toBe(true);
      }
      // The signed-in identity and the chat list are read on every page and
      // change for none of these writes; a mutation that declared nothing would
      // have marked both.
      expect(qc.getQueryState(keys.auth.me)?.isInvalidated).toBe(false);
      expect(qc.getQueryState(keys.chats.all)?.isInvalidated).toBe(false);
    },
  );

  it("making a folder does not pretend the template list changed", async () => {
    stubOk();
    const { qc, wrapper } = seeded();

    const { result } = renderHook(() => useEnsurePlaces(), { wrapper });
    await result.current.mutateAsync({ driveId: DRIVE, places: ["chatTemplates"] });

    expect(qc.getQueryState(keys.chatTemplates.all)?.isInvalidated).toBe(false);
  });
});
