// "Share", pressed in the chat header of a browser tab.
//
// A chat is a node in the drive and its row names that node (`files_node_id`),
// so the header opens the Files sharing dialog over it, in place. It opens from
// the header's own action cluster, a glyph beside Delete, rather than a word on
// a row of its own under the chrome: the two things a reader does to the open
// chat sit together, and the word floating below the header is gone.
//
// This file mounts the REAL `ChatSurface` over the browser's own routes and
// presses the key: the URL must not move, the dialog must be addressed to the
// chat's node, and a grant made through it must show up in the roster
// afterwards. A chat with no node has nothing to share, so there is no key.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const DRIVE = "drv_1";
const NODE = "nd_chat";

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

/** What `GET /api/v1/chats/c1` answers — the node id is the whole question. */
let filesNodeId: string | null = NODE;
/** The node's direct grants, mutated by a landed POST so the refetch that the
 *  invalidation policy fires sees what the write did. */
let grants: unknown[] = [];
let asked: string[] = [];
/** What the node read calls the chat's node. A real chat is stored under its
 *  own id (`<uuid>.alkerachat`), which is precisely the name a reader must
 *  never be asked to recognise. */
let nodeNames = { name: "Quarterly plan.chat", nameDisplay: "Quarterly plan" };
/** What `GET /api/v1/chats/c1` calls the chat. */
let chatTitle = "Quarterly plan";

const MEMBERS = [{ user_id: "usr_dana", email: "dana@acme.test", display_name: "Dana Okafor" }];
const TEAMS = [{ id: "team_root", name: "Acme", is_root: true, parent_team_id: null }];

