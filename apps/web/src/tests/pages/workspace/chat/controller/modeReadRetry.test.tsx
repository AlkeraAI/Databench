// The mode read a chat makes on open, when the server cannot answer it.
//
// After a backend restart the page showed "Command failed: Couldn't read this
// chat's mode… still in progress" and kept showing it a minute after the
// backend was back: the read was made once, a transient 503 was taken for an
// answer, and nothing ever read the mode again. A read that failed for a
// passing reason is now asked again on a climbing wait (and at once when the
// browser comes back online), and the reader is told only when it keeps
// failing or was refused outright.

import { act, renderHook } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";

const { ds } = vi.hoisted(() => ({
  ds: {
    listChats: async () => [],
    listModels: async () => [],
    resolveChatDefaults: async () => ({ model: null, effort: null, permissionMode: "default" }),
    listCommands: async () => [],
    getPermissionMode: vi.fn<(chatId: string) => Promise<string | null>>(),
    setPermissionMode: async (): Promise<void> => {},
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
    chatCaps: () => ({ opencodeActive: false, modelCatalog: false }),
  };
});

import { useChatStore } from "@/pages/workspace/chat/chatStore";
import {
  MODE_READ_RETRIES,
  readFailurePasses,
  useComposerPrefs,
} from "@/pages/workspace/chat/controller/useComposerPrefs";

const busy = (): ApiError =>
  new ApiError(503, {
    error: {
      code: "db_lock_timeout",
      message: "Another change to the same data is still in progress. Please retry shortly.",
    },
  });

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={createQueryClient({ retry: false })}>{children}</QueryClientProvider>;
}

function mount() {
  return renderHook(
    () => useComposerPrefs({ chatId: "c-1", currentChat: undefined, handoff: null }),
    { wrapper },
  );
}

const advance = (ms: number): Promise<void> =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

const mode = (): string | undefined => useChatStore.getState().composerPrefs["c-1"]?.mode;

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(0);
  ds.getPermissionMode.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  useChatStore.setState({ composerPrefs: {} });
});

describe("readFailurePasses", () => {
  it.each([
    ["a 503", busy(), true],
    ["a bare 500", new ApiError(500, null), true],
    ["a 429", new ApiError(429, null), true],
    ["no answer at all", new TypeError("Failed to fetch"), true],
    ["a refusal", new ApiError(403, null), false],
    ["a chat that is not there", new ApiError(404, null), false],
  ])("%s: %s", (_why, err, expected) => {
    expect(readFailurePasses(err)).toBe(expected);
  });
});

describe("the mode read on open", () => {
  it("asks again after a transient failure and never raises the banner once it lands", async () => {
    ds.getPermissionMode
      .mockRejectedValueOnce(busy())
      .mockRejectedValueOnce(busy())
      .mockResolvedValue("plan");
    const { result } = mount();
    await advance(0);
    expect(result.current.commandError).toBeNull();
    await advance(60_000);
    expect(ds.getPermissionMode).toHaveBeenCalledTimes(3);
    expect(mode()).toBe("plan");
    expect(result.current.commandError).toBeNull();
  });

  it("tells the reader when the failures outlast the retries", async () => {
    ds.getPermissionMode.mockRejectedValue(busy());
    const { result } = mount();
    await advance(10 * 60_000);
    expect(ds.getPermissionMode).toHaveBeenCalledTimes(MODE_READ_RETRIES + 1);
    expect(result.current.commandError?.title).toBe("Couldn't read this chat's mode");
    // And stops asking: a server that stays down is not hammered.
    await advance(10 * 60_000);
    expect(ds.getPermissionMode).toHaveBeenCalledTimes(MODE_READ_RETRIES + 1);
  });

  it("tells the reader at once when the read was refused, without asking again", async () => {
    ds.getPermissionMode.mockRejectedValue(new ApiError(403, { error: { code: "forbidden", message: "No." } }));
    const { result } = mount();
    await advance(0);
    expect(result.current.commandError?.title).toBe("Couldn't read this chat's mode");
    expect(result.current.commandError?.reason).toContain("No.");
    await advance(10 * 60_000);
    expect(ds.getPermissionMode).toHaveBeenCalledTimes(1);
  });

  it("asks again the moment the browser comes back online", async () => {
    ds.getPermissionMode.mockRejectedValueOnce(new TypeError("Failed to fetch")).mockResolvedValue("plan");
    mount();
    await advance(0);
    expect(ds.getPermissionMode).toHaveBeenCalledTimes(1);
    await act(async () => {
      window.dispatchEvent(new Event("online"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(ds.getPermissionMode).toHaveBeenCalledTimes(2);
    expect(mode()).toBe("plan");
  });
});
