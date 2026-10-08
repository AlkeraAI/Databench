import type { ReactNode } from "react";
import { focusManager, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSaveWorkspace, useWorkspaceState } from "@/api/chats";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";

// The two reads and writes behind a chat's workspace, driven through a REAL
// query client with only `fetch` stubbed.
//
// The write is the interesting one. Saving a layout says nothing about the
// chat, the rail, the machine or the drive, so it must not make any of them
// stale — a keystroke-rate mutation under the default policy would refetch the
// whole page every half second. It declares that, and seeds the one entry it
// does know the new value of, so the next reader of the layout gets the
// server's answer without a round trip.

const CHAT = "chat-4b2";
const NODE = "00000000-0000-4000-8000-000000000001";

let fetchSpy: ReturnType<typeof vi.fn>;

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: () => Promise.resolve(body) } as unknown as Response;
}

beforeEach(() => {
  fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function harness(queryDefaults?: Parameters<typeof createQueryClient>[0]) {
  const client = createQueryClient(queryDefaults);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

describe("the chat workspace hooks", () => {
  it("reads this reader's layout from the chat's own workspace path", async () => {
    const answer = { state: { tabs: [], active_tab_id: null }, updated_at: null };
    fetchSpy.mockResolvedValue(ok(answer));
    const { wrapper } = harness();

    const { result } = renderHook(() => useWorkspaceState(CHAT), { wrapper });

    await waitFor(() => expect(result.current.data).toEqual(answer));
    expect(String(fetchSpy.mock.calls[0]?.[0])).toContain(`/api/v1/chats/${CHAT}/workspace`);
  });

  it("re-reads the layout when the reader comes back to the tab", async () => {
    // Nothing on the event stream announces a layout change, so the other tab
    // of this same reader is invisible until this one is looked at again.
    const first = { state: { tabs: [], active_tab_id: null }, updated_at: null };
    const second = {
      state: { tabs: [{ id: "t1", kind: "file", node_id: NODE, name: "q3.html" }], active_tab_id: "t1" },
      updated_at: "2026-09-16T10:00:00Z",
    };
    fetchSpy.mockResolvedValueOnce(ok(first)).mockResolvedValue(ok(second));
    // Staleness is not the mechanism under test; the portal's default of NOT
    // refetching on focus is, and this read opts out of it.
    const { wrapper } = harness({ staleTime: 0 });

    const { result } = renderHook(() => useWorkspaceState(CHAT), { wrapper });
    await waitFor(() => expect(result.current.data).toEqual(first));

    focusManager.setFocused(false);
    focusManager.setFocused(true);

    await waitFor(() => expect(result.current.data).toEqual(second));
    focusManager.setFocused(undefined);
  });

  it("asks for nothing until there is a chat to ask about", () => {
    const { wrapper } = harness();

    renderHook(() => useWorkspaceState(undefined), { wrapper });

    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("leaves every other cache entry alone and seeds the layout it just wrote", async () => {
    const stored = {
      state: { tabs: [{ id: "t1", kind: "file", node_id: NODE, name: "q3.html" }], active_tab_id: "t1" },
      updated_at: "2026-09-16T10:00:00Z",
    };
    fetchSpy.mockResolvedValue(ok(stored));
    const { client, wrapper } = harness();
    // A neighbouring entry that a blanket invalidation would have marked stale.
    client.setQueryData(keys.chats.all, [{ id: CHAT }]);
    const before = client.getQueryState(keys.chats.all)?.isInvalidated;

    const { result } = renderHook(() => useSaveWorkspace(), { wrapper });
    await result.current.mutateAsync({ chatId: CHAT, state: { tabs: [], active_tab_id: null } });

    expect(client.getQueryData(keys.chatWorkspace.one(CHAT))).toEqual(stored);
    expect(client.getQueryState(keys.chats.all)?.isInvalidated).toBe(before);
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ state: { tabs: [], active_tab_id: null } });
  });

  it("sends the write in a way that survives the page closing when asked to", async () => {
    fetchSpy.mockResolvedValue(ok({ state: { tabs: [], active_tab_id: null }, updated_at: null }));
    const { wrapper } = harness();

    const { result } = renderHook(() => useSaveWorkspace(), { wrapper });
    await result.current.mutateAsync({
      chatId: CHAT,
      state: { tabs: [], active_tab_id: null },
      keepalive: true,
    });

    expect((fetchSpy.mock.calls[0]?.[1] as RequestInit).keepalive).toBe(true);
  });

  it("raises the server's refusal instead of resolving with it", async () => {
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 422,
      json: () => Promise.resolve({ detail: { code: "workspace_state_too_large", message: "too big" } }),
    } as unknown as Response);
    const { wrapper } = harness();

    const { result } = renderHook(() => useSaveWorkspace(), { wrapper });

    await expect(
      result.current.mutateAsync({ chatId: CHAT, state: { tabs: [], active_tab_id: null } }),
    ).rejects.toMatchObject({ status: 422, code: "workspace_state_too_large" });
  });
});
