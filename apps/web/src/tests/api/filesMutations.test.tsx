// The Files write contract, driven through the real hooks:
//
//  * an optimistic rename is on screen before the server answers, and a 412
//    puts the old name back with the server's `{code}` on the error;
//  * every mutation sends an `Idempotency-Key`, and a retry of the SAME attempt
//    sends the SAME one;
//  * a success refreshes the children family through `createQueryClient`'s
//    invalidation policy — not through a hand-wired `invalidateQueries`.

import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CHILDREN_PAGE,
  isOperation,
  useCopyItem,
  useCreateFolder,
  useDuplicateItem,
  useEmptyTrash,
  useMoveItem,
  useReleaseLease,
  useRenameItem,
  useRestoreTrash,
  useStar,
  useTrashItem,
  useUndoOperation,
  withIdempotency,
  type Item,
  type Operation,
} from "@/api/files";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { chatKeys } from "@/pages/workspace/chat/chatKeys";

const DRIVE = "11111111-1111-1111-1111-111111111111";
const ITEM = "22222222-2222-2222-2222-222222222222";
const PARENT = "33333333-3333-3333-3333-333333333333";

function item(overrides: Partial<Item> = {}): Item {
  return {
    id: ITEM,
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    name: "notes.md",
    nameDisplay: "notes.md",
    nameEncoding: "utf-8",
    parentId: PARENT,
    etag: "7",
    ...overrides,
  } as Item;
}

/** One page of children, in the shape the infinite-query CACHE holds (the hook
 *  flattens it with `select`, but `setQueryData` writes this). */
function childrenCache(rows: Item[]) {
  return { pages: [{ value: rows, nextMarker: null }], pageParams: [undefined] };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Render one hook and hand back its result plus the client it ran against. */
function mountHook<T>(use: () => T, client = createQueryClient({ retry: false })) {
  const box: { current: T | null } = { current: null };
  function Probe() {
    box.current = use();
    return null;
  }
  render(
    <QueryClientProvider client={client}>
      <Probe />
    </QueryClientProvider>,
  );
  return { box, client };
}

/** What the nth fetch call actually sent: url, method and the parsed body.
 *  openapi-fetch may hand `fetch` a `Request` or a (url, init) pair. */
async function sentAt(call: number): Promise<{ url: string; method: string; body: unknown }> {
  const args = fetchMock.mock.calls[call] ?? [];
  if (args[0] instanceof Request) {
    const req = args[0] as Request;
    const text = await req.clone().text();
    return { url: req.url, method: req.method, body: text === "" ? undefined : JSON.parse(text) };
  }
  const init = (args[1] ?? {}) as RequestInit;
  const raw = typeof init.body === "string" ? init.body : "";
  return {
    url: String(args[0]),
    method: init.method ?? "GET",
    body: raw === "" ? undefined : JSON.parse(raw),
  };
}

/** An operation the server queued, in the shape a 202 answers with. */
function operation(overrides: Partial<Operation> = {}): Operation {
  return {
    id: "44444444-4444-4444-4444-444444444444",
    driveId: DRIVE,
    kind: "move",
    state: "running",
    done: 0,
    bytes: 0,
    skipped: 0,
    ...overrides,
  } as Operation;
}

/** The headers of the nth fetch call, lower-cased for lookup. openapi-fetch may
 *  hand `fetch` a `Request` or a (url, init) pair; read whichever came. */
function headersOf(call: number): Record<string, string> {
  const args = fetchMock.mock.calls[call] ?? [];
  const source =
    args[0] instanceof Request ? args[0].headers : (args[1] as RequestInit | undefined)?.headers;
  const out: Record<string, string> = {};
  new Headers(source).forEach((value, key) => {
    out[key] = value;
  });
  return out;
}

describe("optimistic rename", () => {
  it("shows the new name before the server answers, and keeps it on success", async () => {
    let release: (response: Response) => void = () => {};
    fetchMock.mockImplementation(
      () => new Promise<Response>((resolve) => (release = resolve)),
    );

    const client = createQueryClient({ retry: false });
    client.setQueryData(keys.files.item(ITEM), item());
    client.setQueryData(
      keys.files.children(DRIVE, PARENT, {}, CHILDREN_PAGE),
      childrenCache([item()]),
    );

    const { box } = mountHook(() => useRenameItem(), client);
    await act(async () => {
      box.current?.mutate({ driveId: DRIVE, itemId: ITEM, etag: "7", name: "renamed.md" });
    });

    // No response yet, and the cache already reads the new name.
    expect(client.getQueryData<Item>(keys.files.item(ITEM))?.name).toBe("renamed.md");
    const cached = client.getQueryData<{ pages: { value: Item[] }[] }>(
      keys.files.children(DRIVE, PARENT, {}, CHILDREN_PAGE),
    );
    expect(cached?.pages[0].value[0].name).toBe("renamed.md");

    await act(async () => {
      release(jsonResponse(200, item({ name: "renamed.md", etag: "8" })));
      await Promise.resolve();
    });
    await waitFor(() => expect(box.current?.isSuccess).toBe(true));
    expect(client.getQueryData<Item>(keys.files.item(ITEM))?.name).toBe("renamed.md");
  });

  it("rolls the name back on a 412 and surfaces the server's code", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(412, {
        error: { code: "files.precondition_failed", message: "someone else changed this" },
      }),
    );

    const client = createQueryClient({ retry: false });
    client.setQueryData(keys.files.item(ITEM), item());
    client.setQueryData(
      keys.files.children(DRIVE, PARENT, {}, CHILDREN_PAGE),
      childrenCache([item()]),
    );

    const { box } = mountHook(() => useRenameItem(), client);
    await act(async () => {
      box.current?.mutate({ driveId: DRIVE, itemId: ITEM, etag: "7", name: "renamed.md" });
    });
    await waitFor(() => expect(box.current?.isError).toBe(true));

    expect(box.current?.error?.status).toBe(412);
    expect(box.current?.error?.code).toBe("files.precondition_failed");
    expect(client.getQueryData<Item>(keys.files.item(ITEM))?.name).toBe("notes.md");
    const rolled = client.getQueryData<{ pages: { value: Item[] }[] }>(
      keys.files.children(DRIVE, PARENT, {}, CHILDREN_PAGE),
    );
    expect(rolled?.pages[0].value[0].name).toBe("notes.md");
  });
});

