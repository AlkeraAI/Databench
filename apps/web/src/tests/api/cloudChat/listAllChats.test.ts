// Every chat, not the first page of them.
//
// The rail, the chat home, the dashboard and the breadcrumb lookup all read one
// list. Stopping at the first page would leave an org's fifty-first chat out of
// all of them, and the crumb trail (which treats a chat it cannot name as a
// subagent session) would mislabel it.
//
// Driven over a stubbed `fetch` rather than a stubbed transport: the cursor
// round trip IS the mechanism under test.

import { afterEach, describe, expect, it, vi } from "vitest";

import { listAllChats } from "@/api/cloudChat/transport";
import type { DocHandle } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

afterEach(() => {
  vi.unstubAllGlobals();
});

function row(index: number) {
  return {
    id: `chat-${index}`,
    title: `Chat ${index}`,
    owner_user_id: "user-1",
    machine_id: null,
    machine_status: "none",
    created_at: "2026-09-01T00:00:00.000Z",
    updated_at: "2026-09-01T00:00:00.000Z",
    last_seq: 0,
    permission_mode: "read_only",
    attachments: [],
  };
}

/** A server holding `total` chats over pages of `size`, cursor-paged. */
function stubServer(total: number, size: number, opts: { shortPages?: boolean } = {}) {
  const fetchMock = vi.fn(async (url: string) => {
    const offset = Number(new URL(String(url)).searchParams.get("cursor") ?? "0");
    const end = Math.min(offset + size, total);
    // A page the server cut after taking it: fewer rows than the page size,
    // with more still behind the cursor. Only a missing cursor ends the walk.
    const items = Array.from({ length: end - offset }, (_, n) => row(offset + n)).slice(
      0,
      opts.shortPages ? Math.max(1, size - 2) : size,
    );
    const consumed = offset + (end - offset);
    return new Response(
      JSON.stringify({ items, next_cursor: consumed < total ? String(consumed) : null }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function idleDoc(): DocHandle<never> {
  return {
    onMessage: () => () => undefined,
    onPhase: () => () => undefined,
    getPhase: () => ({
      phase: "live",
      epoch: 1,
      seq: 0,
      peerId: "p:1",
      canWrite: false,
      pending: 0,
      error: null,
    }),
    sendOp: () => Promise.reject(new Error("a reader never writes")),
    dispose: () => undefined,
  } as unknown as DocHandle<never>;
}

describe("reading an org's chats", () => {
  it("follows the cursor to the end rather than stopping at the first page", async () => {
    const fetchMock = stubServer(120, 40);

    const page = await listAllChats();

    expect(page.items).toHaveLength(120);
    expect(page.next_cursor).toBeNull();
    expect(page.items.at(-1)?.id).toBe("chat-119");
    expect(fetchMock).toHaveBeenCalledTimes(3);
    // The first call names no cursor; each one after names the last answer's.
    const cursors = fetchMock.mock.calls.map((call) =>
      new URL(String(call[0])).searchParams.get("cursor"),
    );
    expect(cursors).toEqual([null, "40", "80"]);
  });

  it("pins no page size of its own, so the server's default is the page", async () => {
    const fetchMock = stubServer(10, 40);

    await listAllChats();

    for (const call of fetchMock.mock.calls) {
      expect(new URL(String(call[0])).searchParams.get("limit")).toBeNull();
    }
  });

  it("keeps walking past a page the server cut short", async () => {
    // The listing filters each page to what the reader may see AFTER taking it,
    // so a short page is not the end — treating it as one would hide chats
    // behind a single unreadable row.
    stubServer(120, 40, { shortPages: true });

    const page = await listAllChats();

    expect(page.items.length).toBeGreaterThan(40);
    expect(page.next_cursor).toBeNull();
  });

  it("gives the rail and the breadcrumb lookup the hundred and twentieth chat", async () => {
    stubServer(120, 40);
    const source = new CloudDataSource({
      rest: { listAllChats } as never,
      openDoc: () => idleDoc(),
      acquire: () => () => undefined,
      clientId: () => "client-1",
    });

    const chats = await source.listChats();

    expect(chats).toHaveLength(120);
    // What `useStackedCrumbs` does: a chat absent from this set is taken for a
    // subagent session and named as one.
    const named = new Set(chats.map((chat) => chat.id));
    expect(named.has("chat-119")).toBe(true);
  });
});
