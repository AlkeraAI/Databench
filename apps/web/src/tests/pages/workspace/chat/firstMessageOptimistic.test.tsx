// The first message of a brand-new chat, from Send to echo.
//
// Starting a chat spans two routes: the reader presses Send on the chats
// screen, and the moment the server has minted an id they are moved onto
// `/chat/<id>`, which mounts a FRESH surface. That surface's transcript is
// whatever the box running the chat has published so far — and a box that is
// still waking up has published nothing, so the bubble the reader was looking
// at blinked out and came back seconds later. This file pins the whole span:
//
//  1. the message is on screen before the server has answered anything;
//  2. it is still on screen after the hop onto the new chat's own route, while
//     the box is silent;
//  3. when the box finally echoes it there is exactly ONE of it; and
//  4. a chat that could not be started hands the words back to the composer,
//     with the reason where the bubble stands.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";

const MESSAGE = "Map the warehouse";

/** The echoed user turn, as the box publishes it back. */
const ECHO = {
  id: "u1",
  author: "user",
  status: "done",
  parts: [{ id: "u1-t", kind: "text", text: MESSAGE }],
};

const { ds, state } = vi.hoisted(() => {
  const state = {
    chats: [] as Record<string, unknown>[],
    /** What the box has published for the new chat. Empty while it wakes up. */
    turns: [] as unknown[],
    /** Released by the test to let `POST /chats` answer. */
    releaseCreate: () => {},
    /** How the create answers: the chat, or a rejection. */
    createRejection: null as unknown,
    listeners: [] as ((event: { replay?: boolean }) => void)[],
  };
  const ds = {
    listChats: async () => state.chats,
    listModels: async () => [] as unknown[],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: vi.fn(async () => state.turns),
    searchFiles: async () => [] as unknown[],
    sendUserMessage: async () => ({ id: "m1", role: "user", content: MESSAGE }),
    getPermissionMode: async () => "read_only",
    setPermissionMode: async () => {},
    subscribePermissionMode: () => () => {},
    subscribeChat: (_chatId: string, onEvent: (event: { replay?: boolean }) => void) => {
      state.listeners.push(onEvent);
      return () => {
        state.listeners = state.listeners.filter((entry) => entry !== onEvent);
      };
    },
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
    // The whole point of the delay: `POST /chats` (+ the message post behind
    // it) is a real round trip to a machine that may still be booting.
    createChat: vi.fn(async () => {
      await new Promise<void>((resolve) => {
        state.releaseCreate = resolve;
      });
      if (state.createRejection) throw state.createRejection;
      // Titled differently from the message on purpose: the chrome renders the
      // title, so a title equal to the message would make every count of the
      // bubble count the heading too.
      const chat = { id: "c1", title: "Warehouse map", updatedAt: "2026-09-15T00:00:00Z" };
      state.chats = [chat];
      return chat;
    }),
  };
  return { ds, state };
});

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const host = real.createBrowserChatHost({
    account: () => ({ email: "ben@tideline.example", webAppUrl: null }),
  });
  return {
    ...real,
    chatHost: () => host,
    chatData: () => ds,
    refetchWhileErrored: () => false as const,
    refetchWhileNoChatDefault: () => false as const,
    refetchWhileErroredOrEmpty: () => false as const,
    chatCaps: () => ({ opencodeActive: false, modelCatalog: true }),
  };
});

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";
import { useChatStore } from "@/pages/workspace/chat/chatStore";
import { createErrorTurn } from "@/pages/workspace/chat/controller";

/** What a refused create reads as, taken from the formatter the surface uses. */
function failureLine(rejection: unknown): string {
  const part = createErrorTurn(rejection).parts[0];
  if (part.kind !== "system") throw new Error("a failed create no longer reads as a system line");
  return part.text;
}

function Probe() {
  const location = useLocation();
  return <div data-testid="loc">{location.pathname}</div>;
}

/** The portal's own mount: a fresh surface per chat id, exactly as ChatPage
 *  keys it, so the remount is what could throw the bubble away. */
