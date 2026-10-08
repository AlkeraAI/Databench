// The composer's access follows a role change the moment the server makes it.
//
// After an owner raised a reader from Can view to Can edit, the live file
// editor unlocked at once (it follows the live channel's grant) while the
// composer kept saying the chat was read-only: its two rows (the chat
// folder's capabilities, the chat row's `can_send`) were re-read only by a
// later refresh. The channel's grant change now re-reads both, so the
// composer is decided from the same server answer as the editor.

import { act, cleanup, render } from "@testing-library/react";
import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";

const { live } = vi.hoisted(() => ({
  live: {
    listeners: new Set<(canWrite: boolean) => void>(),
    holds: 0,
  },
}));

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/pages/workspace/chat/data")>()),
  chatData: () => ({
    openLiveDraft: () => {
      live.holds += 1;
      return {
        draft: {
          onGrantChange: (listener: (canWrite: boolean) => void) => {
            live.listeners.add(listener);
            return () => void live.listeners.delete(listener);
          },
        },
        release: () => void (live.holds -= 1),
      };
    },
  }),
}));

import { useLiveRoleRefresh } from "@/pages/workspace/chat/useLiveRoleRefresh";

const reads = { chat: 0, node: 0, workspace: 0, otherChat: 0, listing: 0 };

function Rows({ chatId }: { chatId: string | null }) {
  useQuery({ queryKey: keys.chats.one("c1"), queryFn: () => ++reads.chat });
  useQuery({ queryKey: keys.chats.one("c2"), queryFn: () => ++reads.otherChat });
  useQuery({ queryKey: keys.files.item("nd_chat"), queryFn: () => ++reads.node });
  // The folder the share was granted on, above the chat's own.
  useQuery({ queryKey: keys.files.item("nd_workspace"), queryFn: () => ++reads.workspace });
  useQuery({
    queryKey: keys.files.childrenOf("drv", "nd_workspace"),
    queryFn: () => ++reads.listing,
  });
  useLiveRoleRefresh(chatId);
  return null;
}

async function mount(chatId: string | null = "c1") {
  const qc = createQueryClient({ retry: false });
  const view = render(
    <QueryClientProvider client={qc}>
      <Rows chatId={chatId} />
    </QueryClientProvider>,
  );
  await act(async () => {
    await Promise.resolve();
  });
  return view;
}

const grant = async (canWrite: boolean): Promise<void> => {
  await act(async () => {
    for (const listener of [...live.listeners]) listener(canWrite);
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
};

afterEach(() => {
  cleanup();
  for (const key of Object.keys(reads) as (keyof typeof reads)[]) reads[key] = 0;
  live.listeners.clear();
  live.holds = 0;
});

describe("useLiveRoleRefresh", () => {
  it("re-reads the chat row and every item's capabilities when the reader's grant changes", async () => {
    await mount();
    const before = { ...reads };
    await grant(true);
    expect(reads.chat).toBe(before.chat + 1);
    expect(reads.node).toBe(before.node + 1);
    expect(reads.workspace).toBe(before.workspace + 1);
    // Not other chats, and not listings: a role is not a change to what a
    // folder holds.
    expect(reads.otherChat).toBe(before.otherChat);
    expect(reads.listing).toBe(before.listing);
  });

  it("reads nothing again while the grant stands", async () => {
    await mount();
    const before = { ...reads };
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(reads).toEqual(before);
  });

  it("holds the chat's live channel for as long as the page shows the chat", async () => {
    const view = await mount();
    expect(live.holds).toBe(1);
    view.unmount();
    expect(live.holds).toBe(0);
    expect(live.listeners.size).toBe(0);
  });

  it("holds nothing for a page with no readable chat", async () => {
    await mount(null);
    expect(live.holds).toBe(0);
  });
});
