// The Files hooks (api/files.ts): what each one asks the server for, how a
// folder's marker paging terminates, how filters and order reach the wire, and
// that every key the event map names is a key a hook really caches under.
//
// `fetch` is stubbed so the assertions are on the REQUEST the hook built (URL +
// query string) and on what react-query does with the response — never on a
// mock echoing itself back.

import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { EVENT_KEYS, type RealtimeEventFrame } from "@/api/events/eventMap";
import {
  flattenChildren,
  toChildrenParams,
  useChildren,
  useDrive,
  useItem,
  useLeases,
  useOperation,
  usePermissions,
  useRecent,
  useSharedWithMe,
  useStarred,
  useTrash,
  useVersions,
  type ChildrenPage,
} from "@/api/files";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";

const DRIVE = "d1";
const ROOT = "n-root";

const wrapperFor = (qc: QueryClient) =>
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };

const json = (body: unknown): Response =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

/** Stub `fetch`, recording every URL it was asked for. */
function stubFetch(reply: (url: string) => unknown): { urls: string[] } {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      urls.push(url);
      return json(reply(url));
    }),
  );
  return { urls };
}

const item = (id: string, name: string): Record<string, unknown> => ({ id, name, kind: "file" });
const page = (ids: string[], nextMarker: string | null): ChildrenPage =>
  ({ value: ids.map((id) => item(id, id)), nextMarker }) as unknown as ChildrenPage;

const queryOf = (url: string): URLSearchParams => new URL(url).searchParams;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("toChildrenParams", () => {
  it("serialises every filter to its wire spelling", () => {
    expect(
      toChildrenParams({
        kind: "folder",
        objectType: "chat",
        mimeClass: "image",
        owner: "me",
        modifiedAfter: "2026-01-01T00:00:00Z",
        modifiedBefore: "2026-02-01T00:00:00Z",
        sizeMin: 10,
        sizeMax: 20,
        nameFlag: "windows_safe",
        starred: true,
        shared: true,
        leased: true,
        trashed: true,
      }),
    ).toEqual({
      kind: "folder",
      objectType: "chat",
      mimeClass: "image",
      owner: "me",
      modifiedAfter: "2026-01-01T00:00:00Z",
      modifiedBefore: "2026-02-01T00:00:00Z",
      sizeMin: "10",
      sizeMax: "20",
      nameFlag: "windows_safe",
      starred: "true",
      shared: "true",
      leased: "true",
      trashed: "true",
    });
  });

  it("omits an unset filter but keeps an explicitly false one", () => {
    // The server refuses a filter it cannot represent, and "not starred" is a
    // real question — dropping `false` would silently widen the view.
    expect(toChildrenParams({ starred: false })).toEqual({ starred: "false" });
    expect(toChildrenParams({})).toEqual({});
    expect(toChildrenParams({ kind: undefined })).toEqual({});
  });

  it("spells orderBy as the route parses it, direction optional", () => {
    expect(toChildrenParams({}, { field: "size", direction: "desc" }).orderBy).toBe("size desc");
    expect(toChildrenParams({}, { field: "name" }).orderBy).toBe("name");
    expect(toChildrenParams({}).orderBy).toBeUndefined();
  });
});