describe("optimistic move and trash", () => {
  it("takes the row out of the folder it left, and puts it back on a 412", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(412, { error: { code: "files.precondition_failed", message: "stale" } }),
    );
    const client = createQueryClient({ retry: false });
    client.setQueryData(
      keys.files.children(DRIVE, PARENT, {}, CHILDREN_PAGE),
      childrenCache([item()]),
    );

    const { box } = mountHook(() => useMoveItem(), client);
    let duringFlight: Item[] | undefined;
    await act(async () => {
      box.current?.mutate({
        driveId: DRIVE,
        itemId: ITEM,
        etag: "7",
        parentId: "44444444-4444-4444-4444-444444444444",
      });
      duringFlight = client.getQueryData<{ pages: { value: Item[] }[] }>(
        keys.files.children(DRIVE, PARENT, {}, CHILDREN_PAGE),
      )?.pages[0].value;
    });
    expect(duringFlight).toEqual([]);

    await waitFor(() => expect(box.current?.isError).toBe(true));
    const rolled = client.getQueryData<{ pages: { value: Item[] }[] }>(
      keys.files.children(DRIVE, PARENT, {}, CHILDREN_PAGE),
    );
    expect(rolled?.pages[0].value).toHaveLength(1);
  });

  it("drops a trashed row from the listing at the click", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    const client = createQueryClient({ retry: false });
    client.setQueryData(
      keys.files.children(DRIVE, PARENT, {}, CHILDREN_PAGE),
      childrenCache([item()]),
    );

    const { box } = mountHook(() => useTrashItem(), client);
    await act(async () => {
      box.current?.mutate({ driveId: DRIVE, itemId: ITEM, etag: "7" });
    });
    const listed = client.getQueryData<{ pages: { value: Item[] }[] }>(
      keys.files.children(DRIVE, PARENT, {}, CHILDREN_PAGE),
    );
    expect(listed?.pages[0].value).toEqual([]);
  });
});