function Keyed() {
  const { chatId } = useParams<{ chatId: string }>();
  return <ChatSurface key={chatId ?? "new"} chatId={chatId} />;
}

afterEach(() => {
  cleanup();
  state.chats = [];
  state.turns = [];
  state.createRejection = null;
  state.listeners = [];
  state.releaseCreate = () => {};
  vi.clearAllMocks();
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
});

function renderHome() {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route path="/chat" element={<Keyed />} />
          <Route path="/chat/:chatId" element={<Keyed />} />
        </Routes>
        <Probe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const field = (): HTMLElement => screen.getByRole("textbox", { name: /^message /i });

async function send(text: string): Promise<void> {
  await act(async () => {
    fireEvent.change(field(), { target: { value: text } });
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
  });
}

/** Let the create resolve and the navigation land. */
async function landOnNewChat(): Promise<void> {
  await act(async () => {
    state.releaseCreate();
    await Promise.resolve();
  });
  await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/chat/c1"));
}

describe("the first message of a new chat", () => {
  it("is on screen before the server has answered", async () => {
    renderHome();
    await waitFor(() => expect(field()).toBeInTheDocument());

    await send(MESSAGE);

    // Nothing has come back: the create is still parked on its round trip.
    expect(ds.createChat).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("loc")).toHaveTextContent("/chat");
    expect(screen.getByText(MESSAGE)).toBeInTheDocument();
    // …and the composer is already clear for the next one.
    expect(field()).toHaveValue("");
  });

  it("stays on screen across the hop onto the new chat, while the box is silent", async () => {
    renderHome();
    await waitFor(() => expect(field()).toBeInTheDocument());
    await send(MESSAGE);

    await landOnNewChat();

    // The box has published nothing yet — `getChatTurns` answers with an empty
    // transcript — and the reader is still looking at their own message.
    expect(state.turns).toEqual([]);
    expect(screen.getByText(MESSAGE)).toBeInTheDocument();
    // The chat reads as working, not as an idle chat under an unanswered line.
    expect(screen.getByText(/^working/i)).toBeInTheDocument();
  });

  it("is reconciled in place when the box echoes it — exactly one bubble", async () => {
    renderHome();
    await waitFor(() => expect(field()).toBeInTheDocument());
    await send(MESSAGE);
    await landOnNewChat();
    expect(screen.getAllByText(MESSAGE)).toHaveLength(1);

    // The box wakes up and publishes the message back.
    state.turns = [ECHO];
    await act(async () => {
      for (const listener of state.listeners) listener({ replay: false });
      await Promise.resolve();
    });

    await waitFor(() => expect(ds.getChatTurns).toHaveBeenCalled());
    // Never dropped, never drawn twice.
    await waitFor(() => expect(screen.getAllByText(MESSAGE)).toHaveLength(1));
  });

  it("hands the words back to the composer when the chat cannot be started", async () => {
    state.createRejection = new ApiError(409, {
      error: { code: "machine_unreachable", message: "Your workspace is not reachable." },
    });
    renderHome();
    await waitFor(() => expect(field()).toBeInTheDocument());

    await send(MESSAGE);
    await act(async () => {
      state.releaseCreate();
      await Promise.resolve();
    });

    // The reason is said in the transcript…
    await waitFor(() =>
      expect(screen.getByText(failureLine(state.createRejection))).toBeInTheDocument(),
    );
    // …the reader is still on the composer they sent from…
    expect(screen.getByTestId("loc")).toHaveTextContent("/chat");
    // …and the sentence is back in the field, ready to send again.
    await waitFor(() => expect(field()).toHaveValue(MESSAGE));
  });

  it("leaves the composer alone when the chat starts", async () => {
    renderHome();
    await waitFor(() => expect(field()).toBeInTheDocument());
    await send(MESSAGE);
    await landOnNewChat();

    // The negative case that keeps the hand-back load-bearing: a create that
    // succeeded must not push the text back at the reader.
    expect(field()).toHaveValue("");
  });
});