describe("useDrive / useItem", () => {
  it("reads the one drive", async () => {
    const { urls } = stubFetch(() => ({ id: DRIVE }));
    const { result } = renderHook(() => useDrive(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await waitFor(() => expect(result.current.data).toEqual({ id: DRIVE }));
    expect(urls[0]).toContain("/api/v1/files/drives");
  });

  it("reads one node by id", async () => {
    const { urls } = stubFetch(() => item("n1", "report.csv"));
    const { result } = renderHook(() => useItem(DRIVE, "n1"), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await waitFor(() => expect(result.current.data?.id).toBe("n1"));
    expect(urls[0]).toContain(`/api/v1/files/drives/${DRIVE}/items/n1`);
  });

  it("asks for nothing until both ids are known", async () => {
    const { urls } = stubFetch(() => item("n1", "x"));
    const { result } = renderHook(() => useItem(undefined, "n1"), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await waitFor(() => expect(result.current.fetchStatus).toBe("idle"));
    expect(urls).toHaveLength(0);
  });
});

describe("useChildren — marker paging", () => {
  it("appends the next page and stops when a page carries no marker", async () => {
    const { urls } = stubFetch((url) =>
      queryOf(url).get("marker") === "m1" ? page(["c", "d"], null) : page(["a", "b"], "m1"),
    );
    const { result } = renderHook(() => useChildren(DRIVE, ROOT), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });

    await waitFor(() => expect(result.current.data).toHaveLength(1));
    expect(flattenChildren(result.current.data).map((i) => i.id)).toEqual(["a", "b"]);
    expect(result.current.hasNextPage).toBe(true);
    expect(queryOf(urls[0]).has("marker")).toBe(false);

    await result.current.fetchNextPage();

    await waitFor(() => expect(result.current.data).toHaveLength(2));
    expect(flattenChildren(result.current.data).map((i) => i.id)).toEqual(["a", "b", "c", "d"]);
    // The final page has no marker: the caller's "load more" is gone, and no
    // request re-reads the last page for ever.
    await waitFor(() => expect(result.current.hasNextPage).toBe(false));
    expect(queryOf(urls[1]).get("marker")).toBe("m1");
    expect(urls).toHaveLength(2);
  });

  it("puts every filter and the order on the query string beside the limit", async () => {
    const { urls } = stubFetch(() => page(["a"], null));
    const { result } = renderHook(
      () =>
        useChildren(DRIVE, ROOT, {
          filters: { kind: "folder", starred: true, sizeMin: 5 },
          orderBy: { field: "mtime", direction: "desc" },
          limit: 42,
        }),
      { wrapper: wrapperFor(createQueryClient({ retry: false })) },
    );
    await waitFor(() => expect(result.current.data).toHaveLength(1));
    const q = queryOf(urls[0]);
    expect(q.get("limit")).toBe("42");
    expect(q.get("kind")).toBe("folder");
    expect(q.get("starred")).toBe("true");
    expect(q.get("sizeMin")).toBe("5");
    expect(q.get("orderBy")).toBe("mtime desc");
  });

  it("two filter sets are two cache entries, not one overwritten view", async () => {
    stubFetch((url) =>
      queryOf(url).get("kind") === "folder" ? page(["folder"], null) : page(["all"], null),
    );
    const qc = createQueryClient({ retry: false });
    const { result } = renderHook(
      () => ({
        all: useChildren(DRIVE, ROOT),
        folders: useChildren(DRIVE, ROOT, { filters: { kind: "folder" } }),
      }),
      { wrapper: wrapperFor(qc) },
    );
    await waitFor(() => expect(flattenChildren(result.current.all.data)).toHaveLength(1));
    await waitFor(() => expect(flattenChildren(result.current.folders.data)).toHaveLength(1));
    expect(flattenChildren(result.current.all.data)[0].id).toBe("all");
    expect(flattenChildren(result.current.folders.data)[0].id).toBe("folder");
  });

  it("two page sizes on one folder are two cache entries, not one shared page", async () => {
    // The browser and the soft-threshold footer read the same folder. If the
    // page size rides the request but not the key, whichever mounts first
    // decides the page the other renders.
    stubFetch((url) => {
      const size = Number(queryOf(url).get("limit"));
      return page(
        Array.from({ length: size }, (_, i) => `r${i}`),
        "more",
      );
    });
    const qc = createQueryClient({ retry: false });
    const { result } = renderHook(
      () => ({
        small: useChildren(DRIVE, ROOT, { limit: 2 }),
        large: useChildren(DRIVE, ROOT, { limit: 5 }),
      }),
      { wrapper: wrapperFor(qc) },
    );
    await waitFor(() => expect(flattenChildren(result.current.small.data)).toHaveLength(2));
    await waitFor(() => expect(flattenChildren(result.current.large.data)).toHaveLength(5));
  });

  it("two drives' listings of the same node id never share an entry", async () => {
    stubFetch((url) => page([url.includes("/d2/") ? "from-d2" : "from-d1"], null));
    const qc = createQueryClient({ retry: false });
    const { result } = renderHook(
      () => ({ one: useChildren(DRIVE, ROOT), two: useChildren("d2", ROOT) }),
      { wrapper: wrapperFor(qc) },
    );
    await waitFor(() => expect(flattenChildren(result.current.one.data)).toHaveLength(1));
    await waitFor(() => expect(flattenChildren(result.current.two.data)).toHaveLength(1));
    expect(flattenChildren(result.current.one.data)[0].id).toBe("from-d1");
    expect(flattenChildren(result.current.two.data)[0].id).toBe("from-d2");
  });
});

describe("the feeds are drive-scoped routes, not a child listing", () => {
  // Reading a feed as "the children of some node" is what left Recent, Starred
  // and Shared with me unreachable in the rail: a child listing needs a node id
  // and the drive names no node for "recent". Each of the three has a route of
  // its own; the drive is the whole address.
  it.each([
    ["recent", useRecent, "/recent"],
    ["starred", useStarred, "/starred"],
    ["sharedWithMe", useSharedWithMe, "/sharedWithMe"],
  ] as const)("%s", async (_name, hook, path) => {
    const { urls } = stubFetch(() => page(["a"], null));
    const { result } = renderHook(() => hook(DRIVE), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await waitFor(() => expect(result.current.data?.value).toHaveLength(1));
    expect(urls[0]).toContain(`/drives/${DRIVE}${path}`);
    expect(urls[0]).not.toContain("/children");
  });

  it("waits for the drive and never for a node id", () => {
    const { urls } = stubFetch(() => page(["a"], null));
    renderHook(() => useRecent(undefined), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    expect(urls).toHaveLength(0);
  });
});

describe("the facet hooks", () => {
  it("trash pages by marker and stops when the server sends none", async () => {
    const { urls } = stubFetch((url) =>
      queryOf(url).get("marker") === "t1"
        ? { value: [], nextMarker: null }
        : { value: [], nextMarker: "t1" },
    );
    const { result } = renderHook(() => useTrash(DRIVE), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await waitFor(() => expect(result.current.hasNextPage).toBe(true));
    await result.current.fetchNextPage();
    await waitFor(() => expect(result.current.hasNextPage).toBe(false));
    expect(urls[0]).toContain(`/api/v1/files/drives/${DRIVE}/trash`);
  });

  it("versions, permissions, leases and an operation each read their own route", async () => {
    const { urls } = stubFetch(() => ({ value: [] }));
    const qc = createQueryClient({ retry: false });
    const { result } = renderHook(
      () => ({
        versions: useVersions(DRIVE, "n1"),
        permissions: usePermissions(DRIVE, "n1", { effective: true }),
        leases: useLeases(DRIVE),
        operation: useOperation(DRIVE, "op1"),
      }),
      { wrapper: wrapperFor(qc) },
    );
    await waitFor(() => expect(result.current.versions.isSuccess).toBe(true));
    await waitFor(() => expect(result.current.operation.isSuccess).toBe(true));

    expect(urls.some((u) => u.includes(`/items/n1/versions`))).toBe(true);
    expect(
      urls.some(
        (u) => u.includes(`/items/n1/permissions`) && queryOf(u).get("effective") === "true",
      ),
    ).toBe(true);
    expect(
      urls.some((u) => u.includes(`/drives/${DRIVE}/leases`) && queryOf(u).get("mine") === "true"),
    ).toBe(true);
    expect(urls.some((u) => u.includes(`/operations/op1`))).toBe(true);
  });

  it("no hook polls while the event stream is up", async () => {
    stubFetch(() => ({ id: DRIVE }));
    const { result } = renderHook(() => useDrive(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.isRefetching).toBe(false);
  });
});

describe("the event map names keys the hooks cache under", () => {
  const frame = (
    type: "file_node.changed" | "file_operation.changed",
    id: string,
  ): RealtimeEventFrame =>
    ({ type, entity: "file", entity_id: id, version: 1, org_id: "o1" }) as RealtimeEventFrame;

  it("file_node.changed refreshes the node and every listing that could hold it", () => {
    const named = EVENT_KEYS["file_node.changed"](frame("file_node.changed", "n1")).map((k) =>
      JSON.stringify(k),
    );
    for (const expected of [
      keys.files.item("n1"),
      keys.files.childrenAll,
      keys.files.searchAll,
      keys.files.recent,
      keys.files.starred,
      keys.files.sharedWithMe,
      keys.files.trashAll,
      keys.files.leasesAll,
    ]) {
      expect(named).toContain(JSON.stringify(expected));
    }
  });

  it("file_operation.changed refreshes the operation and the tree it moved", () => {
    const named = EVENT_KEYS["file_operation.changed"](frame("file_operation.changed", "op1")).map(
      (k) => JSON.stringify(k),
    );
    expect(named).toContain(JSON.stringify(keys.files.operation("op1")));
    expect(named).toContain(JSON.stringify(keys.files.trashAll));
    expect(named).toContain(JSON.stringify(keys.files.childrenAll));
  });

  it("every family prefix an entry names really is a prefix of the hook's key", () => {
    // A prefix invalidation only reaches a hook's entry if the hook's key starts
    // with it — spelling a family that no hook hangs under would refresh nothing.
    const children = keys.files.children(
      DRIVE,
      ROOT,
      { orderBy: "name" },
      500,
    ) as readonly unknown[];
    expect(children.slice(0, keys.files.childrenAll.length)).toEqual([...keys.files.childrenAll]);
    const trash = keys.files.trash(DRIVE) as readonly unknown[];
    expect(trash.slice(0, keys.files.trashAll.length)).toEqual([...keys.files.trashAll]);
    const leases = keys.files.leases(DRIVE) as readonly unknown[];
    expect(leases.slice(0, keys.files.leasesAll.length)).toEqual([...keys.files.leasesAll]);
    const search = keys.files.search(DRIVE, { q: "x" }) as readonly unknown[];
    expect(search.slice(0, keys.files.searchAll.length)).toEqual([...keys.files.searchAll]);
  });
});
