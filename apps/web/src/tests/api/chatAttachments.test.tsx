import type { ReactNode } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  useChatAttachments,
  useLinkAttachment,
  type ChatAttachmentRead,
} from "@/api/chats";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";

// The two attachment hooks driven through a REAL query client with only `fetch`
// stubbed. What is pinned here is the wire the page cannot see: the link is a
// POST of `{nodeId}` to the chat's own attachments path, the list is a
// per-caller read keyed by the chat, and a completed link makes the list stale
// through the mutation-cache policy rather than by hand.

const CHAT = "chat-9f2";
const NODE = "1f0b6a44-0000-4000-8000-00000000abcd";
const LINKED: ChatAttachmentRead = {
  nodeId: NODE,
  name: "twenty-mb.json",
  size: 20_971_520,
  mime: "application/json",
  state: "available",
};

let fetchSpy: ReturnType<typeof vi.fn>;

function jsonOk(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

beforeEach(() => {
  fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function harness() {
  const client = createQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

function urlOf(call: unknown[]): string {
  const first = call[0];
  return typeof first === "string" ? first : String(first);
}

describe("the chat's attachment hooks", () => {
  it("reads the linked nodes this caller can see, keyed by the chat", async () => {
    // The server answers `ChatAttachmentList`: the rows under `items`, never a
    // bare array. The hook hands the page the rows.
    fetchSpy.mockResolvedValue(jsonOk({ items: [LINKED] }));
    const { wrapper, client } = harness();

    const { result } = renderHook(() => useChatAttachments(CHAT), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toEqual([LINKED]);
    expect(urlOf(fetchSpy.mock.calls[0] as unknown[])).toContain(
      `/api/v1/chats/${CHAT}/attachments`,
    );
    // The cookie is the credential; no token ever rides the URL.
    expect(
      (fetchSpy.mock.calls[0] as unknown[])[1] as RequestInit,
    ).toMatchObject({ credentials: "include" });
    // Keyed under this chat, so another chat's answer can never be served here.
    expect(
      client.getQueryData(keys.chats.attachments(CHAT)),
    ).toEqual([LINKED]);
  });

  it("unwraps the envelope the generated document declares, not a bare array", async () => {
    // `GET .../attachments` answers `ChatAttachmentList` — `{items: [...]}` — so a
    // reader that hands the body straight through gives the page an object where it
    // expects a list, and every `.map` over it throws at runtime.
    fetchSpy.mockResolvedValue(jsonOk({ items: [LINKED] }));
    const { wrapper } = harness();

    const { result } = renderHook(() => useChatAttachments(CHAT), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(Array.isArray(result.current.data)).toBe(true);
    expect(result.current.data?.map((row) => row.nodeId)).toEqual([NODE]);
  });

  it("reads an empty envelope as an empty list, never as nothing at all", async () => {
    // An empty answer is a real answer: the caller may read the chat and none of
    // its nodes. It must be a list, so the page renders "no attachments" rather
    // than a spinner over `undefined`.
    fetchSpy.mockResolvedValue(jsonOk({ items: [] }));
    const { wrapper } = harness();

    const { result } = renderHook(() => useChatAttachments(CHAT), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toEqual([]);
  });

  it("asks for nothing until there is a chat to ask about", () => {
    const { wrapper } = harness();
    const { result } = renderHook(() => useChatAttachments(undefined), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("links a node by posting its id to the chat, and the list goes stale", async () => {
    fetchSpy.mockResolvedValue(jsonOk(LINKED));
    const { wrapper, client } = harness();
    // A list already read, so the invalidation has something to mark.
    client.setQueryData(keys.chats.attachments(CHAT), []);

    const { result } = renderHook(() => useLinkAttachment(), { wrapper });
    await result.current.mutateAsync({ chatId: CHAT, nodeId: NODE });

    const post = fetchSpy.mock.calls.find(
      (call) => (call[1] as RequestInit | undefined)?.method === "POST",
    );
    expect(post).toBeTruthy();
    expect(urlOf(post as unknown[])).toContain(`/api/v1/chats/${CHAT}/attachments`);
    // The body names the node and nothing else — the server decides whether
    // this caller may read it, so the request never carries the file's facts.
    expect(JSON.parse(String((post?.[1] as RequestInit).body))).toEqual({
      nodeId: NODE,
    });

    await waitFor(() =>
      expect(
        client.getQueryState(keys.chats.attachments(CHAT))?.isInvalidated,
      ).toBe(true),
    );
  });

  it("a refused link surfaces the server's status rather than a silent success", async () => {
    fetchSpy.mockResolvedValue({
      ok: false,
      status: 404,
      json: () => Promise.resolve({ detail: "not found" }),
    } as unknown as Response);
    const { wrapper } = harness();

    const { result } = renderHook(() => useLinkAttachment(), { wrapper });

    await expect(
      result.current.mutateAsync({ chatId: CHAT, nodeId: NODE }),
    ).rejects.toMatchObject({ status: 404 });
  });
});