function stubApi(): void {
  asked = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const asRequest = input instanceof Request ? input : null;
      const raw = asRequest ? asRequest.url : String(input);
      const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();
      asked.push(`${method} ${raw}`);
      const url = new URL(raw, "http://localhost");
      const answer = (status: number, payload: unknown): Response =>
        new Response(payload === null ? null : JSON.stringify(payload), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/v1/chats/c1") {
        return answer(200, {
          id: "c1",
          title: chatTitle,
          owner_user_id: "usr_me",
          can_delete: true,
          machine_id: null,
          machine_status: "none",
          created_at: "2026-09-06T12:00:00Z",
          updated_at: "2026-09-06T12:00:00Z",
          last_seq: 0,
          permission_mode: "read_only",
          files_node_id: filesNodeId,
        });
      }
      if (url.pathname === "/api/v1/files/drives") {
        return answer(200, { id: DRIVE, orgId: "org_1", rootId: "nd_root", quotaBytes: 1 });
      }
      if (url.pathname.endsWith("/permissions")) {
        if (method === "POST") {
          const text = asRequest ? await asRequest.clone().text() : String(init?.body ?? "");
          const body = JSON.parse(text === "" ? "{}" : text) as {
            principal: { kind: string; id: string };
            role: string;
          };
          grants = [
            ...grants,
            {
              id: "sh_new",
              principal: body.principal,
              principalName: "Dana Okafor",
              role: body.role,
            },
          ];
          return answer(201, { id: "sh_new" });
        }
        // Nothing is inherited here: the effective set is the direct one. The
        // rungs on offer are the server's, as it answers a reader who may share.
        return answer(200, {
          value: grants,
          assignableRoles: [
            { role: "reader", label: "Can view" },
            { role: "writer", label: "Can edit" },
            { role: "manager", label: "Full access" },
          ],
        });
      }
      // The picker's directory search, answered the way the server answers it
      // for a caller who may share the node.
      if (url.pathname.endsWith("/share-candidates")) {
        const q = (url.searchParams.get("q") ?? "").trim().toLowerCase();
        const people = MEMBERS.filter(
          (one) =>
            q !== "" &&
            (one.display_name.toLowerCase().includes(q) || one.email.toLowerCase().includes(q)),
        ).map((one) => ({
          principal: { kind: "user", id: one.user_id },
          name: one.display_name,
          email: one.email,
          isOrg: false,
        }));
        const teams = TEAMS.filter((one) => q !== "" && one.name.toLowerCase().includes(q)).map(
          (one) => ({
            principal: { kind: "team", id: one.id },
            name: one.name,
            email: null,
            isOrg: one.is_root,
          }),
        );
        return answer(200, { value: [...people, ...teams] });
      }
      if (url.pathname.includes("/items/")) {
        return answer(200, {
          id: NODE,
          driveId: DRIVE,
          kind: "file",
          name: nodeNames.name,
          nameDisplay: nodeNames.nameDisplay,
          parentId: "nd_root",
          etag: "7",
          ctag: "c1",
          capabilities: { can_read: true, can_write: true, can_share: true, refusals: {} },
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

function renderChat() {
  // createQueryClient, never a bare QueryClient: the invalidation policy that
  // turns a landed grant into a refreshed roster lives on it.
  const client = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Where />
        <Routes>
          <Route path="/chat/:chatId" element={<ChatSurface chatId="c1" />} />
          <Route path="/chat" element={<p>Chat home</p>} />
          <Route path="/files" element={<p>Files</p>} />
          <Route path="*" element={<p>Page not found</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The header's action cluster — the keys that act on the open chat. Scoped,
 *  because the dialog the key opens carries a submit key of the same name. */
const actions = (): HTMLElement => {
  const group = document.querySelector(".chat-chrome__actions");
  expect(group).not.toBeNull();
  return group as HTMLElement;
};
const shareKey = (): Promise<HTMLElement> =>
  within(actions()).findByRole("button", { name: "Share" });

beforeEach(() => {
  filesNodeId = NODE;
  grants = [];
  nodeNames = { name: "Quarterly plan.chat", nameDisplay: "Quarterly plan" };
  chatTitle = "Quarterly plan";
  stubApi();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("Share, in the chat header", () => {
  it("is a glyph key in the header's own cluster, reached just before Delete", async () => {
    renderChat();
    await shareKey();

    const named = Array.from(actions().querySelectorAll("button")).map(
      (key) => key.getAttribute("aria-label") ?? "",
    );
    // Tab order through the cluster: what lets people in, then what makes
    // another chat of this one, then what throws the chat away.
    const copyKey = named.find((name) => name === "Duplicate" || name === "Copy to my drive");
    expect(copyKey).toBeDefined();
    expect(named.indexOf("Share")).toBeGreaterThanOrEqual(0);
    expect(named.indexOf("Share")).toBe(named.indexOf(copyKey ?? "") - 1);
    expect(named.indexOf(copyKey ?? "")).toBe(named.indexOf("Delete chat") - 1);
  });

  it("no longer floats as a word on a row of its own below the chrome", async () => {
    renderChat();
    await shareKey();

    // No paragraph under the header carrying the word.
    expect(document.querySelector(".chat-share")).toBeNull();
    const topbar = document.querySelector(".chat-topbar");
    if (topbar) expect(within(topbar as HTMLElement).queryByText("Share")).toBeNull();
    // And the key that remains is a glyph, not the word.
    expect((await shareKey()).textContent).toBe("");
  });

  it("keeps the top-right row for the faces of who has the chat open", async () => {
    // Presence is the browser's, and it stays where it was; only Share moved.
    const client = createQueryClient({ retry: false });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/chat/c1"]}>
          <Routes>
            <Route
              path="/chat/:chatId"
              element={<ChatSurface chatId="c1" presence={<span data-testid="presence">faces</span>} />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const row = (await screen.findByTestId("presence")).closest(".chat-topbar");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).queryByRole("button", { name: "Share" })).toBeNull();
  });

  it("opens the sharing dialog over the chat's own node without leaving the chat", async () => {
    renderChat();
    fireEvent.click(await shareKey());

    const dialog = await screen.findByRole("dialog");
    // Addressed to the chat's node, named by the node the chat's row points at.
    expect(await within(dialog).findByText(/Quarterly plan/)).toBeTruthy();
    await waitFor(() =>
      expect(
        asked.some((one) => one.includes(`/files/drives/${DRIVE}/items/${NODE}/permissions`)),
      ).toBe(true),
    );
    // Still in the chat: the reader never went to the drive listing.
    expect(screen.getByTestId("at")).toHaveTextContent("/chat/c1");
    expect(screen.queryByText("Files")).toBeNull();
  });

  it("titles the dialog with the chat, not the storage name of its node", async () => {
    // A chat's node is named by its id on disk. The dialog reading the node
    // would put a 36-character UUID in front of somebody about to hand out
    // access, so the chat's own title is what it must say.
    const storageName = "f27af3ac-a2a7-4e29-b2b3-fcea98b48e2f.alkerachat";
    nodeNames = { name: storageName, nameDisplay: storageName };
    renderChat();
    fireEvent.click(await shareKey());

    const dialog = await screen.findByRole("dialog");
    // The node read has landed (the roster is addressed by it), so the UUID is
    // absent because the title prefers the chat, not because nothing arrived.
    await waitFor(() =>
      expect(
        asked.some((one) => one.includes(`/files/drives/${DRIVE}/items/${NODE}/permissions`)),
      ).toBe(true),
    );
    expect(await within(dialog).findByText("Share “Quarterly plan”")).toBeTruthy();
    expect(within(dialog).queryByText(new RegExp(storageName))).toBeNull();
  });

  it("falls back to 'Untitled chat' when the chat has no title yet", async () => {
    chatTitle = "";
    nodeNames = { name: "x.alkerachat", nameDisplay: "x.alkerachat" };
    renderChat();
    fireEvent.click(await shareKey());
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("Share “Untitled chat”")).toBeTruthy();
  });

  it("shows a grant made through it in the roster afterwards", async () => {
    renderChat();
    fireEvent.click(await shareKey());
    const dialog = await screen.findByRole("dialog");

    const roster = within(dialog).getByRole("region", { name: "People with access" });
    expect(within(roster).queryByText("Dana Okafor")).toBeNull();

    fireEvent.change(await within(dialog).findByPlaceholderText("Name, email or team"), {
      target: { value: "Dana" },
    });
    fireEvent.click(await within(dialog).findByText("Dana Okafor"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Share" }));

    // The write landed AND the roster re-read it: the row is there because the
    // server answered with it, not because the dialog kept what was typed.
    await waitFor(() =>
      expect(asked.some((one) => one.startsWith("POST") && one.includes("/permissions"))).toBe(
        true,
      ),
    );
    await waitFor(() => expect(within(roster).getByText("Dana Okafor")).toBeTruthy());
  });

  it("offers no key at all for a chat that is not in Files", async () => {
    filesNodeId = null;
    renderChat();
    // Delete arrives on the same header, so the cluster has been built by the
    // time this asserts what is missing from it.
    await within(actions()).findByRole("button", { name: "Delete chat" });

    await waitFor(() =>
      expect(asked.some((one) => one.includes("/api/v1/chats/c1"))).toBe(true),
    );
    expect(within(actions()).queryByRole("button", { name: "Share" })).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    // And nothing was read about a node that does not exist.
    expect(asked.some((one) => one.includes("/permissions"))).toBe(false);
  });
});
