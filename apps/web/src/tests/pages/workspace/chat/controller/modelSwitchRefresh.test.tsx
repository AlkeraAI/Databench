// A model switch refreshes what it changed through the portal's mutation
// policy: the chat list the chip reads its pin from, and the open chat's picker
// verdicts. Nothing else on the page is re-read, a switch refused because the
// chat moved under the reader (409) re-reads the same two before the refusal
// is shown, and a client without the policy refreshes nothing, because the
// refresh is declared on the mutation rather than wired by hand.

import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";

const { ds, reads } = vi.hoisted(() => {
  const reads = { chats: 0, options: 0, unrelated: 0 };
  return {
    reads,
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
      modelOptions: async () => {
        reads.options += 1;
        return {
          currentModelId: "claude-sonnet-5-5",
          currentEffort: null,
          canSwitch: true,
          applies: "next_turn",
          billedToOwner: false,
          options: [],
        };
      },
    },
  };
});

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
    chatCaps: () => ({ opencodeActive: false, modelCatalog: true, switchableModel: true }),
  };
});

import { chatKeys } from "@/pages/workspace/chat/chatKeys";
import { useChatStore } from "@/pages/workspace/chat/chatStore";
import { useComposerPrefs } from "@/pages/workspace/chat/controller/useComposerPrefs";

const CHAT = "c-1";

/** The composer, plus two other reads on the same page: the chat list and one
 *  the switch has nothing to do with. */
function useComposerOnPage() {
  useQuery({
    queryKey: chatKeys.chats(),
    queryFn: async () => {
      reads.chats += 1;
      return [];
    },
  });
  useQuery({
    queryKey: ["unrelated"],
    queryFn: async () => {
      reads.unrelated += 1;
      return 1;
    },
  });
  return useComposerPrefs({ chatId: CHAT, currentChat: undefined, handoff: null });
}

function mount(client: QueryClient) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(() => useComposerOnPage(), { wrapper });
}

/** Every read on the page has answered once; returns the counts from there. */
async function settled(): Promise<typeof reads> {
  await waitFor(() => {
    expect(reads.chats).toBeGreaterThan(0);
    expect(reads.options).toBeGreaterThan(0);
    expect(reads.unrelated).toBeGreaterThan(0);
  });
  // Let the mount's own re-reads (the verdicts follow the chat's history) land.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  return { ...reads };
}

beforeEach(() => {
  reads.chats = 0;
  reads.options = 0;
  reads.unrelated = 0;
});

afterEach(() => {
  useChatStore.setState({ composerPrefs: {} });
  ds.setModel.mockReset();
});

describe("a model switch", () => {
  it("re-reads the chat list and the picker's verdicts once it is accepted, and nothing else", async () => {
    ds.setModel.mockResolvedValue(undefined);
    const { result } = mount(createQueryClient({ retry: false, staleTime: Infinity }));
    const before = await settled();

    act(() => result.current.changeModel("claude-opus-5-5"));

    await waitFor(() => {
      expect(reads.chats).toBe(before.chats + 1);
      expect(reads.options).toBe(before.options + 1);
    });
    expect(ds.setModel).toHaveBeenCalledWith(CHAT, "claude-opus-5-5", undefined, "claude-sonnet-5-5");
    expect(reads.unrelated).toBe(before.unrelated);
  });

  it("re-reads both before the refusal is shown when the chat moved under the reader", async () => {
    ds.setModel.mockRejectedValue(
      new ApiError(409, { error: { code: "model_changed", message: "Someone switched this chat." } }),
    );
    const client = createQueryClient({ retry: false, staleTime: Infinity });
    let readsWhenShown: { chats: number; options: number } | null = null;
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(
      () => {
        const composer = useComposerOnPage();
        if (composer.commandError && readsWhenShown === null) {
          readsWhenShown = { chats: reads.chats, options: reads.options };
        }
        return composer;
      },
      { wrapper },
    );
    const before = await settled();

    act(() => result.current.changeModel("claude-opus-5-5"));

    await waitFor(() => expect(result.current.commandError).not.toBeNull());
    expect(readsWhenShown).toEqual({ chats: before.chats + 1, options: before.options + 1 });
    expect(result.current.commandError?.reason).toBe("Someone switched this chat.");
  });

  it("refreshes nothing on a client without the mutation policy", async () => {
    ds.setModel.mockResolvedValue(undefined);
    const { result } = mount(
      new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } }),
    );
    const before = await settled();

    act(() => result.current.changeModel("claude-opus-5-5"));
    await waitFor(() => expect(ds.setModel).toHaveBeenCalled());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });

    expect(reads.chats).toBe(before.chats);
    expect(reads.options).toBe(before.options);
  });
});
