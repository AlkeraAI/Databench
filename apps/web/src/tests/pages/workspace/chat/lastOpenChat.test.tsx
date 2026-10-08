// Chat remembers which chat you were in.
//
// Chat is a place, not a form: leaving it for Files and coming back through the
// nav should land on the conversation that was open. The nav's Chat leaf can
// only name one path, so `/chat` is the one that resolves — and `/chat/new`,
// the empty composer, is the one that never does.
//
// The three chat paths declared here are exactly the ones `App.tsx` declares
// (`/chat`, `/chat/new`, `/chat/:chatId`), all on the same element, so what is
// exercised is the real resolution order: a static `new` outranks the dynamic
// id, and all three are one component instance.
//
// The memory is a convenience with teeth: it is per signed-in account, it is
// only written for a chat that actually opened, and it is dropped the moment
// the chat refuses — a remembered chat that was deleted or unshared costs its
// reader one bounce, never a door that keeps leading back to a dead transcript.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { accountKey } from "@alkera/ui/storage";

import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";

vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: ({ chatId }: { chatId?: string }) => (
    <div data-testid="surface">{chatId ?? "no chat"}</div>
  ),
}));

import { CHAT_GONE_TITLE, ChatPage, NEW_CHAT_PATH } from "@/pages/workspace/chat/ChatPage";
import { forgetDeletedChat } from "@/pages/workspace/chat/lastOpenChat";

const chatRow = (id: string) => ({
  id,
  title: `Chat ${id}`,
  machine_id: "m1",
  machine_status: "ready" as const,
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
  last_seq: 1,
  files_node_id: `nd_${id}`,
});

/** The API: every chat in `known` reads back; anything in `broken` answers a
 *  500 the way a server having a bad minute does; anything else 404s the way a
 *  deleted or unshared chat does. */
function script({
  me = "u1",
  org = "org_a",
  known = ["a", "b"],
  broken = [],
}: { me?: string; org?: string; known?: string[]; broken?: string[] } = {}): void {
  const json = (payload: unknown, code = 200): Response =>
    new Response(JSON.stringify(payload), {
      status: code,
      headers: { "content-type": "application/json" },
    });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const at = new URL(url, "http://x").pathname;
      const one = /^\/api\/v1\/chats\/([^/?]+)$/.exec(at);
      if (one) {
        const id = one[1] ?? "";
        if (broken.includes(id)) {
          return json({ detail: { code: "server_error", message: "Boom" } }, 500);
        }
        return known.includes(id)
          ? json(chatRow(id))
          : json({ detail: { code: "not_found", message: "No such chat" } }, 404);
      }
      if (at.startsWith("/api/v1/chats")) {
        return json({ items: known.map(chatRow), next_cursor: null });
      }
      if (at.includes("/api/v1/machines/current")) {
        return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
      }
      if (at.includes("/api/v1/auth/me")) {
        return json({ id: me, email: `${me}@example.com`, org_team_id: org });
      }
      return json({});
    }),
  );
}

/** Reports the router's location, so where a leaf lands is read off the URL. */
function Where() {
  return <span data-testid="at">{useLocation().pathname}</span>;
}

/** The nav, reduced to the one leaf this is about. */
function Nav() {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => navigate("/files")}>
        Files
      </button>
      <button type="button" onClick={() => navigate("/chat")}>
        Chat
      </button>
      <button type="button" onClick={() => navigate(-1)}>
        Back
      </button>
    </>
  );
}

