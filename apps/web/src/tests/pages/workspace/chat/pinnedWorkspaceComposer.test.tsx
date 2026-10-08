// A chat of a workspace pinned to an org machine runs on that machine, not
// where the org's other chats go. An org with nothing serving its regular
// chats must still be able to start a chat in a workspace whose machine is up:
// the status the composer waits on asks for the workspace the chat will be
// made in.

import { renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { useMachineStatus } from "@/pages/workspace/chat/ChatRuntimeLayout";

const PINNED = "ws-pinned";

function script(chat: Record<string, unknown> | null): string[] {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const raw = input instanceof Request ? input.url : String(input);
      const url = new URL(raw, "http://x");
      const json = (payload: unknown): Response =>
        new Response(JSON.stringify(payload), { status: 200, headers: { "content-type": "application/json" } });
      if (url.pathname === "/api/v1/machines/current") {
        const workspace = url.searchParams.get("workspace_id");
        asked.push(workspace ?? "");
        // The org's regular chats have nowhere to run; the pinned workspace's machine is up.
        return workspace === PINNED
          ? json({ machine_id: "alloc-lab", status: "ready", name: "Lab A", reason: null })
          : json({ machine_id: null, status: "none", name: "", reason: null });
      }
      if (chat && /\/api\/v1\/chats\/[^/?]+$/.test(url.pathname)) return json(chat);
      return new Response("{}", { status: 404 });
    }),
  );
  return asked;
}

function wrapper({ children }: { children: ReactNode }) {
  const client = createQueryClient({ retry: false });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the composer's machine for a pinned workspace", () => {
  it("a new chat aimed at a pinned workspace waits on that workspace's machine", async () => {
    const asked = script(null);
    const { result } = renderHook(() => useMachineStatus(undefined, PINNED), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(asked).toContain(PINNED);
  });

  it("a new chat with no workspace named reads the org's machine", async () => {
    script(null);
    const { result } = renderHook(() => useMachineStatus(undefined, null), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("none"));
  });

  it("an unplaced chat of a pinned workspace reads its workspace's machine", async () => {
    const asked = script({
      id: "c1",
      title: "Run",
      owner_user_id: "u1",
      workspace_id: PINNED,
      machine_id: null,
      machine_status: "none",
      machine_refusal_reason: null,
      created_at: "2026-10-05T12:00:00Z",
      updated_at: "2026-10-05T12:00:00Z",
      last_seq: 0,
    });
    const { result } = renderHook(() => useMachineStatus("c1"), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(asked).toContain(PINNED);
  });
});