describe("idempotency and If-Match", () => {
  it("sends a key on every mutation, and If-Match on the ones that need it", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, item()));

    // Each case is one hook driven with real variables. `If-Match` rides the
    // writes that name a version (PATCH / PUT / DELETE); a create names none.
    const cases: { name: string; fire: () => void; ifMatch: boolean }[] = [];

    function Probe() {
      const rename = useRenameItem();
      const star = useStar();
      const create = useCreateFolder();
      cases.length = 0;
      cases.push(
        {
          name: "rename",
          ifMatch: true,
          fire: () => rename.mutate({ driveId: DRIVE, itemId: ITEM, etag: "7", name: "x" }),
        },
        {
          name: "star",
          ifMatch: true,
          fire: () => star.mutate({ driveId: DRIVE, itemId: ITEM, etag: "7", starred: true }),
        },
        {
          name: "create folder",
          ifMatch: false,
          fire: () => create.mutate({ driveId: DRIVE, parentId: PARENT, name: "new" }),
        },
      );
      return null;
    }

    render(
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <Probe />
      </QueryClientProvider>,
    );

    for (const one of [...cases]) {
      fetchMock.mockClear();
      await act(async () => {
        one.fire();
      });
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      const sent = headersOf(0);
      expect(sent["idempotency-key"], one.name).toBeTruthy();
      expect("if-match" in sent, one.name).toBe(one.ifMatch);
    }
  });

  it("reuses the key when the same attempt is retried, and mints a new one for a new attempt", async () => {
    // A fresh Response per call: a body may only be read once.
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse(200, item())));
    const { box } = mountHook(() => useRenameItem());

    // A retry is `mutationFn` re-run with the SAME variables object, which is
    // what react-query hands it — so driving that object twice is the retry.
    const attempt = { driveId: DRIVE, itemId: ITEM, etag: "7", name: "retried.md" };
    await act(async () => {
      await box.current?.mutateAsync(attempt);
      await box.current?.mutateAsync(attempt);
    });
    expect(headersOf(1)["idempotency-key"]).toBe(headersOf(0)["idempotency-key"]);

    // A fresh press of the same button builds fresh variables: a fresh key, so
    // the server treats it as the second write it is.
    await act(async () => {
      await box.current?.mutateAsync({ driveId: DRIVE, itemId: ITEM, etag: "8", name: "again.md" });
    });
    expect(headersOf(2)["idempotency-key"]).toBeTruthy();
    expect(headersOf(2)["idempotency-key"]).not.toBe(headersOf(0)["idempotency-key"]);
  });

  it("mints one key per attempt object and never a second for the same one", () => {
    const attempt = { etag: "3" };
    expect(withIdempotency(attempt)["Idempotency-Key"]).toBe(
      withIdempotency(attempt)["Idempotency-Key"],
    );
    expect(withIdempotency({ etag: "3" })["Idempotency-Key"]).not.toBe(
      withIdempotency({ etag: "3" })["Idempotency-Key"],
    );
    expect(withIdempotency({ etag: "3" })["If-Match"]).toBe("3");
    expect(withIdempotency({})["If-Match"]).toBeUndefined();
    expect(
      withIdempotency({ headers: { "X-Alkera-Lease-Epoch": "4" } })["X-Alkera-Lease-Epoch"],
    ).toBe("4");
  });
});

describe("the invalidation policy, not hand-wired invalidation", () => {
  it("a successful mutation refreshes the children family", async () => {
    const childrenFn = vi.fn().mockResolvedValue({ value: [], nextMarker: null });
    fetchMock.mockResolvedValue(jsonResponse(200, item()));
    const client = createQueryClient({ retry: false });

    function Screen() {
      useQuery({
        queryKey: keys.files.children(DRIVE, PARENT, {}, CHILDREN_PAGE),
        queryFn: childrenFn,
      });
      const rename = useRenameItem();
      return (
        <button
          onClick={() =>
            rename.mutate({ driveId: DRIVE, itemId: ITEM, etag: "7", name: "after.md" })
          }
        >
          rename
        </button>
      );
    }

    render(
      <QueryClientProvider client={client}>
        <Screen />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(childrenFn).toHaveBeenCalledTimes(1));

    await act(async () => {
      screen.getByRole("button", { name: "rename" }).click();
    });
    // The policy re-runs the query; nothing in files.ts calls invalidateQueries.
    await waitFor(() => expect(childrenFn).toHaveBeenCalledTimes(2));
  });
});

describe("a PATCH that comes back as an operation", () => {
  it("tells a queued move apart from a node, and a node apart from an operation", async () => {
    fetchMock.mockResolvedValue(jsonResponse(202, operation()));
    const { box } = mountHook(() => useMoveItem());

    let queued: Item | Operation | null = null;
    await act(async () => {
      queued = await box.current!.mutateAsync({
        driveId: DRIVE,
        itemId: ITEM,
        etag: "7",
        parentId: "55555555-5555-5555-5555-555555555555",
      });
    });

    // A queued move has no node yet: reading a name off it would be reading a
    // field that is not there, which is what the guard exists to prevent.
    if (queued === null || !isOperation(queued)) throw new Error("expected the operation branch");
    expect((queued as Operation).state).toBe("running");

    // The negative twin: the 200 answer is a node, and must NOT take the
    // operation branch — a guard that says yes to everything is no guard.
    expect(isOperation(item())).toBe(false);
  });
});

