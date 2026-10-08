// Copying the open chat from its header.
//
// One key beside Share, worded by whose chat it is: the owner reads
// "Duplicate" and stays put over a second chat filed beside the first; anyone
// else reads "Copy to my drive" and is taken into the copy, which is the chat
// they will work in from here. Both press the same Files duplicate over the
// chat's own node, with no destination — the server files the copy in the
// caller's Chats folder. A chat with no node has nothing to copy and no key.
//
// This file mounts the REAL `ChatSurface` over the browser's own routes.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const DRIVE = "drv_1";
const NODE = "nd_chat";
const COPY_NODE = "nd_copy";
const COPY_CHAT = "c2";

const { ds } = vi.hoisted(() => ({
  ds: {
    listChats: async () => [{ id: "c1", title: "Chat", updatedAt: "2026-09-06T12:00:00Z" }],
    listModels: async () => [],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listCommands: async () => [],
    runCommand: async () => ({ kind: "cli_only", command: null, payload: {}, message: "" }),
    listContext: async () => ({ total: 0 }),
    lineageRoots: async () => ({}),
    getChatTurns: async () => [],
    searchFiles: async () => [],
    sendUserMessage: async () => ({ id: "m1", role: "user", content: "x" }),
    createChat: async () => ({ id: "c1", title: null, updatedAt: "" }),
    getPermissionMode: async () => "read_only",
    setPermissionMode: vi.fn(async (_chatId: string, _mode: string) => {}),
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
  },
}));

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const host = real.createBrowserChatHost({
    account: () => ({ email: "analyst@tideline.example", webAppUrl: null }),
  });
  return {
    ...real,
    chatHost: () => host,
    chatData: () => ds,
    refetchWhileErrored: () => false as const,
    refetchWhileNoChatDefault: () => false as const,
    refetchWhileErroredOrEmpty: () => false as const,
    chatCaps: () => ({
      opencodeActive: false,
      modelCatalog: true,
      permissionModes: ["read_only", "default", "plan"],
      fixedPermissionMode: "read_only",
    }),
  };
});

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";

/** Who is signed in, as `GET /api/v1/auth/me` answers. */
let me = "usr_me";
/** Who owns chat c1, as `GET /api/v1/chats/c1` answers. */
let owner = "usr_me";
let filesNodeId: string | null = NODE;
let asked: { method: string; path: string; body: unknown }[] = [];

function stubApi(): void {
  asked = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const asRequest = input instanceof Request ? input : null;
      const raw = asRequest ? asRequest.url : String(input);
      const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();
      const url = new URL(raw, "http://localhost");
      const text = asRequest ? await asRequest.clone().text() : String(init?.body ?? "");
      const body: unknown = text === "" ? null : JSON.parse(text);
      asked.push({ method, path: url.pathname, body });
      const answer = (status: number, payload: unknown): Response =>
        new Response(payload === null ? null : JSON.stringify(payload), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/v1/auth/me") {
        return answer(200, { id: me, email: "analyst@tideline.example" });
      }
      if (url.pathname === "/api/v1/chats/c1" || url.pathname === `/api/v1/chats/${COPY_CHAT}`) {
        const id = url.pathname.endsWith("c1") ? "c1" : COPY_CHAT;
        return answer(200, {
          id,
          title: id === "c1" ? "Quarterly plan" : "Quarterly plan",
          owner_user_id: id === "c1" ? owner : me,
          can_delete: true,
          machine_id: null,
          machine_status: "none",
          created_at: "2026-09-06T12:00:00Z",
          updated_at: "2026-09-06T12:00:00Z",
          last_seq: 0,
          permission_mode: "read_only",
          files_node_id: id === "c1" ? filesNodeId : COPY_NODE,
        });
      }
      if (url.pathname === "/api/v1/files/drives") {
        return answer(200, {
          id: DRIVE,
          orgId: "org_1",
          rootId: "nd_root",
          homeId: "nd_home",
          quotaBytes: 1,
        });
      }
      if (url.pathname === `/api/v1/files/drives/${DRIVE}/items/${NODE}/duplicate`) {
        return answer(201, {
          item: {
            id: COPY_NODE,
            driveId: DRIVE,
            kind: "folder",
            name: "Quarterly plan.alkerachat",
            nameDisplay: "Quarterly plan",
            parentId: "nd_chats",
            etag: "1",
            ctag: "1",
          },
          chatId: COPY_CHAT,
          objectId: COPY_CHAT,
        });
      }
      return answer(200, {});
    }),
  );
}

