// A suggested first ask goes out the way a typed message does.
//
// A suggestion must not take its own path from the button to the chat store,
// which would skip the composer's checks (no machine to serve the organization)
// and drop whatever files the reader had staged.
//
// Driven through the real `ChatSurface`, store, router and composer over a
// source that can open a chat ahead of its first message. The pinned contract:
// a suggestion is pressable exactly when the composer would send; pressed, it
// carries every staged file into the chat it opens; over words the reader has
// typed it sends nothing and loses nothing; and an unavailable composer stages
// no file by any door, so nothing is ever staged that cannot be sent.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

interface UploadCall {
  chatId: string;
  kind: string;
  n: number;
  name: string;
  resolve: (path: string) => void;
}

const { ds, state } = vi.hoisted(() => {
  const state = {
    chats: [] as Record<string, unknown>[],
    uploads: [] as UploadCall[],
  };
  const ds = {
    chatFiles: {
      upload: (chatId: string, file: File, hint: { kind: string; n: number }) =>
        new Promise<{ path: string }>((resolve) => {
          state.uploads.push({
            chatId,
            kind: hint.kind,
            n: hint.n,
            name: file.name,
            resolve: (path) => resolve({ path }),
          });
        }),
      resolveUrl: async () => null,
    },
    listChats: async () => state.chats,
    listModels: async () => [] as unknown[],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: vi.fn(async () => [] as unknown[]),
    searchFiles: async () => [] as unknown[],
    sendUserMessage: vi.fn(async (_chatId: string, content: string) => ({ id: "m1", role: "user", content })),
    getPermissionMode: async () => "read_only",
    setPermissionMode: async () => {},
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
    createChat: vi.fn(async (text: string, _opts?: unknown) => {
      const chat = { id: "c2", title: text, updatedAt: "2026-09-27T00:00:00Z" };
      state.chats = [chat];
      return chat;
    }),
    startChat: vi.fn(async (title: string) => {
      const chat = { id: "c1", title, updatedAt: "2026-09-27T00:00:00Z" };
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

import { chatHost } from "@/pages/workspace/chat/data";
import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";
import { DRAFT_CHAT_KEY, useChatStore } from "@/pages/workspace/chat/chatStore";
import { useSurfaceKey } from "@/pages/workspace/chat/useSurfaceKey";

const NOTHING_SERVES = "No machine can serve your organization right now";

function Probe() {
  return <div data-testid="loc">{useLocation().pathname}</div>;
}

function Latched({ unavailable }: { unavailable: boolean }) {
  const { chatId } = useParams<{ chatId: string }>();
  return (
    <ChatSurface
      key={useSurfaceKey(chatId)}
      chatId={chatId}
      unavailable={unavailable}
      unavailableReason={unavailable ? NOTHING_SERVES : undefined}
    />
  );
}

afterEach(() => {
  cleanup();
  state.chats = [];
  state.uploads = [];
  vi.clearAllMocks();
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
});

function renderNewChat({ unavailable = false } = {}) {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route path="/chat" element={<Latched unavailable={unavailable} />} />
          <Route path="/chat/:chatId" element={<Latched unavailable={unavailable} />} />
        </Routes>
        <Probe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function prompts() {
  const listed = chatHost().emptyState?.prompts;
  if (!listed || listed.length === 0) throw new Error("the browser host offers no suggested asks");
  return listed;
}
const suggestion = (index = 0): HTMLButtonElement =>
  screen.getByRole("button", { name: new RegExp(prompts()[index]!.label) }) as HTMLButtonElement;
const field = (): HTMLTextAreaElement =>
  screen.getByRole("textbox", { name: /^message /i }) as HTMLTextAreaElement;
const pills = (): HTMLLIElement[] =>
  Array.from(screen.queryByRole("list", { name: "Uploads" })?.querySelectorAll("li") ?? []);

const PNG = new File(["\x89PNG"], "chart.png", { type: "image/png" });

function pasteImage(file: File = PNG): void {
  fireEvent.paste(field(), { clipboardData: { files: [file], getData: () => "" } });
}

function dropFile(file: File = PNG): void {
  const zone = document.querySelector(".chat-composer-fieldwrap");
  if (!zone) throw new Error("the composer has no drop zone");
  fireEvent.drop(zone, { dataTransfer: { files: [file], types: ["Files"] } });
}

function pickFile(file: File = PNG): void {
  const input = document.querySelector<HTMLInputElement>(".chat-composer-attachinput");
  if (!input) throw new Error("the composer has no attach door");
  fireEvent.change(input, { target: { files: [file] } });
}

/** Nothing reached the server: no chat opened or created, no message, no bytes. */
function expectNothingSent(): void {
  expect(ds.startChat).not.toHaveBeenCalled();
  expect(ds.createChat).not.toHaveBeenCalled();
  expect(ds.sendUserMessage).not.toHaveBeenCalled();
  expect(state.uploads).toHaveLength(0);
}

describe("a suggested ask while nothing can serve the organization", () => {
  it("is listed but not pressable, and a press opens no chat and queues no message", async () => {
    renderNewChat({ unavailable: true });
    await waitFor(() => expect(field()).toBeDisabled());
    for (let index = 0; index < prompts().length; index += 1) {
      expect(suggestion(index)).toBeDisabled();
    }
    await act(async () => {
      fireEvent.click(suggestion(0));
      fireEvent.click(suggestion(1));
    });
    expectNothingSent();
    expect(screen.getByTestId("loc")).toHaveTextContent(/^\/chat$/);
  });

  it("the composer stages nothing by any door — picker, paste or drop", async () => {
    renderNewChat({ unavailable: true });
    await waitFor(() => expect(field()).toBeDisabled());
    expect(screen.getByRole("button", { name: "Attach files" })).toBeDisabled();
    pickFile();
    pasteImage();
    dropFile();
    expect(pills()).toHaveLength(0);
    expect(field().value).toBe("");
    expectNothingSent();
  });
});

describe("a suggested ask on a composer that can send", () => {
  it("carries every staged file into the chat it opens, under the suggestion's words", async () => {
    renderNewChat();
    await waitFor(() => expect(field()).toBeEnabled());
    pasteImage();
    expect(pills().map((pill) => pill.getAttribute("data-state"))).toEqual(["held"]);

    await act(async () => {
      fireEvent.click(suggestion(0));
    });
    const words = prompts()[0]!.prefill;
    // Opened for the suggestion's words, once, and the file goes into THAT chat.
    await waitFor(() => expect(ds.startChat).toHaveBeenCalledTimes(1));
    expect(ds.startChat).toHaveBeenCalledWith(words, expect.anything());
    await waitFor(() => expect(state.uploads).toHaveLength(1));
    expect(state.uploads.map((u) => [u.chatId, u.kind, u.name])).toEqual([["c1", "image", "chart.png"]]);
    expect(ds.sendUserMessage).not.toHaveBeenCalled();

    await act(async () => state.uploads[0]!.resolve("paste-1-ab12.png"));
    await waitFor(() => expect(ds.sendUserMessage).toHaveBeenCalledTimes(1));
    expect(ds.sendUserMessage.mock.calls[0]![0]).toBe("c1");
    expect(ds.sendUserMessage.mock.calls[0]![1]).toBe(`${words} ![Image 1](paste-1-ab12.png)`);
    expect(ds.createChat).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/chat/c1"));
    // One press, one message: the hop onto the opened chat does not send it again.
    expect(ds.startChat).toHaveBeenCalledTimes(1);
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
  });

  it("over words the reader typed, writes the suggestion in front of them and sends nothing", async () => {
    renderNewChat();
    await waitFor(() => expect(field()).toBeEnabled());
    fireEvent.change(field(), { target: { value: "only the orders table" } });
    pasteImage();

    await act(async () => {
      fireEvent.click(suggestion(1));
    });
    expect(field().value).toBe(`${prompts()[1]!.prefill} only the orders table [Image 1]`);
    expect(pills()).toHaveLength(1);
    expectNothingSent();
  });

  it("on an empty composer, one press sends the suggestion as the chat's first message", async () => {
    renderNewChat();
    await waitFor(() => expect(field()).toBeEnabled());
    await act(async () => {
      fireEvent.click(suggestion(2));
    });
    await waitFor(() => expect(ds.createChat).toHaveBeenCalledTimes(1));
    expect(ds.createChat.mock.calls[0]![0]).toBe(prompts()[2]!.prefill);
    await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/chat/c2"));
    expect(within(document.querySelector(".chat-tape") as HTMLElement).getByText(prompts()[2]!.prefill)).toBeInTheDocument();
    expect(ds.createChat).toHaveBeenCalledTimes(1);
  });

  it("carries the stance the reader picked, as a typed first message does", async () => {
    // The pick the composer shows for the chat that does not exist yet.
    useChatStore.getState().setComposerPref(DRAFT_CHAT_KEY, { mode: "plan" });
    renderNewChat();
    await waitFor(() => expect(field()).toBeEnabled());
    await act(async () => {
      fireEvent.click(suggestion(0));
    });
    await waitFor(() => expect(ds.createChat).toHaveBeenCalledTimes(1));
    expect(ds.createChat.mock.calls[0]![1]).toMatchObject({ mode: "plan" });
  });
});
