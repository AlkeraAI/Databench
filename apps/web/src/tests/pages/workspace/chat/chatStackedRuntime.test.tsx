// A surface stacked over a chat has the chat's runtime.
//
// The source and the host are a module-level installation every chat surface
// reads through. The chat page installed it, and the stacked pages — the
// results list, a plan, a compaction summary — were declared as its SIBLINGS,
// so nothing had installed one by the time they mounted: a cold load of any of
// them, and the compaction card's own "Open summary", threw `no chat runtime
// installed` into the app's error boundary. The install belongs on the route
// they share.

import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { MemoryRouter, Route, RouterProvider, Routes, createMemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryClient } from "@/api/queryClient";
import { chatData } from "@/pages/workspace/chat/data";
import { ChatRuntimeLayout } from "@/pages/workspace/chat/ChatRuntimeLayout";
import { BlobsSurface } from "@/pages/workspace/chat/BlobsSurface";
import { CompactionSurface } from "@/pages/workspace/chat/CompactionSurface";

const UUID = "6f1a1c2e-6d41-4e2a-9a77-2b1f0c9d4e55";
const PART = "part-1";

function scriptFetch(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const at = new URL(url, "http://x").pathname;
      const json = (body: unknown, code = 200): Response =>
        new Response(JSON.stringify(body), {
          status: code,
          headers: { "content-type": "application/json" },
        });
      if (at.includes("/auth/me")) return json({ id: "u1", email: "dana@example.com" });
      if (at.includes("/machines/current")) {
        return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
      }
      if (at.endsWith("/messages")) return json({ items: [], next_cursor: null });
      if (/^\/api\/v1\/chats\/[^/]+$/.test(at)) {
        return json({
          id: UUID,
          title: "A long one",
          owner_user_id: "u1",
          machine_id: "m1",
          machine_status: "ready",
          machine_refusal_reason: null,
          permission_mode: "default",
          last_seq: 0,
          created_at: "2026-09-06T12:00:00Z",
          updated_at: "2026-09-06T12:00:00Z",
        });
      }
      if (at.startsWith("/api/v1/chats")) return json({ items: [], next_cursor: null });
      return json({});
    }),
  );
}

// Under StrictMode, because the app runs its tree under StrictMode and these
// tests are about what a cold MOUNT of a stacked surface gets. Rendering them
// bare is how the layout shipped with a runtime its own effect cleanup cleared
// out from under its children.
function renderAt(path: string) {
  return render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route element={<ChatRuntimeLayout />}>
              <Route path="/chat/:chatId/results" element={<BlobsSurface />} />
              <Route path="/chat/:chatId/compaction/:partId" element={<CompactionSurface />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </StrictMode>,
  );
}

beforeEach(() => {
  queryClient.clear();
  scriptFetch();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("a stacked chat surface opened cold", () => {
  it("has the chat's runtime rather than throwing for want of one", async () => {
    renderAt(`/chat/${UUID}/compaction/${PART}`);

    // It renders its own chrome — the surface, not the app's error boundary.
    expect(await screen.findByText("Compaction")).toBeTruthy();
    // And the runtime it read through is installed: asking for it is what used
    // to throw.
    expect(() => chatData()).not.toThrow();
  });

  it("keeps ONE runtime across the hop between two stacked surfaces", async () => {
    const router = createMemoryRouter(
      [
        {
          element: <ChatRuntimeLayout />,
          children: [
            { path: "/chat/:chatId/results", element: <BlobsSurface /> },
            { path: "/chat/:chatId/compaction/:partId", element: <CompactionSurface /> },
          ],
        },
      ],
      { initialEntries: [`/chat/${UUID}/compaction/${PART}`] },
    );
    render(
      <StrictMode>
        <QueryClientProvider client={queryClient}>
          <RouterProvider router={router} />
        </QueryClientProvider>
      </StrictMode>,
    );
    await screen.findByText("Compaction");
    const first = chatData();

    await act(async () => {
      await router.navigate(`/chat/${UUID}/results`);
    });

    // A second source is a second subscription that has never read the chat,
    // so the hop must not build one — and nothing is torn down under the
    // surface that is arriving.
    expect(chatData()).toBe(first);
  });
});
