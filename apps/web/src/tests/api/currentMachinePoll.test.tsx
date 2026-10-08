// The machine banner must never be able to lie for as long as a tab stays open.
//
// `compute_machine.changed` invalidates the key and is the fast path, but that
// frame travels over the same connection a dead box may have taken down with
// it. The poll is the floor under it: whatever else fails, what the banner says
// is at most one interval stale. Pinned on the hook itself, so a page that
// happens to read a second polled query cannot stand in for it.

import { renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MACHINE_POLL_MS, useCurrentMachine } from "@/api/chats";
import { createQueryClient } from "@/api/queryClient";

let status: string;
let reads: number;

function scriptFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/machines/current")) {
        reads += 1;
        return new Response(
          JSON.stringify({ machine_id: "m1", status, name: "box-1", reason: null }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
}

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={createQueryClient({ retry: false })}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  status = "ready";
  reads = 0;
  scriptFetch();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("the org machine's live state", () => {
  it("re-reads on its own within the poll, with no event to prompt it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { result } = renderHook(() => useCurrentMachine(), { wrapper });
    await waitFor(() => expect(result.current.data?.status).toBe("ready"));
    const afterFirst = reads;

    // The box goes quiet. Nothing invalidates the key — the only thing that can
    // correct the banner is the hook re-reading.
    status = "unreachable";
    await vi.advanceTimersByTimeAsync(MACHINE_POLL_MS + 1_000);

    await waitFor(() => expect(result.current.data?.status).toBe("unreachable"));
    expect(reads).toBeGreaterThan(afterFirst);
  });

  it("polls on the drill's floor: the reachability sweep plus this interval is under a minute", () => {
    expect(MACHINE_POLL_MS).toBeLessThanOrEqual(20_000);
  });
});
