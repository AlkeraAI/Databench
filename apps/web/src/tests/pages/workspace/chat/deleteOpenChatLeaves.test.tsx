// Deleting the chat that is ON SCREEN: the reader has to leave it, and leave
// it the moment the server has agreed.
//
// The header's own suite proves the hop home happens; what it cannot see is
// WHEN. Every callback a callsite hands `mutate` runs after the cache's
// invalidation has been awaited, and that invalidation refetches the queries of
// the chat the DELETE just removed — reads that now answer 404 and are retried
// on the way. A hop home written in that callback is therefore queued behind a
// doomed refetch: the reader stays on `/chat/<deleted-id>` with the transcript,
// the files dock and a live composer in front of them for as long as it takes
// to settle.
//
// So the refetch here never settles at all. That is the honest model of the
// slow case, and it is the assertion: leaving a deleted chat does not wait on
// anything the deleted chat's own reads have to say.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/pages/workspace/chat/data", () => ({ chatHost: () => ({ kind: "browser" }) }));

import { useChat, useChats } from "@/api/chats";
import { createQueryClient } from "@/api/queryClient";
import { DELETE_CHAT_KEY, useDeleteChatAction } from "@/pages/workspace/chat/useDeleteChatAction";

const CHAT_ID = "11111111-2222-4333-8444-555555555555";
const CHAT = {
  id: CHAT_ID,
  title: "Quarterly prompts",
  owner_user_id: "u-1",
  can_delete: true,
  team_id: null,
  visibility_scope: "org",
  machine_id: null,
  machine_status: "none",
  machine_refusal_reason: null,
  last_seq: 0,
  version: 1,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

/** The chat's surface: the delete key, the transcript, and the composer that
 *  must go with them. It is also the component that holds the mutation. */
function Surface({ chatId }: { chatId: string }) {
  const remove = useDeleteChatAction(chatId);
  return (
    <>
      <p>{`transcript of ${chatId}`}</p>
      <textarea aria-label="Message" />
      <button type="button" onClick={remove.onDelete}>
        Delete chat
      </button>
      {remove.dialog}
    </>
  );
}

/** The page: it stands on the chat read, and reads the list the rail draws —
 *  the two query slots the delete's invalidation refetches. */
function Page() {
  const { chatId } = useParams();
  useChat(chatId);
  useChats();
  if (!chatId) return <h1>chat home</h1>;
  return <Surface chatId={chatId} />;
}

function Here() {
  const location = useLocation();
  return <span data-testid="here">{location.pathname}</span>;
}

/**
 * @param settle whether the reads the delete's invalidation refetches ever
 *   answer. `false` is the slow case in the limit: the awaited refetch is still
 *   outstanding, so every callsite callback the mutation owes is still unpaid.
 */
function mount(settle: boolean) {
  const deleted = { value: false };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw = input instanceof Request ? input.url : String(input);
      const url = new URL(raw, "http://x");
      const method = init?.method ?? (input instanceof Request ? input.method : "GET");
      if (method === "DELETE" && url.pathname === `/api/v1/chats/${CHAT_ID}`) {
        deleted.value = true;
        return new Response(null, { status: 204 });
      }
      if (deleted.value && !settle) return new Promise<Response>(() => {});
      if (url.pathname === `/api/v1/chats/${CHAT_ID}`) {
        // A deleted chat is tombstoned, and every read hides a tombstone.
        return deleted.value
          ? Response.json({ error: { code: "not_found", message: "Not found" } }, { status: 404 })
          : Response.json(CHAT);
      }
      if (url.pathname === "/api/v1/chats") {
        return Response.json({ items: deleted.value ? [] : [CHAT], next_cursor: null });
      }
      return Response.json({ error: { code: "not_found", message: "Not found" } }, { status: 404 });
    }),
  );
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[`/chat/${CHAT_ID}`]}>
        <Here />
        <Routes>
          <Route path="/chat/new" element={<h1>chat home</h1>} />
          <Route path="/chat/:chatId" element={<Page />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("deleting the chat that is on screen", () => {
  it("leaves it without waiting for the deleted chat's own reads to answer", async () => {
    mount(false);
    expect(await screen.findByText(`transcript of ${CHAT_ID}`)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete chat" }));
    await userEvent.click(await screen.findByRole("button", { name: DELETE_CHAT_KEY }));

    expect(await screen.findByText("chat home")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("here")).toHaveTextContent("/chat/new"));
    // Nothing of the chat is left in front of the reader: no transcript to read
    // back, and no composer inviting a message into a chat that is gone.
    expect(screen.queryByText(`transcript of ${CHAT_ID}`)).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
  });

  it("still leaves when those reads do answer", async () => {
    mount(true);
    expect(await screen.findByText(`transcript of ${CHAT_ID}`)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete chat" }));
    await userEvent.click(await screen.findByRole("button", { name: DELETE_CHAT_KEY }));

    expect(await screen.findByText("chat home")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
  });
});
