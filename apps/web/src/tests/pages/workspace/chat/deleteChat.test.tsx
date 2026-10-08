// Deleting the open chat from its header: the confirm step gates the DELETE,
// the reader lands on the chat home once it is gone, and the rail no longer
// lists the chat -- through the mutation's invalidation, not a hand-wired
// refetch, which is why the client here is the portal's own.
//
// The question is the product's own dialog rather than the browser's. That is
// not a matter of looks: `window.confirm` offers "OK" for deleting a
// colleague's work, and a reader who has ticked the browser's "don't show me
// these again" deletes a chat on a single click with no question at all. So
// this file also pins that the browser's own confirm is never reached.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

// The route table asks the host which shell this is; a browser tab goes home to
// `/chat`, and no chat runtime is installed in this test.
vi.mock("@/pages/workspace/chat/data", () => ({ chatHost: () => ({ kind: "browser" }) }));

import { useChat, useChats } from "@/api/chats";
import { createQueryClient } from "@/api/queryClient";
import {
  DELETE_CHAT_BODY,
  DELETE_CHAT_KEY,
  DELETE_CHAT_TITLE,
  useDeleteChatAction,
} from "@/pages/workspace/chat/useDeleteChatAction";
import {
  flushWorkspace,
  useWorkspaceStore,
  workspaceOf,
} from "@/pages/workspace/chat/workspace/workspaceStore";

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

function Rail() {
  const chats = useChats();
  return (
    <ul aria-label="chats">
      {(chats.data?.items ?? []).map((chat) => (
        <li key={chat.id}>{chat.title}</li>
      ))}
    </ul>
  );
}

function OpenChat() {
  const { chatId } = useParams();
  // The header's own read of the chat row, which is where the name in the
  // delete question comes from. Rendered so a test can press the key only once
  // the row a real header would already be drawn from has landed.
  const chat = useChat(chatId);
  const remove = useDeleteChatAction(chatId);
  return (
    <>
      <h1>chat {chatId}</h1>
      {chat.isSuccess ? <p>row loaded</p> : null}
      {remove.onDelete ? (
        <button type="button" onClick={remove.onDelete}>
          Delete chat
        </button>
      ) : null}
      {remove.dialog}
    </>
  );
}

/** Answering the dialog the way a reader does. */
async function confirmDelete() {
  await userEvent.click(await screen.findByRole("button", { name: DELETE_CHAT_KEY }));
}

