// The browser's chat host names the signed-in user and the org they are acting
// in, so what the chat remembers in this browser is filed under both.

import { cleanup, render, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { meKey, type CurrentUser } from "@/api/auth";
import { createQueryClient } from "@/api/queryClient";
import { ChatRuntimeProvider } from "@/pages/workspace/chat/ChatRuntimeLayout";
import { chatHost, resetChatRuntime } from "@/pages/workspace/chat/data";

beforeEach(() => {
  // Nothing here needs the network; anything asked answers empty.
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  resetChatRuntime();
});

it("hands the chat the user id and the org the session is acting in", async () => {
  const client = createQueryClient({ retry: false });
  client.setQueryData(meKey, { id: "usr_dana", email: "dana@example.com", org_team_id: "org_a" } as unknown as CurrentUser);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ChatRuntimeProvider>
          <p>chat</p>
        </ChatRuntimeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );

  await waitFor(() =>
    expect(chatHost().account()).toEqual({
      email: "dana@example.com",
      webAppUrl: null,
      userId: "usr_dana",
      orgId: "org_a",
    }),
  );
});
