// A mode switch the server refuses puts the chip back where it was and says
// why. Before, the chip kept stating the refused stance until a reload, so the
// reader believed the agent would plan when it would not.

import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

// The stance the server holds: a refused switch leaves it, an accepted one
// moves it, and the composer's own sync reads it back.
const { ds, server } = vi.hoisted(() => ({
  server: { mode: "default" },
  ds: {
    listChats: async () => [],
    listModels: async () => [],
    resolveChatDefaults: async () => ({ model: null, effort: null, permissionMode: "default" }),
    listCommands: async () => [],
    getPermissionMode: vi.fn(async () => server.mode),
    setPermissionMode: vi.fn(async (_chatId: string, _mode: string): Promise<void> => {}),
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
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

const REFUSAL = "You need edit access to this chat to send a message.";

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={createQueryClient({ retry: false })}>{children}</QueryClientProvider>;
}

async function mount() {
  const view = renderHook(
    () =>
      useComposerPrefs({
        chatId: "c-1",
        currentChat: { id: "c-1", title: "t", updatedAt: "", permissionMode: "default" },
        handoff: null,
      }),
    { wrapper },
  );
  // The composer reads the chat's stance back from the server on open; let
  // that land before anything is switched.
  await waitFor(() => expect(ds.getPermissionMode).toHaveBeenCalled());
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  return view;
}

afterEach(() => {
  useChatStore.setState({ composerPrefs: {} });
  ds.setPermissionMode.mockReset();
  server.mode = "default";
});

describe("a permission-mode switch", () => {
  it("reverts the chip and states the refusal when the server says no", async () => {
    ds.setPermissionMode.mockRejectedValueOnce(new Error(REFUSAL));
    const { result } = await mount();
    act(() => result.current.changeMode("plan"));
    expect(result.current.composerMode).toBe("plan");

    await waitFor(() => expect(result.current.composerMode).toBe("default"));
    expect(result.current.commandError?.reason).toContain(REFUSAL);
  });

  it("keeps the new stance when the server agrees", async () => {
    ds.setPermissionMode.mockImplementationOnce(async (_chatId, mode) => {
      server.mode = mode;
    });
    const { result } = await mount();
    act(() => result.current.changeMode("plan"));
    await waitFor(() => expect(ds.setPermissionMode).toHaveBeenCalledWith("c-1", "plan"));
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(result.current.composerMode).toBe("plan");
    expect(result.current.commandError).toBeNull();
  });
});
