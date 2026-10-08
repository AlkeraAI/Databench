// Deleting is the owner's or an org admin's. A reader the server says may not
// delete is offered no key, and a delete the server refuses anyway leaves the
// reader on the chat with the server's sentence rather than walking them home
// as though it had worked.

import { cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/pages/workspace/chat/data", () => ({ chatHost: () => ({ kind: "browser" }) }));

import { useChat } from "@/api/chats";
import { createQueryClient } from "@/api/queryClient";
import { DELETE_CHAT_FAILED, DELETE_CHAT_KEY, useDeleteChatAction } from "@/pages/workspace/chat/useDeleteChatAction";
import { RAW_FAILURES, expectNoRawFailureText } from "@/tests/fixtures/rawFailures";

const CHAT_ID = "11111111-2222-4333-8444-555555555555";
const REFUSAL = "Only the owner or an org admin can delete this chat.";

function chatRow(canDelete: boolean | undefined) {
  return {
    id: CHAT_ID,
    title: "Quarterly prompts",
    owner_user_id: "u-1",
    team_id: null,
    visibility_scope: "org",
    machine_id: null,
    machine_status: "none",
    machine_refusal_reason: null,
    last_seq: 0,
    version: 1,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    ...(canDelete === undefined ? {} : { can_delete: canDelete }),
  };
}

function Surface() {
  const { chatId } = useParams();
  const remove = useDeleteChatAction(chatId);
  const chat = useChat(chatId);
  return (
    <>
      <p>transcript</p>
      {chat.data ? <h2>{chat.data.title}</h2> : null}
      {remove.onDelete ? (
        <button type="button" onClick={remove.onDelete}>
          Delete chat
        </button>
      ) : null}
      {remove.dialog}
    </>
  );
}

function Here() {
  return <span data-testid="here">{useLocation().pathname}</span>;
}

function mount(canDelete: boolean | undefined, deleteStatus = 204, deleteAnswer?: () => Response) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw = input instanceof Request ? input.url : String(input);
      const url = new URL(raw, "http://x");
      const method = init?.method ?? (input instanceof Request ? input.method : "GET");
      if (method === "DELETE") {
        if (deleteAnswer) return deleteAnswer();
        return deleteStatus === 204
          ? new Response(null, { status: 204 })
          : Response.json({ error: { code: null, message: REFUSAL } }, { status: deleteStatus });
      }
      if (url.pathname === `/api/v1/chats/${CHAT_ID}`) return Response.json(chatRow(canDelete));
      return Response.json({ error: { code: "not_found", message: "Not found" } }, { status: 404 });
    }),
  );
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[`/chat/${CHAT_ID}`]}>
        <Here />
        <Routes>
          <Route path="/chat/new" element={<h1>chat home</h1>} />
          <Route path="/chat/:chatId" element={<Surface />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("a delete the reader may not make", () => {
  it("offers no key when the server says this reader may not delete", async () => {
    mount(false);
    // The key is decided on the read, so wait for it to land.
    expect(await screen.findByRole("heading", { name: "Quarterly prompts" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete chat" })).toBeNull();
  });

  it("offers no key when the server says nothing: unsaid is not a yes", async () => {
    mount(undefined);
    expect(await screen.findByRole("heading", { name: "Quarterly prompts" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete chat" })).toBeNull();
  });

  it("stays on the chat and shows the server's sentence when the delete is refused", async () => {
    mount(true, 403);
    await userEvent.click(await screen.findByRole("button", { name: "Delete chat" }));
    await userEvent.click(await screen.findByRole("button", { name: DELETE_CHAT_KEY }));

    expect(await screen.findByRole("alert")).toHaveTextContent(REFUSAL);
    expect(screen.getByTestId("here")).toHaveTextContent(`/chat/${CHAT_ID}`);
    expect(screen.queryByText("chat home")).toBeNull();
  });

  it.each(RAW_FAILURES)("stays on the chat and says %s in its own sentence, never in its own words", async (_label, answer) => {
    mount(true, 204, answer);
    await userEvent.click(await screen.findByRole("button", { name: "Delete chat" }));
    await userEvent.click(await screen.findByRole("button", { name: DELETE_CHAT_KEY }));

    expect(await screen.findByRole("alert")).toHaveTextContent(DELETE_CHAT_FAILED);
    expectNoRawFailureText();
    expect(screen.getByTestId("here")).toHaveTextContent(`/chat/${CHAT_ID}`);
  });

  it("leaves for home once the server agrees", async () => {
    mount(true);
    await userEvent.click(await screen.findByRole("button", { name: "Delete chat" }));
    await userEvent.click(await screen.findByRole("button", { name: DELETE_CHAT_KEY }));

    expect(await screen.findByText("chat home")).toBeInTheDocument();
  });
});
