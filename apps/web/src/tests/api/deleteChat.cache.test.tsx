// What a delete leaves in the cache.
//
// Every `keys.chats.*` key hangs under the list's own `["chats"]` prefix, so
// the one thing the delete declares — refresh the rail — also names the row it
// just deleted, that row's messages, and that row's attachments. The policy
// AWAITS its invalidation, so each of those became a fresh GET of a chat the
// server had already removed: a 404, three retries with backoff, and a caller
// still waiting on a delete that finished at once.
//
// So the deleted chat's own entries are dropped before the policy runs, and the
// list invalidation is left to the policy exactly as it was.

import type { ReactNode } from "react";
import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useDeleteChat } from "@/api/chats";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { activityKeys, chatKeys } from "@/pages/workspace/chat/chatKeys";

const CHAT = "6f1a1c2e-6d41-4e2a-9a77-2b1f0c9d4e55";
const NEIGHBOUR = "0b9c8d7e-1a2b-3c4d-5e6f-708192a3b4c5";

let fetchSpy: ReturnType<typeof vi.fn>;

/** The wire after the chat is gone: the list still answers, and every read of
 *  the deleted chat 404s the way the server really does. */
function script() {
  fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : String(input);
    const at = new URL(url, "http://x").pathname;
    const json = (body: unknown, code = 200): Response =>
      new Response(JSON.stringify(body), {
        status: code,
        headers: { "content-type": "application/json" },
      });
    // The delete succeeds; every READ of that chat afterwards is the 404 the
    // server really gives, retry ladder and all.
    if (init?.method === "DELETE") return new Response(null, { status: 204 });
    if (at.startsWith(`/api/v1/chats/${CHAT}`)) return json({ detail: "Not found" }, 404);
    if (at.startsWith("/api/v1/chats")) return json({ items: [], next_cursor: null });
    return json({});
  });
  vi.stubGlobal("fetch", fetchSpy);
}

/** Every request the deleted chat's own id appears in, ignoring the DELETE
 *  itself — which is the one call that is supposed to name it. */
function readsOfTheDeletedChat(): string[] {
  return fetchSpy.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method !== "DELETE")
    .map(([input]) => (input instanceof Request ? input.url : String(input)))
    .filter((url) => url.includes(CHAT));
}

function harness() {
  const client = createQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

beforeEach(script);

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("deleting a chat", () => {
  it("never re-reads the chat it just deleted", async () => {
    const { client, wrapper } = harness();
    // The entries an open chat leaves behind: its row, its messages, its
    // attachments, and the layout this reader had in it.
    client.setQueryData(keys.chats.one(CHAT), { id: CHAT });
    client.setQueryData(keys.chats.messages(CHAT), { items: [] });
    client.setQueryData(keys.chats.attachments(CHAT), { items: [] });
    client.setQueryData(keys.chatWorkspace.one(CHAT), { state: null });

    const { result } = renderHook(() => useDeleteChat(), { wrapper });
    await result.current.mutateAsync(CHAT);

    expect(readsOfTheDeletedChat()).toEqual([]);
    // And nothing of it is left to be read later, either.
    for (const key of [
      keys.chats.one(CHAT),
      keys.chats.messages(CHAT),
      keys.chats.attachments(CHAT),
      keys.chatWorkspace.one(CHAT),
    ]) {
      expect(client.getQueryState(key)).toBeUndefined();
    }
  });

  it("still refreshes the lists, through the policy", async () => {
    const { client, wrapper } = harness();
    client.setQueryData(keys.chats.all, { items: [{ id: CHAT }], next_cursor: null });

    const { result } = renderHook(() => useDeleteChat(), { wrapper });
    await result.current.mutateAsync(CHAT);

    // The rail's own entry is invalidated, not removed: it is a read of what is
    // left, and it must come back without the deleted row.
    expect(client.getQueryState(keys.chats.all)?.isInvalidated).toBe(true);
  });

  it("drops the chat surface's own entries for that id", async () => {
    const { client, wrapper } = harness();
    client.setQueryData(chatKeys.turns(CHAT), ["a turn"]);
    client.setQueryData(activityKeys.decisions(CHAT), ["a decision"]);
    client.setQueryData(activityKeys.safety(CHAT), []);
    client.setQueryData(activityKeys.cost(CHAT), {});
    client.setQueryData(activityKeys.ledger(CHAT), []);

    const { result } = renderHook(() => useDeleteChat(), { wrapper });
    await result.current.mutateAsync(CHAT);

    // A deleted chat's transcript must not survive it in a cache the next
    // opener of that id would read.
    for (const key of [
      chatKeys.turns(CHAT),
      activityKeys.decisions(CHAT),
      activityKeys.safety(CHAT),
      activityKeys.cost(CHAT),
      activityKeys.ledger(CHAT),
    ]) {
      expect(client.getQueryState(key)).toBeUndefined();
    }
  });

  it("leaves every other chat's entries alone", async () => {
    const { client, wrapper } = harness();
    client.setQueryData(keys.chats.one(NEIGHBOUR), { id: NEIGHBOUR });
    client.setQueryData(chatKeys.turns(NEIGHBOUR), ["a turn"]);

    const { result } = renderHook(() => useDeleteChat(), { wrapper });
    await result.current.mutateAsync(CHAT);

    expect(client.getQueryData(keys.chats.one(NEIGHBOUR))).toEqual({ id: NEIGHBOUR });
    expect(client.getQueryData(chatKeys.turns(NEIGHBOUR))).toEqual(["a turn"]);
  });

  it("settles as soon as the server answers, not after a 404's retries", async () => {
    // An ACTIVE observer of the deleted chat's row is what made this expensive:
    // the awaited invalidation refetches it, and the retry ladder under a 404
    // is what the caller waits out. Retries are left ON here for that reason.
    const { client, wrapper } = harness();
    const readTheRow = vi.fn(async () => ({ id: CHAT }));
    renderHook(() => useQuery({ queryKey: keys.chats.one(CHAT), queryFn: readTheRow }), {
      wrapper,
    });
    await waitFor(() => expect(client.getQueryState(keys.chats.one(CHAT))?.data).toBeTruthy());
    expect(readTheRow).toHaveBeenCalledTimes(1);

    const { result } = renderHook(() => useDeleteChat(), { wrapper });
    await result.current.mutateAsync(CHAT);

    // Counted, not timed. The row is dropped before the policy invalidates
    // ["chats"], so the observer above is no longer there to be refetched: the
    // caller has nothing of this chat to wait on, whatever a loaded runner does
    // to the wall clock between the two lines.
    expect(readTheRow).toHaveBeenCalledTimes(1);
    expect(readsOfTheDeletedChat()).toEqual([]);
  });
});
