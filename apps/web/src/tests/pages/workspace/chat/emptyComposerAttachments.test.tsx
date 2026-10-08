// Attaching from the composer before the chat exists.
//
// The empty composer at `/chat` had no attach door and took nothing from a
// paste: a file is staged INTO a chat's folder, and there was no chat. This
// file drives the real `ChatSurface`, store and router over a source that can
// open a chat ahead of its first message, and pins the whole path: a pasted
// image and a picked file become held pills with Send still open; Send opens
// the chat once, for the reader's words, stages every pill into it, and sends
// the message naming each by its path under the chat root; Send is closed
// while the bytes go up and open again after; a pill that fails for good is
// withdrawn with its reason while the words still go — so a chat opened for a
// message is never left without one; an open that fails keeps the draft and
// the pills; a paste with no file in it does nothing; and a source that
// cannot open a chat ahead of its message offers no door at all.

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
  reject: (e: Error) => void;
}

const { ds, state } = vi.hoisted(() => {
  const state = {
    chats: [] as Record<string, unknown>[],
    uploads: [] as UploadCall[],
    openRejection: null as Error | null,
    listeners: [] as ((event: { replay?: boolean }) => void)[],
  };
  const chatFiles = {
    upload: (chatId: string, file: File, hint: { kind: string; n: number }) =>
      new Promise<{ path: string }>((resolve, reject) => {
        state.uploads.push({
          chatId,
          kind: hint.kind,
          n: hint.n,
          name: file.name,
          resolve: (path) => resolve({ path }),
          reject,
        });
      }),
    resolveUrl: async () => null,
  };
  const ds = {
    chatFiles,
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
    subscribeChat: (_chatId: string, onEvent: (event: { replay?: boolean }) => void) => {
      state.listeners.push(onEvent);
      return () => {
        state.listeners = state.listeners.filter((entry) => entry !== onEvent);
      };
    },
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
    createChat: vi.fn(async () => {
      throw new Error("a chat opened for its files is never created a second time");
    }),
    startChat: vi.fn(async (title: string) => {
      if (state.openRejection) throw state.openRejection;
      const chat = { id: "c1", title, updatedAt: "2026-09-16T00:00:00Z" };
      state.chats = [chat];
      return chat;
    }) as ((title: string) => Promise<unknown>) | undefined,
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
import { useSurfaceKey } from "@/pages/workspace/chat/useSurfaceKey";

function Probe() {
  const location = useLocation();
  return <div data-testid="loc">{location.pathname}</div>;
}

/** The portal's mount, keyed the way ChatPage keys it. */
function Latched() {
  const { chatId } = useParams<{ chatId: string }>();
  return <ChatSurface key={useSurfaceKey(chatId)} chatId={chatId} />;
}

const startChat = ds.startChat;

afterEach(() => {
  cleanup();
  state.chats = [];
  state.uploads = [];
  state.openRejection = null;
  state.listeners = [];
  ds.startChat = startChat;
  vi.clearAllMocks();
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
});

function renderComposer() {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat"]}>
        <Routes>
          <Route path="/chat" element={<Latched />} />
          <Route path="/chat/:chatId" element={<Latched />} />
        </Routes>
        <Probe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const field = (): HTMLTextAreaElement =>
  screen.getByRole("textbox", { name: /^message /i }) as HTMLTextAreaElement;
const sendKey = (): HTMLElement => screen.getByRole("button", { name: /^Send/ });
const tape = (): HTMLElement => {
  const node = document.querySelector(".chat-tape");
  if (!(node instanceof HTMLElement)) throw new Error("no transcript tape on screen");
  return node;
};
const pills = (): HTMLLIElement[] =>
  Array.from(screen.getByRole("list", { name: "Uploads" }).querySelectorAll("li"));

const PNG = new File(["\x89PNG"], "image.png", { type: "image/png" });
const CSV = new File(["a,b\n1,2\n"], "orders.csv", { type: "text/csv" });

function pasteImage(file: File = PNG): void {
  fireEvent.paste(field(), { clipboardData: { files: [file], getData: () => "" } });
}
function pickFile(file: File = CSV): void {
  const input = document.querySelector<HTMLInputElement>(".chat-composer-attachinput");
  if (!input) throw new Error("the composer has no attach door");
  fireEvent.change(input, { target: { files: [file] } });
}

/** Type around the pills: the words first, then the files at the caret. */
async function draftWithFiles(): Promise<void> {
  await waitFor(() => expect(field()).toBeInTheDocument());
  fireEvent.change(field(), { target: { value: "compare " } });
  field().setSelectionRange(8, 8);
  pickFile();
  pasteImage();
  fireEvent.change(field(), { target: { value: `${field().value}please` } });
  expect(field().value).toBe("compare [File 1] [Image 1] please");
}

describe("attaching before the chat exists", () => {
  it("offers the door; a pasted image and a picked file are held pills, and Send stays open", async () => {
    renderComposer();
    await draftWithFiles();
    expect(pills().map((p) => p.getAttribute("data-state"))).toEqual(["held", "held"]);
    expect(state.uploads).toHaveLength(0);
    expect(ds.startChat).not.toHaveBeenCalled();
    expect(sendKey().hasAttribute("disabled")).toBe(false);
  });

  it("Send opens the chat once for the words, stages every pill into it, and sends the message naming each path", async () => {
    renderComposer();
    await draftWithFiles();
    await act(async () => {
      fireEvent.click(sendKey());
    });

    // Opened for the reader's words — never through createChat, and only once.
    await waitFor(() => expect(ds.startChat).toHaveBeenCalledTimes(1));
    expect(ds.startChat).toHaveBeenCalledWith("compare please", expect.anything());
    expect(ds.createChat).not.toHaveBeenCalled();

    // Every pill goes into THAT chat; Send is closed while they go.
    await waitFor(() => expect(state.uploads).toHaveLength(2));
    expect(state.uploads.map((u) => [u.chatId, u.kind, u.n, u.name])).toEqual([
      ["c1", "file", 1, "orders.csv"],
      ["c1", "image", 1, "image.png"],
    ]);
    expect(sendKey().hasAttribute("disabled")).toBe(true);
    expect(sendKey().getAttribute("aria-label")).toBe("Send: Waiting for uploads to finish");
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    // The draft is still the reader's, pills and all, until the bytes land.
    expect(field().value).toBe("compare [File 1] [Image 1] please");

    await act(async () => {
      state.uploads[0].resolve("file-1-cd34.csv");
      state.uploads[1].resolve("paste-1-ab12.png");
    });

    const sent = "compare [File 1: orders.csv](file-1-cd34.csv) ![Image 1](paste-1-ab12.png) please";
    await waitFor(() => expect(ds.sendUserMessage).toHaveBeenCalledTimes(1));
    expect(ds.sendUserMessage.mock.calls[0][0]).toBe("c1");
    expect(ds.sendUserMessage.mock.calls[0][1]).toBe(sent);
    // The surface hops onto the opened chat with the bubble already up and the
    // composer open again.
    await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/chat/c1"));
    expect(within(tape()).getByText(/compare/)).toBeInTheDocument();
    expect(field().value).toBe("");
    // Nothing is held for uploads any more: the key in the rail is the turn's
    // Stop, not a Send waiting on bytes.
    await waitFor(() => expect(screen.queryByRole("button", { name: /Waiting for uploads/ })).toBeNull());
    expect(screen.getByText(/^working/i)).toBeInTheDocument();
    expect(ds.startChat).toHaveBeenCalledTimes(1);
    expect(ds.createChat).not.toHaveBeenCalled();
  });

  it(
    "a pill that fails for good is withdrawn with its reason, and the words still open the chat once",
    async () => {
      renderComposer();
      await draftWithFiles();
      await act(async () => {
        fireEvent.click(sendKey());
      });
      await waitFor(() => expect(state.uploads).toHaveLength(2));
      await act(async () => state.uploads[0].resolve("file-1-cd34.csv"));
      // The image is refused on every attempt the composer makes.
      for (let attempt = 0; attempt < 3; attempt += 1) {
        await waitFor(
          () => expect(state.uploads.filter((u) => u.kind === "image")).toHaveLength(attempt + 1),
          { timeout: 3000 },
        );
        const call = state.uploads.filter((u) => u.kind === "image")[attempt];
        await act(async () => call.reject(new Error("quota exceeded")));
      }
      await waitFor(() => expect(ds.sendUserMessage).toHaveBeenCalledTimes(1), { timeout: 3000 });
      expect(ds.sendUserMessage.mock.calls[0][1]).toBe("compare [File 1: orders.csv](file-1-cd34.csv) please");
      expect(ds.startChat).toHaveBeenCalledTimes(1);
      expect(ds.createChat).not.toHaveBeenCalled();
      // The reader is told, on the surface they are looking at.
      const alerts = screen.getAllByRole("alert").map((node) => node.textContent);
      expect(alerts).toContain("image.png could not be uploaded: quota exceeded");
      await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/chat/c1"));
    },
    10_000,
  );

  it("an open that fails keeps the draft and the pills, says why, and sends nothing", async () => {
    state.openRejection = new Error("no machine is running");
    renderComposer();
    await draftWithFiles();
    await act(async () => {
      fireEvent.click(sendKey());
    });
    await waitFor(() =>
      expect(screen.getAllByRole("alert").map((node) => node.textContent)).toContain(
        "Couldn't start the chat: no machine is running",
      ),
    );
    expect(field().value).toBe("compare [File 1] [Image 1] please");
    expect(pills().map((p) => p.getAttribute("data-state"))).toEqual(["held", "held"]);
    expect(state.uploads).toHaveLength(0);
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(ds.createChat).not.toHaveBeenCalled();
    expect(screen.getByTestId("loc")).toHaveTextContent("/chat");
    expect(sendKey().hasAttribute("disabled")).toBe(false);
  });

  it("a paste with no file in it is a no-op: no pill, no chat", async () => {
    renderComposer();
    await waitFor(() => expect(field()).toBeInTheDocument());
    fireEvent.change(field(), { target: { value: "plain words" } });
    fireEvent.paste(field(), { clipboardData: { files: [], getData: () => "clipboard text" } });
    expect(field().value).toBe("plain words");
    expect(screen.queryByRole("list", { name: "Uploads" })).toBeNull();
    expect(ds.startChat).not.toHaveBeenCalled();
    expect(state.uploads).toHaveLength(0);
  });

  it("a message with no files still opens its chat by being sent, never through an open", async () => {
    ds.createChat = vi.fn(async (text: string) => {
      const chat = { id: "c2", title: text, updatedAt: "2026-09-16T00:00:00Z" };
      state.chats = [chat];
      return chat;
    }) as never;
    renderComposer();
    await waitFor(() => expect(field()).toBeInTheDocument());
    fireEvent.change(field(), { target: { value: "no files here" } });
    await act(async () => {
      fireEvent.click(sendKey());
    });
    await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/chat/c2"));
    expect(ds.createChat).toHaveBeenCalledTimes(1);
    expect(ds.startChat).not.toHaveBeenCalled();
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
  });

  it("a source that cannot open a chat ahead of its message offers no door, and a paste stages nothing", async () => {
    ds.startChat = undefined;
    renderComposer();
    await waitFor(() => expect(field()).toBeInTheDocument());
    expect(document.querySelector('input[type="file"]')).toBeNull();
    pasteImage();
    expect(field().value).toBe("");
    expect(screen.queryByRole("list", { name: "Uploads" })).toBeNull();
  });
});
