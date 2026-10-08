// Opening a workspace with no chat open wakes it: the page asks the server
// once, and not at all for a workspace whose chats already read as up.

import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { resetFrameBus } from "@/api/events/frameBus";
import { createQueryClient } from "@/api/queryClient";

vi.mock("@/pages/workspace/chat/ChatSurface", () => ({ ChatSurface: () => null }));
vi.mock("@/pages/workspace/workspaces/useWorkspacePresence", () => ({ useWorkspaceViewers: () => [] }));

import { WorkspaceRoute } from "@/pages/workspace/chat/ChatPage";

const W = "22222222-2222-4222-8222-222222222222";
const ME = "u-me";

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
  last_activity_at: "2026-10-01T00:00:00Z",
  last_seq: 1,
  files_node_id: "node-work",
  workspace_id: W,
};

/** Serve the page with its one chat in `sessionState`; returns the wakes asked. */
function script(sessionState: string): string[] {
  const wakes: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const at = new URL(request ? request.url : String(input), "http://x").pathname;
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
      if (at.endsWith("/wake")) {
        wakes.push(`${request?.method ?? init?.method ?? "GET"} ${at}`);
        return json({ outcome: "waking" });
      }
      if (at === "/api/v1/auth/me") return json({ id: ME, email: "me@x" });
      if (at === "/api/v1/config") return json({ workspaces_multi_chat: true });
      if (at === "/api/v1/workspaces") return json({ items: [workspace], next_cursor: null });
      if (at === "/api/v1/workspaces/main") return json({ ...workspace, id: "main", kind: "main" });
      // The page reads the server's status fact; session_state rides along as the row has it.
      const status = { subject: "chat", state: sessionState, label: sessionState, tone: "neutral", reason_code: "", sentence: "" };
      if (at === "/api/v1/chats") return json({ items: [{ ...chat, session_state: sessionState, status }], next_cursor: null });
      if (at === "/api/v1/files/drives") return json({ id: "drv", orgId: "o", rootId: "r", quotaBytes: 1 });
      if (at === "/api/v1/machines/current") return json({ machine_id: "m1", status: "ready", name: "b", reason: null });
      if (at.endsWith("/children")) return json({ value: [], nextMarker: null });
      if (at.includes("/items/")) return json({ id: "node-work", name: "Revenue model.alkerachat", kind: "folder" });
      return json({});
    }),
  );
  return wakes;
}

async function open(): Promise<void> {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[`/workspaces/${W}`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId" element={<WorkspaceRoute />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
  await screen.findAllByText("Revenue model");
}

afterEach(() => {
  cleanup();
  resetFrameBus();
  vi.unstubAllGlobals();
});

describe("opening a workspace with no chat open", () => {
  it("asks the server once to wake a workspace that is asleep", async () => {
    const wakes = script("asleep");
    await open();
    await waitFor(() => expect(wakes).toEqual([`POST /api/v1/workspaces/${W}/wake`]));
    await act(async () => {});
    expect(wakes).toHaveLength(1);
  });

  it("asks for nothing when its chat already reads awake, on open or on focus", async () => {
    const wakes = script("awake");
    await open();
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(wakes).toEqual([]);
  });
});
