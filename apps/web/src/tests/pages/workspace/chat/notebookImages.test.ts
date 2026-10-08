// Where an output a notebook holds is read from, once the agent shows it in
// the chat: the notebook's own blob route, on the node the path names, with
// what the route answered told apart (the bytes, a refusal, an output gone).

import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { installChatRuntime, resetChatRuntime, type ChatDataSource, type ChatHost } from "@/pages/workspace/chat/data";
import type { ChatFileLocation, ChatFilesPort } from "@/pages/workspace/chat/data/chatFiles";
import { useNotebookChartSpec, useNotebookImage } from "@/pages/workspace/chat/notebookImages";

const CHAT = "11111111-1111-1111-1111-111111111111";
const SHA = "50c4fb6e076d592e5359af9d1549d46d3afa268daacd09a7114c161dfb256048";
const PATH = "work/analysis.alknb.py";
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3]);

function found(over: Partial<ChatFileLocation> = {}): ChatFileLocation {
  return { nodeId: "nb node", driveId: "drive/1", parentId: "root", name: "analysis.alknb.py", path: PATH, kind: "file", ...over };
}

function install(port: Partial<ChatFilesPort> | undefined): void {
  installChatRuntime({
    source: { chatFiles: port } as unknown as ChatDataSource,
    host: {} as unknown as ChatHost,
  });
}

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: createQueryClient({ retry: false }) }, children);
}

function answer(status: number, body: BodyInit | null = null) {
  const fetchMock = vi.fn(async () => new Response(status === 204 ? null : body, { status }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  resetChatRuntime();
  vi.unstubAllGlobals();
});

describe("a shown notebook image", () => {
  it("is the bytes the blob route of the notebook the path names answers", async () => {
    const locate = vi.fn(async () => found());
    install({ locate });
    const fetchMock = answer(200, PNG);

    const { result } = renderHook(() => useNotebookImage(CHAT), { wrapper });
    const read = await result.current?.(PATH, SHA);

    expect(locate).toHaveBeenCalledWith(CHAT, PATH);
    expect(read?.kind).toBe("ready");
    const bytes = read?.kind === "ready" ? new Uint8Array(await read.value.arrayBuffer()) : null;
    expect(bytes).toEqual(PNG);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(new RegExp(`/api/v1/notebooks/drive%2F1/nb%20node/blobs/${SHA}$`));
    // Same-origin credentials: the cookie reaches the blob route, never the
    // content host the route redirects a stored file to.
    expect(init.credentials).toBe("same-origin");
  });

  it("is read once per session: the bytes a hash names never change", async () => {
    install({ locate: vi.fn(async () => found()) });
    const fetchMock = answer(200, PNG);

    const { result } = renderHook(() => useNotebookImage(CHAT), { wrapper });
    await result.current?.(PATH, SHA);
    await result.current?.(PATH, SHA);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["no notebook this reader can open is at the path", null, 200],
    ["the path names a folder", found({ kind: "folder" }), 200],
    ["the route refuses the reader", found(), 403],
  ])("is refused when %s", async (_case, located, status) => {
    install({ locate: vi.fn(async () => located) });
    answer(status, PNG);

    const { result } = renderHook(() => useNotebookImage(CHAT), { wrapper });

    expect(await result.current?.(PATH, SHA)).toEqual({ kind: "refused" });
  });

  it("is gone when the notebook no longer holds it", async () => {
    install({ locate: vi.fn(async () => found()) });
    answer(404, "{}");

    const { result } = renderHook(() => useNotebookImage(CHAT), { wrapper });

    expect(await result.current?.(PATH, SHA)).toEqual({ kind: "gone" });
  });

  it("fails, rather than claiming a refusal, when the server errs", async () => {
    install({ locate: vi.fn(async () => found()) });
    answer(500, "{}");

    const { result } = renderHook(() => useNotebookImage(CHAT), { wrapper });

    await expect(result.current?.(PATH, SHA)).rejects.toThrow();
  });

  it.each([
    ["the shell cannot look a path up", { resolveUrl: vi.fn() }, CHAT],
    ["the shell has no chat files", undefined, CHAT],
    ["there is no chat yet", { locate: vi.fn(async () => found()) }, null],
  ])("has no reader when %s", (_case, port, chatId) => {
    install(port);

    const { result } = renderHook(() => useNotebookImage(chatId), { wrapper });

    expect(result.current).toBeUndefined();
  });
});

describe("a stored notebook chart", () => {
  it("is the spec the notebook's blob route answers for that hash", async () => {
    install({ locate: vi.fn(async () => found()) });
    answer(200, JSON.stringify({ mark: "bar" }));

    const { result } = renderHook(() => useNotebookChartSpec(CHAT), { wrapper });

    expect(await result.current?.(PATH, SHA)).toEqual({ kind: "ready", value: { mark: "bar" } });
  });

  it.each([
    ["no file is at the path", null, 200, { kind: "refused" }],
    ["the notebook no longer holds it", found(), 404, { kind: "gone" }],
  ])("says why there is none when %s", async (_case, located, status, expected) => {
    install({ locate: vi.fn(async () => located) });
    answer(status, "{}");

    const { result } = renderHook(() => useNotebookChartSpec(CHAT), { wrapper });

    expect(await result.current?.(PATH, SHA)).toEqual(expected);
  });
});