function mount(title = CHAT.title) {
  const deleted = { value: false };
  const row = { ...CHAT, title };
  const calls: { method: string; path: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
      const method = init?.method ?? "GET";
      calls.push({ method, path: url.pathname });
      if (method === "DELETE" && url.pathname === `/api/v1/chats/${CHAT_ID}`) {
        deleted.value = true;
        return new Response(null, { status: 204 });
      }
      if (method === "GET" && url.pathname === "/api/v1/chats") {
        return Response.json({ items: deleted.value ? [] : [row], next_cursor: null });
      }
      if (method === "GET" && url.pathname === `/api/v1/chats/${CHAT_ID}`) {
        return deleted.value
          ? Response.json({ error: { code: "not_found", message: "Not found" } }, { status: 404 })
          : Response.json(row);
      }
      return Response.json({ error: { code: "not_found", message: "Not found" } }, { status: 404 });
    }),
  );
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[`/chat/${CHAT_ID}`]}>
        <Rail />
        <Routes>
          <Route path="/chat" element={<h1>resumed chat</h1>} />
          {/* The portal's chat home is the empty composer, not the nav's /chat
              leaf: that leaf resolves to the chat the reader last had open, and
              landing there after a delete would put them back in front of the
              transcript they just removed. */}
          <Route path="/chat/new" element={<h1>chat home</h1>} />
          <Route path="/chat/:chatId" element={<OpenChat />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { calls };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("deleting the open chat", () => {
  it("asks first, deletes by id, lands on the chat home, and the rail drops the chat", async () => {
    const browserConfirm = vi.fn(() => true);
    vi.stubGlobal("confirm", browserConfirm);
    const { calls } = mount();
    expect(await screen.findByText("Quarterly prompts")).toBeInTheDocument();
    await screen.findByText("row loaded");

    await userEvent.click(screen.getByRole("button", { name: "Delete chat" }));

    // The question is on screen, in the product's own dialog, under the name of
    // the chat it is about, and nothing has been deleted while it stands.
    expect(
      await screen.findByRole("dialog", { name: "Delete Quarterly prompts?" }),
    ).toBeInTheDocument();
    expect(screen.getByText(DELETE_CHAT_BODY)).toBeInTheDocument();
    expect(browserConfirm).not.toHaveBeenCalled();
    expect(calls.filter((call) => call.method === "DELETE")).toEqual([]);

    await confirmDelete();

    expect(await screen.findByText("chat home")).toBeInTheDocument();
    expect(calls).toContainEqual({ method: "DELETE", path: `/api/v1/chats/${CHAT_ID}` });
    await waitFor(() => expect(screen.queryByText("Quarterly prompts")).not.toBeInTheDocument());
  });

  // A chat whose first message has not named it yet has nothing to put in the
  // heading, so the question keeps its unnamed form.
  it("asks about this chat when the chat has no title yet", async () => {
    mount("");
    await screen.findByText("row loaded");

    await userEvent.click(screen.getByRole("button", { name: "Delete chat" }));

    expect(await screen.findByRole("dialog", { name: DELETE_CHAT_TITLE })).toBeInTheDocument();
  });

  it.each([
    [
      "a title that is itself a question keeps one question mark",
      "What changed in prompt runs this week?",
      "Delete What changed in prompt runs this week?",
    ],
    [
      "a title past the heading's length is cut with an ellipsis",
      `qa ${"renamed ".repeat(20)}end`,
      `Delete qa ${"renamed ".repeat(9)}rena…?`,
    ],
  ])("%s", async (_case, title, heading) => {
    mount(title);
    await screen.findByText("row loaded");
    await userEvent.click(screen.getByRole("button", { name: "Delete chat" }));
    expect(await screen.findByRole("dialog", { name: heading })).toBeInTheDocument();
  });

  it("a declined confirm deletes nothing and stays put", async () => {
    const { calls } = mount();
    expect(await screen.findByText("Quarterly prompts")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete chat" }));
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(calls.filter((call) => call.method === "DELETE")).toEqual([]);
    expect(screen.getByText(`chat ${CHAT_ID}`)).toBeInTheDocument();
    expect(screen.getByText("Quarterly prompts")).toBeInTheDocument();
  });

  // A chat is gone for everyone it was shared with, so the question opens with Cancel under the
  // finger and refuses the Enter a reader on their way somewhere else would press.
  it("opens on Cancel, and Enter leaves the chat alone", async () => {
    const { calls } = mount();
    expect(await screen.findByText("Quarterly prompts")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete chat" }));
    const cancel = await screen.findByRole("button", { name: "Cancel" });
    await waitFor(() => expect(cancel).toHaveFocus());
    await userEvent.keyboard("{Enter}");

    expect(calls.filter((call) => call.method === "DELETE")).toEqual([]);
    expect(screen.getByText(`chat ${CHAT_ID}`)).toBeInTheDocument();
  });

  it("takes the chat's open tabs with it, and writes none of them back", async () => {
    // The tabs live on the chat row the DELETE just removed. A pending write
    // that outlived the delete would be a PUT to a chat the server no longer
    // has — and a workspace left in memory would be inherited by whatever next
    // opened under that id.
    useWorkspaceStore.getState().hydrate(CHAT_ID, {
      tabs: [
        { id: "files", kind: "files", name: "Files", params: {} },
        { id: "tab-1", kind: "file", name: "q3-report.html", node_id: "nd_report" },
      ],
      active_tab_id: "tab-1",
    });
    useWorkspaceStore.getState().openFileTab(CHAT_ID, { nodeId: "nd_plot", name: "q3.png" });
    expect(workspaceOf(CHAT_ID)?.tabs).toHaveLength(3);

    const { calls } = mount();
    expect(await screen.findByText("Quarterly prompts")).toBeInTheDocument();
    // Settle the opened tab's own debounced save — a write to a chat that still
    // exists, and none of this case's business — then leave a fresh pending one
    // behind, which is the write the delete has to cancel.
    await flushWorkspace(CHAT_ID);
    useWorkspaceStore.getState().activate(CHAT_ID, "files");

    await userEvent.click(screen.getByRole("button", { name: "Delete chat" }));
    await confirmDelete();
    expect(await screen.findByText("chat home")).toBeInTheDocument();

    const removed = calls.findIndex(
      (call) => call.method === "DELETE" && call.path === `/api/v1/chats/${CHAT_ID}`,
    );
    expect(removed).toBeGreaterThanOrEqual(0);
    // Nothing is left that could write to the chat: asking for the pending
    // write now finds neither a timer nor anything to send, so the DELETE is
    // the last thing this chat's id ever sees.
    await flushWorkspace(CHAT_ID);
    expect(calls.slice(removed + 1).filter((call) => call.path.endsWith("/workspace"))).toEqual([]);
    expect(workspaceOf(CHAT_ID)).toBeUndefined();
  });
});