function Where() {
  const location = useLocation();
  return <span data-testid="at">{location.pathname}</span>;
}

function renderChat(onRenameTitle?: (title: string) => Promise<void>) {
  const client = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Where />
        <Routes>
          <Route
            path="/chat/:chatId"
            element={<ChatSurface chatId="c1" onRenameTitle={onRenameTitle} />}
          />
          <Route path="/chat" element={<p>Chat home</p>} />
          <Route path="*" element={<p>Page not found</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const actions = (): HTMLElement => {
  const group = document.querySelector(".chat-chrome__actions");
  expect(group).not.toBeNull();
  return group as HTMLElement;
};
const key = (name: string): Promise<HTMLElement> =>
  within(actions()).findByRole("button", { name });
const duplicates = () =>
  asked.filter((call) => call.method === "POST" && call.path.endsWith("/duplicate"));

beforeEach(() => {
  me = "usr_me";
  owner = "usr_me";
  filesNodeId = NODE;
  stubApi();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("copying the open chat from its header", () => {
  it("reads Duplicate for the owner, between Share and Delete", async () => {
    renderChat();
    await key("Duplicate");
    const named = Array.from(actions().querySelectorAll("button")).map(
      (button) => button.getAttribute("aria-label") ?? "",
    );
    expect(named.indexOf("Duplicate")).toBe(named.indexOf("Share") + 1);
    expect(named.indexOf("Duplicate")).toBe(named.indexOf("Delete chat") - 1);
    expect(within(actions()).queryByRole("button", { name: "Copy to my drive" })).toBeNull();
  });

  it("reads Copy to my drive for anyone who is not the owner", async () => {
    owner = "usr_someone_else";
    renderChat();
    await key("Copy to my drive");
    expect(within(actions()).queryByRole("button", { name: "Duplicate" })).toBeNull();
  });

  it("the owner names the copy first, and the name is what the request carries", async () => {
    renderChat();
    fireEvent.click(await key("Duplicate"));

    // The key asks before it copies: nothing has left the browser yet.
    const dialog = await screen.findByRole("dialog");
    expect(duplicates()).toHaveLength(0);
    const field = within(dialog).getByLabelText("Name") as HTMLInputElement;
    expect(field.value).toBe("Quarterly plan (copy)");
    fireEvent.change(field, { target: { value: "Quarterly plan v2" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Duplicate" }));

    await waitFor(() => expect(duplicates()).toHaveLength(1));
    const [call] = duplicates();
    expect(call?.path).toBe(`/api/v1/files/drives/${DRIVE}/items/${NODE}/duplicate`);
    // No destination: the server files the copy beside the source for its owner.
    expect(call?.body).toEqual({ destinationId: null, name: "Quarterly plan v2" });
  });

  it("the owner lands in the copy, which is the chat they meant to work in", async () => {
    renderChat();
    fireEvent.click(await key("Duplicate"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Duplicate" }));

    await waitFor(() => expect(duplicates()).toHaveLength(1));
    await waitFor(() => expect(screen.getByTestId("at").textContent).toBe(`/chat/${COPY_CHAT}`));
  });

  it("a non-owner's copy opens on the source's own title, and they land in it", async () => {
    owner = "usr_someone_else";
    renderChat();
    fireEvent.click(await key("Copy to my drive"));

    const dialog = await screen.findByRole("dialog");
    expect((within(dialog).getByLabelText("Name") as HTMLInputElement).value).toBe(
      "Quarterly plan",
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Duplicate" }));

    await waitFor(() => expect(duplicates()).toHaveLength(1));
    expect(duplicates()[0]?.body).toEqual({ destinationId: null, name: "Quarterly plan" });
    await waitFor(() => expect(screen.getByTestId("at").textContent).toBe(`/chat/${COPY_CHAT}`));
  });

  it("Cancel copies nothing", async () => {
    renderChat();
    fireEvent.click(await key("Duplicate"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(duplicates()).toHaveLength(0);
    expect(screen.getByTestId("at").textContent).toBe("/chat/c1");
  });

  it("a second press while the first is in flight sends nothing twice", async () => {
    renderChat();
    fireEvent.click(await key("Duplicate"));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Duplicate" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    await waitFor(() => expect(duplicates()).toHaveLength(1));
    // Settle, then make sure no late second request arrived.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(duplicates()).toHaveLength(1);
  });

  it("a chat with no node has nothing to copy and carries no key", async () => {
    filesNodeId = null;
    renderChat();
    await key("Delete chat");
    expect(within(actions()).queryByRole("button", { name: "Duplicate" })).toBeNull();
    expect(within(actions()).queryByRole("button", { name: "Copy to my drive" })).toBeNull();
  });
});

describe("the chat's own actions, from its name in the header", () => {
  const titleMenu = (): HTMLElement => screen.getByRole("menu");
  const rows = (): string[] =>
    within(titleMenu())
      .getAllByRole("menuitem")
      .map((item) => item.textContent ?? "");

  /** The copy row is offered once the chat and the drive behind it have been
   *  read, which is what the header's own key waits for too. */
  const settled = async (copy: string): Promise<void> => {
    await key(copy);
  };

  /** The identity row: the name, and the right-click that reaches its actions. */
  const identity = (): HTMLElement => document.querySelector(".chat-chrome__title-row") as HTMLElement;

  it("the name carries no kebab, and annotates nothing but itself", async () => {
    renderChat(async () => {});
    await settled("Duplicate");
    expect(screen.queryByRole("button", { name: "Chat actions" })).toBeNull();
    // The one annotation in the row is the name handing back its own full
    // reading, for the header that had to cut it. Anything else here would be a
    // second tip on the same hover.
    const annotated = Array.from(identity().querySelectorAll("[title]"));
    expect(annotated).toHaveLength(1);
    expect(annotated[0]?.getAttribute("title")).toBe(annotated[0]?.textContent);
  });

  it("a right-click on the name offers rename, duplicate and delete", async () => {
    renderChat(async () => {});
    await settled("Duplicate");
    expect(screen.queryByRole("menu")).toBeNull();
    fireEvent.contextMenu(identity());
    expect(rows()).toEqual(["Rename", "Duplicate", "Delete chat"]);
    // The same panel the Files page opens, not a second menu beside it.
    expect(titleMenu().classList.contains("alk-ctxmenu")).toBe(true);
    expect(
      within(titleMenu()).getByRole("menuitem", { name: "Delete chat" }).getAttribute("data-tone"),
    ).toBe("destructive");
  });

  it("Escape closes it and hands the name back its focus", async () => {
    renderChat(async () => {});
    await settled("Duplicate");
    const name = identity();
    name.focus();
    expect(document.activeElement).toBe(name);
    fireEvent.keyDown(name, { key: "F10", shiftKey: true });
    await waitFor(() => expect(titleMenu().contains(document.activeElement)).toBe(true));

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(document.activeElement).toBe(name);
  });

  it("Rename opens the header's own inline editor and writes through it", async () => {
    const renamed: string[] = [];
    renderChat(async (title) => {
      renamed.push(title);
    });
    await settled("Duplicate");
    fireEvent.contextMenu(identity());
    fireEvent.click(within(titleMenu()).getByRole("menuitem", { name: "Rename" }));

    // The field opens on the name the header was showing, not on an empty box.
    const field = (await screen.findByLabelText("Chat title")) as HTMLInputElement;
    expect(field.value).not.toBe("");
    fireEvent.change(field, { target: { value: "Quarterly plan v2" } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(renamed).toEqual(["Quarterly plan v2"]));
  });

  it("a reader who may not rename still gets the copy row, and no rename", async () => {
    owner = "usr_someone_else";
    renderChat();
    await settled("Copy to my drive");
    fireEvent.contextMenu(identity());
    expect(rows()).toEqual(["Copy to my drive", "Delete chat"]);
  });

  it("the menu's Duplicate asks for a name, the same question the key asks", async () => {
    renderChat(async () => {});
    await settled("Duplicate");
    fireEvent.contextMenu(identity());
    fireEvent.click(within(titleMenu()).getByRole("menuitem", { name: "Duplicate" }));

    const dialog = await screen.findByRole("dialog");
    expect((within(dialog).getByLabelText("Name") as HTMLInputElement).value).toBe(
      "Quarterly plan (copy)",
    );
  });
});
