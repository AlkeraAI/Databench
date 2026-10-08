// The chat page says its machine is gone the moment the server says the box's
// socket went, not when the heartbeat's window finally lapses.
//
// With the box container killed, nothing on the page changed for about forty
// seconds: the machine's status still read ready, because the heartbeat it is
// derived from turns only after four missed beats. The server now tells every
// reader of the chat when the box's socket goes and when it is back, and the
// page reads that over a status that still says ready.

import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import {
  PUBLISHER_GONE_HINT_MS,
  notePublisherFrame,
  resetPublisherLinks,
} from "@/api/realtime/publisherLinks";
import { useMachineStatus } from "@/pages/workspace/chat/ChatRuntimeLayout";

const rows = {
  machine: { machine_id: "m1", status: "ready" as string, reason: null },
  chat: { id: "c1", title: "t", machine_id: "m1", machine_status: "ready" as string },
};

function stubServer(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const body = url.includes("/machines/current") ? rows.machine : rows.chat;
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={createQueryClient({ retry: false })}>{children}</QueryClientProvider>;
}

const publisher = (state: "here" | "gone", at: string, chatId = "c1") =>
  act(() => notePublisherFrame({ t: "publisher", channel: `doc:chat:${chatId}`, state, at }));

async function mount() {
  const view = renderHook(() => useMachineStatus("c1"), { wrapper });
  await waitFor(() => expect(view.result.current.status).toBe("ready"));
  return view;
}

beforeEach(() => {
  resetPublisherLinks();
  rows.machine.status = "ready";
  rows.chat.machine_status = "ready";
  stubServer();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  resetPublisherLinks();
});

describe("useMachineStatus when the box's socket goes", () => {
  it("reads unreachable at once, and ready again when the box is back on the channel", async () => {
    const { result } = await mount();
    publisher("gone", "2026-10-04T16:00:00Z");
    expect(result.current.status).toBe("unreachable");
    publisher("here", "2026-10-04T16:00:05Z");
    expect(result.current.status).toBe("ready");
  });

  it("never lets a gone one replica noticed late outrank a here sent after it", async () => {
    const { result } = await mount();
    publisher("here", "2026-10-04T16:00:05Z");
    publisher("gone", "2026-10-04T16:00:01Z");
    expect(result.current.status).toBe("ready");
  });

  it("is about this chat's box only", async () => {
    const { result } = await mount();
    publisher("gone", "2026-10-04T16:00:00Z", "another-chat");
    expect(result.current.status).toBe("ready");
  });

  it("leaves a status that already says more alone: an asleep chat stays asleep", async () => {
    rows.chat.machine_status = "asleep";
    const view = renderHook(() => useMachineStatus("c1"), { wrapper });
    await waitFor(() => expect(view.result.current.status).toBe("asleep"));
    publisher("gone", "2026-10-04T16:00:00Z");
    expect(view.result.current.status).toBe("asleep");
  });

  it("lets the hint lapse once the heartbeat's own window would have spoken", async () => {
    const { result } = await mount();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    publisher("gone", "2026-10-04T16:00:00Z");
    expect(result.current.status).toBe("unreachable");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PUBLISHER_GONE_HINT_MS + 1);
    });
    expect(result.current.status).toBe("ready");
  });
});