function renderPortal(at: string) {
  const client = createQueryClient({ retry: false });
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[at]}>
        <Where />
        <Nav />
        <Routes>
          <Route path="/chat" element={<ChatPage />} />
          <Route path={NEW_CHAT_PATH} element={<ChatPage />} />
          <Route path="/chat/:chatId" element={<ChatPage />} />
          <Route path="/files" element={<p>Files</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

const at = (): string => screen.getByTestId("at").textContent ?? "";
const click = (name: string): void => {
  fireEvent.click(screen.getByRole("button", { name }));
};
const stored = (user: string, org = "org_a"): string | null =>
  window.localStorage.getItem(accountKey(user, org, "chat.last"));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("coming back to Chat", () => {
  it("lands on the chat that was open, not on an empty surface", async () => {
    script();
    renderPortal("/chat/a");
    await waitFor(() => expect(stored("u1")).toBe("a"));

    click("Files");
    await waitFor(() => expect(at()).toBe("/files"));
    click("Chat");

    await waitFor(() => expect(at()).toBe("/chat/a"));
    expect(screen.getByTestId("surface")).toHaveTextContent("a");
  });

  it("the LAST chat wins, not the first one ever opened", async () => {
    script();
    const { unmount } = renderPortal("/chat/a");
    await waitFor(() => expect(stored("u1")).toBe("a"));
    unmount();

    renderPortal("/chat/b");
    await waitFor(() => expect(stored("u1")).toBe("b"));
    click("Chat");
    await waitFor(() => expect(at()).toBe("/chat/b"));
  });

  it("with nothing remembered, /chat is the empty surface it always was", async () => {
    script();
    renderPortal("/chat");

    await waitFor(() => expect(screen.getByTestId("surface")).toHaveTextContent("no chat"));
    expect(at()).toBe("/chat");
  });

  it("New chat bypasses the memory: its path is the composer, not the last chat", async () => {
    script();
    const { unmount } = renderPortal("/chat/a");
    await waitFor(() => expect(stored("u1")).toBe("a"));
    unmount();

    renderPortal(NEW_CHAT_PATH);
    await waitFor(() => expect(screen.getByTestId("surface")).toHaveTextContent("no chat"));
    // And the memory is untouched, so the nav's own leaf still resumes.
    expect(at()).toBe("/chat/new");
    expect(stored("u1")).toBe("a");
  });
});

describe("a memory that must not be followed", () => {
  it("never leaks one account's chat to the next person on this browser", async () => {
    script({ me: "u1" });
    const { unmount } = renderPortal("/chat/a");
    await waitFor(() => expect(stored("u1")).toBe("a"));
    unmount();
    vi.unstubAllGlobals();

    // A different account, same browser, same storage.
    script({ me: "u2" });
    renderPortal("/chat");

    await waitFor(() => expect(screen.getByTestId("surface")).toHaveTextContent("no chat"));
    expect(at()).toBe("/chat");
    // The first account's memory is still theirs, untouched.
    expect(stored("u1")).toBe("a");
    expect(stored("u2")).toBeNull();
  });

  it("never resumes a chat from the org the reader switched away from", async () => {
    script({ org: "org_a" });
    const { unmount } = renderPortal("/chat/a");
    await waitFor(() => expect(stored("u1", "org_a")).toBe("a"));
    unmount();
    vi.unstubAllGlobals();

    // The same person, now acting in another org in this browser.
    script({ org: "org_b" });
    const second = renderPortal("/chat");
    await waitFor(() => expect(screen.getByTestId("surface")).toHaveTextContent("no chat"));
    expect(at()).toBe("/chat");
    expect(stored("u1", "org_b")).toBeNull();
    second.unmount();
    vi.unstubAllGlobals();

    // Back in the first org, its memory is still there to follow.
    script({ org: "org_a" });
    renderPortal("/chat");
    await waitFor(() => expect(at()).toBe("/chat/a"));
  });

  it("forgets a deleted chat wherever it is remembered, in every org", () => {
    window.localStorage.setItem(accountKey("u1", "org_a", "chat.last"), "gone");
    window.localStorage.setItem(accountKey("u1", "org_b", "chat.last"), "gone");
    window.localStorage.setItem(accountKey("u1", "org_c", "chat.last"), "kept");
    // Another family holding the same id is not this memory.
    window.localStorage.setItem(accountKey("u1", "org_a", "chat.layout"), "gone");

    forgetDeletedChat("gone");

    expect(stored("u1", "org_a")).toBeNull();
    expect(stored("u1", "org_b")).toBeNull();
    expect(stored("u1", "org_c")).toBe("kept");
    expect(window.localStorage.getItem(accountKey("u1", "org_a", "chat.layout"))).toBe("gone");
  });

  it("costs a chat that has gone exactly one bounce, and then stops leading there", async () => {
    script();
    const { unmount } = renderPortal("/chat/a");
    await waitFor(() => expect(stored("u1")).toBe("a"));
    unmount();
    vi.unstubAllGlobals();

    // `a` is gone — deleted, or unshared from under this reader.
    script({ known: ["b"] });
    renderPortal("/chat");

    // The memory resolves and the chat is not there, so the reader is told so
    // on the id they were sent to, rather than being handed a composer for a
    // chat they did not ask to start.
    await screen.findByText(CHAT_GONE_TITLE);
    expect(screen.queryByTestId("surface")).not.toBeInTheDocument();
    // And the id is dropped, so the next visit to the leaf does not repeat it.
    await waitFor(() => expect(stored("u1")).toBeNull());
    click("Chat");
    await waitFor(() => expect(at()).toBe("/chat"));
    expect(screen.getByTestId("surface")).toHaveTextContent("no chat");
  });

  it("leaves no dead history entry behind: Back is where the reader came from", async () => {
    script();
    const { unmount } = renderPortal("/chat/a");
    await waitFor(() => expect(stored("u1")).toBe("a"));
    unmount();
    vi.unstubAllGlobals();

    script({ known: ["b"] });
    renderPortal("/files");
    click("Chat");

    await screen.findByText(CHAT_GONE_TITLE);
    // One entry was pushed — the nav's leaf — and the hop off it onto the
    // remembered id replaced it, so Back is Files and not the leaf that would
    // only resolve here again.
    click("Back");
    await waitFor(() => expect(at()).toBe("/files"));
  });

  it("a direct link to a chat that is gone says so, and is never remembered", async () => {
    script({ known: ["b"] });
    renderPortal("/chat/a");

    await screen.findByText(CHAT_GONE_TITLE);
    expect(screen.queryByTestId("surface")).not.toBeInTheDocument();
    // The link stays in the address bar: the reader can see which id failed
    // them, and nothing wrote it down on their behalf.
    expect(at()).toBe("/chat/a");
    await waitFor(() => expect(stored("u1")).toBeNull());
  });

  it("a server having a bad minute is not a chat that is gone", async () => {
    script({ known: ["a"], broken: ["a"] });
    window.localStorage.setItem(accountKey("u1", "org_a", "chat.last"), "a");
    const { client } = renderPortal("/chat/a");

    // Waited on the read actually failing, not on the first paint: before the
    // 500 lands every one of the assertions below is trivially true.
    await waitFor(() =>
      expect(client.getQueryState(keys.chats.one("a"))?.status).toBe("error"),
    );
    // The read failed, but nothing said the chat is not there: the reader stays
    // on the chat's own route, still in that chat, and the memory keeps
    // pointing at it so the nav's leaf comes back here once the server is well.
    expect(at()).toBe("/chat/a");
    expect(screen.getByTestId("surface").textContent).toBe("a");
    expect(stored("u1")).toBe("a");
  });
});
