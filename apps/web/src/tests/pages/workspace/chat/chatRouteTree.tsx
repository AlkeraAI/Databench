// The app's REAL chat tree, mounted the way the app mounts it.
//
// The app wraps its root in `<StrictMode>`, and StrictMode's dev remount is not
// a curiosity: it re-attaches a subtree's effects without re-rendering the
// parent, which is the same shape as a Suspense re-reveal, an error-boundary
// reset and a keyed remount. A chat test that renders the routes bare has been
// exercising a lifetime the product never runs under — so a chat route test
// that cares about mounting renders `<ChatUnderTest>`, which puts it back.

import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, type ReactElement, type ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { queryClient } from "@/api/queryClient";
import { BlobsSurface } from "@/pages/workspace/chat/BlobsSurface";
import { BlobSurface } from "@/pages/workspace/chat/BlobSurface";
import { ChatPage, ChatRoute, ChatStackedRoute } from "@/pages/workspace/chat/ChatPage";
import { ChatRuntimeLayout } from "@/pages/workspace/chat/ChatRuntimeLayout";
import { CompactionSurface } from "@/pages/workspace/chat/CompactionSurface";
import { PlanSurface } from "@/pages/workspace/chat/PlanSurface";

/** The chat routes exactly as `App.tsx` declares them. */
export function ChatRoutes(): ReactElement {
  return (
    <Routes>
      <Route element={<ChatRuntimeLayout />}>
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/new" element={<ChatPage />} />
        <Route path="/chat/:chatId" element={<ChatRoute />} />
        <Route element={<ChatStackedRoute />}>
          <Route path="/chat/:chatId/results" element={<BlobsSurface />} />
          <Route path="/chat/:chatId/result/:handle" element={<BlobSurface />} />
          <Route path="/chat/:chatId/plan/:partId" element={<PlanSurface />} />
          <Route path="/chat/:chatId/compaction/:partId" element={<CompactionSurface />} />
        </Route>
      </Route>
      <Route path="*" element={<div>away from the chat</div>} />
    </Routes>
  );
}

/** The chat routes under the lifetime the app runs them under. `children`
 *  replaces the route tree for a test that needs its own subtree around it. */
export function ChatUnderTest({
  path,
  children,
}: {
  path: string;
  children?: ReactNode;
}): ReactElement {
  return (
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[path]}>{children ?? <ChatRoutes />}</MemoryRouter>
      </QueryClientProvider>
    </StrictMode>
  );
}