describe("copy", () => {
  it("posts to the copy route with the destination, and takes the 202 operation back", async () => {
    fetchMock.mockResolvedValue(jsonResponse(202, operation({ kind: "copy" })));
    const { box } = mountHook(() => useCopyItem());

    let answer: Operation | null = null;
    await act(async () => {
      answer = await box.current!.mutateAsync({
        driveId: DRIVE,
        itemId: ITEM,
        parentId: PARENT,
        name: "notes copy.md",
      });
    });

    const sent = await sentAt(0);
    expect(sent.method).toBe("POST");
    expect(sent.url).toContain(`/api/v1/files/drives/${DRIVE}/items/${ITEM}/copy`);
    expect(sent.body).toEqual({
      parentId: PARENT,
      name: "notes copy.md",
      // The route's own default; sending it keeps the copy from failing on a
      // name already taken in the destination.
      conflictBehavior: "rename",
    });

    // A copy changes no version the caller holds, so it carries a key but no
    // `If-Match` — an etag here would version-check the wrong node.
    const headers = headersOf(0);
    expect(headers["idempotency-key"]).toBeTruthy();
    expect("if-match" in headers).toBe(false);

    expect(answer).not.toBeNull();
    expect((answer as unknown as Operation).id).toBe("44444444-4444-4444-4444-444444444444");
  });

  it("refreshes the Files family through the policy", async () => {
    const childrenFn = vi.fn().mockResolvedValue({ value: [], nextMarker: null });
    fetchMock.mockResolvedValue(jsonResponse(202, operation({ kind: "copy" })));
    const client = createQueryClient({ retry: false });

    function Screen() {
      useQuery({
        queryKey: keys.files.children(DRIVE, PARENT, {}, CHILDREN_PAGE),
        queryFn: childrenFn,
      });
      const copy = useCopyItem();
      return (
        <button onClick={() => copy.mutate({ driveId: DRIVE, itemId: ITEM, parentId: PARENT })}>
          copy
        </button>
      );
    }

    render(
      <QueryClientProvider client={client}>
        <Screen />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(childrenFn).toHaveBeenCalledTimes(1));

    await act(async () => {
      screen.getByRole("button", { name: "copy" }).click();
    });
    await waitFor(() => expect(childrenFn).toHaveBeenCalledTimes(2));
  });
});

describe("empty trash", () => {
  it("posts to the drive's empty route with a key, and refreshes the Files family", async () => {
    const trashFn = vi.fn().mockResolvedValue({ value: [], nextMarker: null });
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    const client = createQueryClient({ retry: false });

    function Screen() {
      useQuery({ queryKey: keys.files.trash(DRIVE), queryFn: trashFn });
      const empty = useEmptyTrash();
      return <button onClick={() => empty.mutate({ driveId: DRIVE })}>empty</button>;
    }

    render(
      <QueryClientProvider client={client}>
        <Screen />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(trashFn).toHaveBeenCalledTimes(1));

    await act(async () => {
      screen.getByRole("button", { name: "empty" }).click();
    });

    const sent = await sentAt(0);
    expect(sent.method).toBe("POST");
    expect(sent.url).toContain(`/api/v1/files/drives/${DRIVE}/trash/empty`);
    expect(headersOf(0)["idempotency-key"]).toBeTruthy();

    // The answer is not "the trash is now empty" — a held root refuses — so the
    // view re-reads rather than assuming.
    await waitFor(() => expect(trashFn).toHaveBeenCalledTimes(2));
  });
});

describe("releasing a lease", () => {
  it("names the epoch and the holder's instance, which the wire row does not carry", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    const { box } = mountHook(() => useReleaseLease());

    await act(async () => {
      await box.current!.mutateAsync({
        driveId: DRIVE,
        itemId: ITEM,
        etag: "7",
        epoch: 4,
        instanceId: "inst-9",
      });
    });

    const sent = await sentAt(0);
    expect(sent.url).toContain(`/items/${ITEM}/lease/release`);
    // The epoch fences the release: an older holder must not release the lease
    // a newer one now holds.
    expect(sent.body).toEqual({ epoch: 4, instanceId: "inst-9", final: null });
    expect(headersOf(0)["if-match"]).toBe("7");
  });
});

