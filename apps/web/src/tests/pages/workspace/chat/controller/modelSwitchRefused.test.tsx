// A model switch the server refuses says what did not happen and why, in plain
// words: "Couldn't switch the model" over the server's reason, never a generic
// "Command failed".

import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";

const REASON = "This chat has reasoning from Claude Sonnet 5.5 that Claude Opus 5.5 can't read.";

const { ds } = vi.hoisted(() => ({
  ds: {
    listChats: async () => [],
    listModels: async () => [],
    resolveChatDefaults: async () => ({ model: null, effort: null, permissionMode: "default" }),
    listCommands: async () => [],
    getPermissionMode: async () => "default",
    setPermissionMode: async (): Promise<void> => {},
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
    setModel: vi.fn<(chatId: string, model: string) => Promise<void>>(),
  },
}));

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  return {
    ...real,
    chatData: () => ds,
    chatHost: () => ({
      kind: "browser",
      subscribe: () => () => {},
      account: () => ({ email: null, webAppUrl: null }),
      onAccountChange: () => () => {},
    }),
    refetchWhileErrored: () => false as const,
    refetchWhileNoChatDefault: () => false as const,
    refetchWhileErroredOrEmpty: () => false as const,
    chatCaps: () => ({ opencodeActive: false, modelCatalog: true }),
  };
});

import { useChatStore } from "@/pages/workspace/chat/chatStore";
import { useComposerPrefs } from "@/pages/workspace/chat/controller/useComposerPrefs";

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={createQueryClient({ retry: false })}>{children}</QueryClientProvider>;
}

afterEach(() => {
  useChatStore.setState({ composerPrefs: {} });
  ds.setModel.mockReset();
});

describe("a model switch the server refuses", () => {
  it("is titled by what did not happen, with the server's reason under it", async () => {
    ds.setModel.mockRejectedValue(
      new ApiError(409, { error: { code: "model_incompatible", message: REASON } }),
    );
    const { result } = renderHook(
      () => useComposerPrefs({ chatId: "c-1", currentChat: undefined, handoff: null }),
      { wrapper },
    );
    act(() => result.current.changeModel("claude-opus-5-5"));
    await waitFor(() => expect(result.current.commandError).not.toBeNull());
    expect(result.current.commandError).toEqual({ title: "Couldn't switch the model", reason: REASON });
    expect(JSON.stringify(result.current.commandError)).not.toMatch(/Command failed/);
  });
});
