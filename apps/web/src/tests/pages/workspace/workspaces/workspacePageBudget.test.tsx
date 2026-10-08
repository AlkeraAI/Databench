// What a workspace's page costs the server over a minute.
//
// QA measured an idle workspace page at about ninety requests a minute and
// tabs of it tripping the API's rate limiter. Idle, it now asks only on the
// shared polling floor; and with a box writing a file into one of its chats
// every second (a frame per save), the file preview re-reads on its throttle,
// not once per frame, and nothing else on the page re-reads at all.

import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import { createQueryClient } from "@/api/queryClient";
import { MACHINE_POLL_MS } from "@/lib/limits";
import { WORKSPACES_REFRESH_MS } from "@/pages/workspace/workspaces/useWorkspaceRail";

vi.mock("@/pages/workspace/chat/ChatSurface", () => ({ ChatSurface: () => null }));
vi.mock("@/pages/workspace/workspaces/useWorkspacePresence", () => ({ useWorkspaceViewers: () => [] }));

import { WorkspaceRoute } from "@/pages/workspace/chat/ChatPage";

const W = "22222222-2222-4222-8222-222222222222";
const ME = "u-me";
const WORK = "node-work";

const workspace = {
  id: W,
  title: "Q4 forecast",
  kind: "project",
  layout: "native",
  owner_user_id: ME,
  version: 1,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
  files_node_id: "node-ws",
  files_drive_id: "drv",
  working_node_id: "node-ws-files",
  machine_status: "none",
  can_add_chat: true,
  can_rename: true,
  can_delete: true,
};
const chat = {
  id: "aaaaaaaa-0000-4000-8000-000000000001",
  title: "Revenue model",
  owner_user_id: ME,
  machine_id: "m1",
  machine_status: "ready",
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
  last_seq: 1,
  files_node_id: WORK,
  workspace_id: W,
};

function countingFetch(): { calls: () => string[] } {
  const seen: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), "http://x");
      seen.push(url.pathname);
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
      const at = url.pathname;
      if (at === "/api/v1/auth/me") return json({ id: ME, email: "me@x" });
      if (at === "/api/v1/config") return json({ workspaces_multi_chat: true });
      if (at === "/api/v1/workspaces") return json({ items: [workspace], next_cursor: null });
      if (at === "/api/v1/workspaces/main") return json({ ...workspace, id: "main", kind: "main" });
      if (at === "/api/v1/chats") return json({ items: [chat], next_cursor: null });
      if (at === "/api/v1/files/drives") return json({ id: "drv", orgId: "o", rootId: "r", quotaBytes: 1 });
      if (at === "/api/v1/machines/current") return json({ machine_id: "m1", status: "ready", name: "b", reason: null });
      if (at.endsWith("/children")) {
        return json({ value: [{ id: "f1", name: "ticks.log", nameDisplay: "ticks.log", kind: "file" }], nextMarker: null });
      }
      if (at.includes("/items/")) return json({ id: WORK, name: "Revenue model.alkerachat", kind: "folder" });
      return json({});
    }),
  );
  const counted = (p: string) => p.startsWith("/api/v1/") && p !== "/api/v1/ws/tickets";
  return { calls: () => seen.filter(counted) };
}

function renderPage(client: QueryClient) {
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/workspaces/${W}`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId" element={<WorkspaceRoute />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** A box landing one more save into the chat's folder. */
function save(n: number): RealtimeEventFrame {
  return {
    type: "file_node.changed",
    entity: "file_node",
    entity_id: `file-${n}`,
    version: n,
    org_id: "org",
    drive_id: "drv",
    parent_id: WORK,
    reason: "live_saved",
  };
}

afterEach(() => {
  cleanup();
  resetFrameBus();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("a workspace's page over a minute", () => {
  it("asks only on the polling floor while nothing happens", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const wire = countingFetch();
    renderPage(createQueryClient());
    await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    await vi.advanceTimersByTimeAsync(2_000);
    const opened = wire.calls().length;
    await vi.advanceTimersByTimeAsync(60_000);
    const minute = wire.calls().slice(opened);
    // The machine's state on its floor and the spare heartbeat; nothing else.
    expect(minute.filter((p) => p !== "/api/v1/machines/current" && p !== "/api/v1/chats/spare")).toEqual([]);
    expect(minute.filter((p) => p === "/api/v1/machines/current")).toHaveLength(60_000 / MACHINE_POLL_MS);
  });

  it("re-reads the workspace list at once on a person's change, then once per window", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const wire = countingFetch();
    renderPage(createQueryClient());
    await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    await vi.advanceTimersByTimeAsync(2_000);
    const lists = (): number => wire.calls().filter((p) => p === "/api/v1/workspaces").length;
    const before = lists();
    // A share made or revoked somewhere else: a node frame a person caused,
    // outside any folder the page previews.
    const shared = (n: number): RealtimeEventFrame => ({
      ...save(n),
      parent_id: "node-elsewhere",
      reason: "share_changed",
    });
    act(() => publishFrame(shared(0)));
    await vi.advanceTimersByTimeAsync(100);
    expect(lists()).toBe(before + 1);
    for (let n = 1; n < 60; n += 1) {
      act(() => publishFrame(shared(n)));
      await vi.advanceTimersByTimeAsync(1_000);
    }
    const reads = lists() - before;
    expect(reads).toBeGreaterThanOrEqual(60_000 / WORKSPACES_REFRESH_MS);
    expect(reads).toBeLessThanOrEqual(60_000 / WORKSPACES_REFRESH_MS + 1);
  });

  it("does not re-read the workspace list for a box's saves", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const wire = countingFetch();
    renderPage(createQueryClient());
    await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    await vi.advanceTimersByTimeAsync(2_000);
    const lists = (): number => wire.calls().filter((p) => p === "/api/v1/workspaces").length;
    const before = lists();
    for (let n = 0; n < 30; n += 1) {
      act(() => publishFrame({ ...save(n), parent_id: "node-elsewhere" }));
      await vi.advanceTimersByTimeAsync(1_000);
    }
    expect(lists()).toBe(before);
  });
});