describe("a Files write that moves a chat row", () => {
  const CHAT = "44444444-4444-4444-4444-444444444444";

  /** The four reads a chat surface keeps live, each on the key its own hook
   *  uses. `chatKeys.chats()` and `keys.chats.all` are DISJOINT families —
   *  neither is a prefix of the other — and the folder listing stands in for
   *  the Files surface the mutation obviously owns. */
  function chatScreen(label: string, onClick: () => void) {
    const reads = {
      pageList: vi.fn().mockResolvedValue([]),
      railList: vi.fn().mockResolvedValue([]),
      chatRow: vi.fn().mockResolvedValue({ id: CHAT }),
      children: vi.fn().mockResolvedValue({ value: [], nextMarker: null }),
    };
    function Screen() {
      useQuery({ queryKey: chatKeys.chats(), queryFn: reads.pageList });
      useQuery({ queryKey: keys.chats.all, queryFn: reads.railList });
      useQuery({ queryKey: keys.chats.one(CHAT), queryFn: reads.chatRow });
      useQuery({
        queryKey: keys.files.children(DRIVE, PARENT, {}, CHILDREN_PAGE),
        queryFn: reads.children,
      });
      return <button onClick={onClick}>{label}</button>;
    }
    return { reads, Screen };
  }

  /** Mount the four reads plus one real mutation hook, let every read settle,
   *  then click. */
  async function runThrough(label: string, use: () => () => void) {
    const client = createQueryClient({ retry: false });
    let fire: () => void = () => {};
    const { reads, Screen } = chatScreen(label, () => fire());
    function Host() {
      fire = use();
      return <Screen />;
    }
    render(
      <QueryClientProvider client={client}>
        <Host />
      </QueryClientProvider>,
    );
    await waitFor(() => {
      expect(reads.pageList).toHaveBeenCalledTimes(1);
      expect(reads.railList).toHaveBeenCalledTimes(1);
      expect(reads.chatRow).toHaveBeenCalledTimes(1);
      expect(reads.children).toHaveBeenCalledTimes(1);
    });
    await act(async () => {
      screen.getByRole("button", { name: label }).click();
    });
    return reads;
  }

  it("refreshes both chat lists after a copy, not just the rail's", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { item: item(), objectId: CHAT }));
    const reads = await runThrough("copy", () => {
      const duplicate = useDuplicateItem();
      return () => duplicate.mutate({ driveId: DRIVE, itemId: ITEM });
    });

    // The entry the copy's own header, crumb trail and Chat home read. Naming
    // only `keys.chats.all` leaves it holding the list from before the copy, so
    // the chat the reader was just navigated into renders with no title.
    await waitFor(() => expect(reads.pageList).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(reads.railList).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(reads.children).toHaveBeenCalledTimes(2));
  });

  it("refreshes every chat-keyed read after trashing a chat's folder", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, operation({ kind: "trash" })));
    const reads = await runThrough("trash", () => {
      const trash = useTrashItem();
      return () => trash.mutate({ driveId: DRIVE, itemId: ITEM, etag: "7" });
    });

    // The server derives the chat's node id and its trashed flag FROM the node,
    // so the chat row really changed — and `["files"]` reaches none of these
    // three. The rail gates Share, Copy and Save as template on the id it reads
    // here, so a stale one offers all three on a folder that is gone.
    await waitFor(() => expect(reads.chatRow).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(reads.railList).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(reads.pageList).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(reads.children).toHaveBeenCalledTimes(2));
  });

  it("refreshes every chat-keyed read after restoring one from the trash", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    const reads = await runThrough("restore", () => {
      const restore = useRestoreTrash();
      return () => restore.mutate({ driveId: DRIVE, opId: "op-1" });
    });

    // The same pair flips back: the chat has a working directory again, which
    // is what its workspace pane is rooted at.
    await waitFor(() => expect(reads.chatRow).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(reads.railList).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(reads.pageList).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(reads.children).toHaveBeenCalledTimes(2));
  });

  it("refreshes every chat-keyed read after undoing an operation", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, operation({ kind: "trash" })));
    const reads = await runThrough("undo", () => {
      const undo = useUndoOperation();
      return () => undo.mutate({ driveId: DRIVE, operationId: "op-1" });
    });

    // Undoing a trash IS the restore — Cmd+Z and the undo toast both land here
    // rather than on the restore route — and undoing a copy or a rename of a
    // chat's folder moves the row just as surely. The inverse of a write that
    // reaches the chat family has to reach it back, or the rail keeps offering
    // Share, Copy and Save as template on the state the undo just left.
    await waitFor(() => expect(reads.chatRow).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(reads.railList).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(reads.pageList).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(reads.children).toHaveBeenCalledTimes(2));
  });
});
