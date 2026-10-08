// Signing out leaves nothing of the live draft lane behind for the next account
// on the same browser: no live draft held open in memory (it lingers half a
// minute after its composer goes, with the last account's text in it), and no
// edits stashed in the tab for the next page to replay.

import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { safeSessionStorage } from "@alkera/ui/storage";

import { useLogout } from "@/api/auth";
import { fireUnauthorized } from "@/api/client";
import { UnauthorizedBridge } from "@/app/boot/UnauthorizedBridge";
import { createQueryClient } from "@/api/queryClient";
import { unloadStashKey } from "@/api/realtime/crdt/channel";
import { acquireLiveDraft, closeAllLiveDrafts } from "@/api/realtime/crdt/liveDraft";
import { acquireLiveNotebook } from "@/pages/workspace/chat/workspace/notebook/liveNotebook";

import { LiveServer, loroNode } from "./liveServer";

const store = safeSessionStorage();
/** Stashes left by one person in two orgs: a sign-out erases both. */
const STASH = unloadStashKey({ userId: "usr_dana", orgId: "org_a" }, "doc:chat_draft:c1");
const OTHER_ORG_STASH = unloadStashKey({ userId: "usr_dana", orgId: "org_b" }, "doc:chat_draft:c2");

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(null, { status: 204 })),
  );
});

afterEach(() => {
  closeAllLiveDrafts();
  store.keys().forEach((key) => store.remove(key));
  vi.unstubAllGlobals();
});

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={createQueryClient()}>{children}</QueryClientProvider>;
}

describe("signing out", () => {
  it("lets go of every live notebook, so the next account never gets the last one's", async () => {
    const server = new LiveServer();
    const deps = { socket: server.socket("a"), loadLoro: () => Promise.resolve(loroNode), account: null };
    const held = acquireLiveNotebook("n1", deps);
    held.release();

    const { result } = renderHook(() => useLogout(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync();
    });

    await waitFor(() => {
      const next = acquireLiveNotebook("n1", deps);
      next.release();
      expect(next.notebook).not.toBe(held.notebook);
    });
  });

  it("lets go of every live draft and erases the edits stashed for the next page", async () => {
    const server = new LiveServer();
    const deps = { socket: server.socket("a"), loadLoro: () => Promise.resolve(loroNode), hueOf: () => 0, account: null };
    const held = acquireLiveDraft("c1", deps);
    held.release();
    store.set(STASH, "{}");
    store.set(OTHER_ORG_STASH, "{}");
    store.set("someone.else", "kept");

    const { result } = renderHook(() => useLogout(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync();
    });

    await waitFor(() => expect(store.get(STASH)).toBeNull());
    expect(store.get(OTHER_ORG_STASH)).toBeNull();
    expect(store.get("someone.else")).toBe("kept");
    const next = acquireLiveDraft("c1", deps);
    expect(next.draft).not.toBe(held.draft);
    next.release();
  });

  it("does the same when the session ends without one (an expired session's 401)", async () => {
    const server = new LiveServer();
    const deps = { socket: server.socket("a"), loadLoro: () => Promise.resolve(loroNode), hueOf: () => 0, account: null };
    const held = acquireLiveDraft("c1", deps);
    held.release();
    store.set(STASH, "{}");
    render(<UnauthorizedBridge />, { wrapper });

    act(() => fireUnauthorized());

    await waitFor(() => expect(store.get(STASH)).toBeNull());
    const next = acquireLiveDraft("c1", deps);
    expect(next.draft).not.toBe(held.draft);
    next.release();
  });
});
