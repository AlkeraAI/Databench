// The open chat marks itself read as the transcript grows: once the sequence
// settles, never twice for the same sequence, never while the tab is hidden,
// and the rail's row follows the server's answer.

import { act, render } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useMarkReadOnView, type ChatSessionRead } from "@/api/chats";
import { MARK_READ_DEBOUNCE_MS } from "@/lib/limits";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";

function Probe({ chatId, lastSeq, unread = true }: { chatId: string; lastSeq: number; unread?: boolean }) {
  useMarkReadOnView(chatId, lastSeq, unread);
  return null;
}

let visibility: DocumentVisibilityState = "visible";
const posted: { url: string; body: unknown }[] = [];

beforeEach(() => {
  vi.useFakeTimers();
  posted.length = 0;
  visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/read")) {
        posted.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
        return new Response(JSON.stringify({ chat_id: "c1", unread: false, needs_you: false }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ items: [], next_cursor: null }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function settle(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("useMarkReadOnView", () => {
  it("marks read once the sequence settles, and only forward", async () => {
    const qc = createQueryClient();
    qc.setQueryData(keys.chats.all, {
      items: [{ id: "c1", unread: true, needs_you: false } as ChatSessionRead],
      next_cursor: null,
    });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <Probe chatId="c1" lastSeq={3} />
      </QueryClientProvider>,
    );
    rerender(
      <QueryClientProvider client={qc}>
        <Probe chatId="c1" lastSeq={5} />
      </QueryClientProvider>,
    );
    await settle(MARK_READ_DEBOUNCE_MS - 1);
    expect(posted).toEqual([]);
    await settle(1);
    expect(posted).toEqual([{ url: expect.stringContaining("/api/v1/chats/c1/read"), body: { seq: 5 } }]);
    const list = qc.getQueryData<{ items: ChatSessionRead[] }>(keys.chats.all);
    expect(list?.items[0]?.unread).toBe(false);

    // The same sequence again (a re-read of the row) asks nothing more.
    rerender(
      <QueryClientProvider client={qc}>
        <Probe chatId="c1" lastSeq={5} />
      </QueryClientProvider>,
    );
    await settle(MARK_READ_DEBOUNCE_MS * 2);
    expect(posted).toHaveLength(1);
  });

  it("asks nothing for a chat that opened read, until something new is written", async () => {
    const qc = createQueryClient();
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <Probe chatId="c1" lastSeq={4} unread={false} />
      </QueryClientProvider>,
    );
    await settle(MARK_READ_DEBOUNCE_MS * 2);
    expect(posted).toEqual([]);
    rerender(
      <QueryClientProvider client={qc}>
        <Probe chatId="c1" lastSeq={6} unread={false} />
      </QueryClientProvider>,
    );
    await settle(MARK_READ_DEBOUNCE_MS);
    expect(posted).toEqual([{ url: expect.stringContaining("/api/v1/chats/c1/read"), body: { seq: 6 } }]);
  });

  it("waits while the tab is hidden and marks when the reader comes back", async () => {
    visibility = "hidden";
    const qc = createQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <Probe chatId="c1" lastSeq={7} />
      </QueryClientProvider>,
    );
    await settle(MARK_READ_DEBOUNCE_MS * 2);
    expect(posted).toEqual([]);
    visibility = "visible";
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(posted).toEqual([{ url: expect.stringContaining("/api/v1/chats/c1/read"), body: { seq: 7 } }]);
  });
});
