// The chat page arranged around workspaces: the rail's grouping, a workspace's
// own page, a new chat started in the workspace on screen, the rules for
// renaming and deleting, a deep link into a chat resolving into its workspace,
// and a teammate's change reaching the rail without a reload.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { useState, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createInvalidationScheduler, type RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import { createQueryClient } from "@/api/queryClient";
import { TopbarSlotsContext } from "@/app/Topbar";
import { clearStorageMirror } from "@alkera/ui/storage";

vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: (props: {
    chatId?: string;
    presence?: ReactNode;
    notice?: ReactNode;
    newChatWorkspaceId?: string;
    waitingLabel?: string;
    onRenameTitle?: unknown;
    unavailableReason?: string;
  }) => (
    <div
      data-testid="chat-surface"
      data-reason={props.unavailableReason ?? ""}
      data-can-rename={props.onRenameTitle ? "yes" : "no"}
      data-chat-id={props.chatId ?? ""}
      data-new-chat-workspace={props.newChatWorkspaceId ?? ""}
      data-waiting-label={props.waitingLabel ?? ""}
    >
      {props.presence}
      {props.notice}
    </div>
  ),
}));
/** Every workspace the page asked to be present in, render by render. */
const presenceJoins = vi.hoisted((): (string | null)[] => []);
/** How many times the chat's faces were mounted: each mount is a roster join. */
const presenceMounts = vi.hoisted(() => ({ count: 0 }));
vi.mock("@/pages/workspace/chat/ChatPresence", async (importOriginal) => {
  const { useEffect } = await import("react");
  return {
    ...(await importOriginal<object>()),
    ChatPresence: () => {
      useEffect(() => {
        presenceMounts.count += 1;
      }, []);
      return null;
    },
  };
});
// The live roster is its own suite (`workspacePresence.test.ts`); here it
// would open a socket per case.
// The Files pane's own suite proves the pane; here it reports where it is rooted.
vi.mock("@/pages/workspace/chat/workspace/ChatSidePane", () => ({
  ChatSidePane: (props: { rootNodeId: string }) => <div data-testid="files-pane" data-root={props.rootNodeId} />,
}));
vi.mock("@/pages/workspace/workspaces/useWorkspacePresence", () => ({
  useWorkspaceViewers: (workspaceId: string | null) => {
    presenceJoins.push(workspaceId);
    return [];
  },
}));

import { ChatPage, ChatRoute, WorkspaceRoute } from "@/pages/workspace/chat/ChatPage";
import { NOWHERE_TO_RUN } from "@/pages/workspace/workspaces/NewWorkspaceDialog";
import { WORKSPACE_GONE_TITLE } from "@/pages/workspace/workspaces/WorkspacePage";
import { chatFact } from "../../../fixtures/statusFacts";

const ME = "u-me";
const THEM = "u-them";
const MAIN = "11111111-1111-4111-8111-111111111111";
const PROJECT = "22222222-2222-4222-8222-222222222222";
const SHARED = "33333333-3333-4333-8333-333333333333";
const ADOPTED = "44444444-4444-4444-8444-444444444444";
const UNKNOWN = "55555555-5555-4555-8555-555555555555";
const C_PROJECT_A = "aaaaaaaa-0000-4000-8000-000000000001";
const C_PROJECT_B = "aaaaaaaa-0000-4000-8000-000000000002";
const C_ALONE = "aaaaaaaa-0000-4000-8000-000000000003";
const C_SHARED = "aaaaaaaa-0000-4000-8000-000000000004";
const C_NEW = "aaaaaaaa-0000-4000-8000-000000000009";

function workspace(id: string, over: Record<string, unknown> = {}) {
  return {
    id,
    title: id === PROJECT ? "Q4 forecast" : id === SHARED ? "Pricing study" : "Main",
    kind: "project",
    layout: "native",
    owner_user_id: ME,
    version: 3,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    files_node_id: `node-${id}`,
    files_drive_id: "drv",
    working_node_id: `files-${id}`,
    chat_count: 0,
    machine_status: "none",
    writable: true,
    can_rename: true,
    can_delete: true,
    can_add_chat: true,
    ...over,
  };
}

function chat(id: string, title: string, workspaceId: string, over: Record<string, unknown> = {}) {
  return {
    id,
    title,
    owner_user_id: ME,
    machine_id: "m1",
    machine_status: "ready",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    last_seq: 1,
    can_delete: true,
    can_send: true,
    workspace_id: workspaceId,
    ...over,
  };
}

interface Server {
  multi: boolean;
  /** The list has not caught up with a main workspace made by its own read. */
  listLags?: boolean;
  /** How the server answers a delete, when it refuses one. */
  deleteRefusal?: { status: number; body: unknown };
  /** How many creates fail before one lands. */
  createFailures?: number;
  /** How the server answers a create, when it refuses one. */
  createRefusal?: { status: number; body: unknown };
  /** Items a test answers, by node id; `"hang"` never answers. */
  items?: Record<string, unknown>;
  /** What a folder lists, by node id, where a test gives it something. */
  folders?: Record<string, { id: string; name: string; nameDisplay: string; kind: string }[]>;
  /** The direct grants on every workspace's folder. */
  grants?: unknown[];
  /** The org machines the reader may see, and each workspace's machine read. */
  machines?: unknown[];
  workspaceMachines?: Record<string, unknown>;
  /** What `GET /machines/current` says serves the org's regular chats. */
  placement?: string;
  workspaces: ReturnType<typeof workspace>[];
  chats: ReturnType<typeof chat>[];
  calls: { method: string; path: string; body: unknown }[];
}

let server: Server;

function defaultServer(): Server {
  return {
    multi: true,
    workspaces: [
      workspace(MAIN, { kind: "main", can_delete: false, can_rename: false }),
      workspace(PROJECT),
      workspace(SHARED, { owner_user_id: THEM, can_delete: false, can_rename: false }),
      workspace(ADOPTED, { layout: "adopted", adopted_chat_id: C_ALONE, can_add_chat: false }),
    ],
    chats: [
      chat(C_PROJECT_A, "Revenue model", PROJECT),
      chat(C_PROJECT_B, "Churn drivers", PROJECT, { machine_status: "asleep" }),
      chat(C_ALONE, "Loose question", ADOPTED),
      chat(C_SHARED, "Competitor prices", SHARED, { owner_user_id: THEM }),
    ],
    calls: [],
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function stubServer(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = new URL(request ? request.url : String(input), "http://x");
      const method = (request?.method ?? init?.method ?? "GET").toUpperCase();
      const raw = request ? await request.clone().text() : (init?.body as string | undefined);
      const body = raw ? JSON.parse(raw) : null;
      const path = url.pathname;
      server.calls.push({ method, path, body });
      if (path === "/api/v1/config") {
        return json({
          product_name: "Databench",
          support_email: "s@x",
          telemetry_enabled: false,
          self_hosted: false,
          workspaces_multi_chat: server.multi,
        });
      }
      if (path === "/api/v1/auth/me") return json({ id: ME, email: "me@example.com" });
      if (path === "/api/v1/machines/current") {
        return json({ machine_id: "m1", status: server.placement ?? "ready", name: "box", reason: null });
      }
      if (path === "/api/v1/workspaces" && method === "GET") {
        const items = server.listLags ? server.workspaces.filter((w) => w.kind !== "main") : server.workspaces;
        return json({ items, next_cursor: null });
      }
      if (path === "/api/v1/workspaces" && method === "POST" && server.createRefusal) {
        return json(server.createRefusal.body, server.createRefusal.status);
      }
      if (path === "/api/v1/workspaces" && method === "POST" && (server.createFailures ?? 0) > 0) {
        server.createFailures = (server.createFailures ?? 0) - 1;
        return json({ detail: "Service unavailable" }, 503);
      }
      if (path === "/api/v1/workspaces" && method === "POST") {
        const made = workspace("66666666-6666-4666-8666-666666666666", { title: body.title });
        server.workspaces.push(made);
        return json(made, 201);
      }
      if (path === "/api/v1/org/machines") return json(server.machines ?? []);
      const wsMachine = /^\/api\/v1\/workspaces\/([^/]+)\/machine$/.exec(path);
      if (wsMachine) {
        const read = server.workspaceMachines?.[wsMachine[1] as string];
        return read ? json(read) : json({ detail: "Not found" }, 404);
      }
      if (path === "/api/v1/workspaces/main") {
        // Made on first ask, like the server's.
        let main = server.workspaces.find((w) => w.kind === "main" && w.owner_user_id === ME);
        if (!main) {
          main = workspace(MAIN, { kind: "main", can_delete: false, can_rename: false });
          server.workspaces.push(main);
        }
        return json(main);
      }
      const one = /^\/api\/v1\/workspaces\/([^/]+)$/.exec(path);
      if (one) {
        const found = server.workspaces.find((w) => w.id === one[1]);
        if (!found) return json({ detail: "Not found" }, 404);
        if (method === "DELETE" && server.deleteRefusal) {
          return json(server.deleteRefusal.body, server.deleteRefusal.status);
        }
        if (method === "DELETE") {
          server.workspaces = server.workspaces.filter((w) => w.id !== found.id);
          return new Response(null, { status: 204 });
        }
        if (method === "PATCH") {
          found.title = body.title;
          found.version += 1;
        }
        return json(found);
      }
      if (path === "/api/v1/chats" && method === "POST") {
        const made = chat(C_NEW, "", body.workspace_id ?? MAIN);
        server.chats.push(made);
        return json(made, 201);
      }
      if (path === "/api/v1/chats" && method === "GET") return json({ items: server.chats, next_cursor: null });
      if (path === "/api/v1/files/drives") return json({ id: "drv", orgId: "org", rootId: "root", quotaBytes: 1 });
      if (path === `/api/v1/files/drives/drv/items/files-${PROJECT}/children`) {
        return json({
          value: [{ id: "f-report", name: "forecast.csv", nameDisplay: "forecast.csv", kind: "file" }],
          nextMarker: null,
        });
      }
      const listed = /^\/api\/v1\/files\/drives\/drv\/items\/([^/]+)\/children$/.exec(path);
      if (listed && server.folders?.[listed[1] as string]) {
        return json({ value: server.folders[listed[1] as string], nextMarker: null });
      }
      if (/\/children$/.test(path)) return json({ value: [], nextMarker: null });
      const item = /^\/api\/v1\/files\/drives\/drv\/items\/([^/]+)$/.exec(path);
      if (item && server.items?.[item[1] as string] !== undefined) {
        const answer = server.items[item[1] as string];
        if (answer === "hang") return new Promise<Response>(() => undefined);
        return json(answer);
      }
      if (/\/permissions$/.test(path)) return json({ value: server.grants ?? [] });
      const oneChat = /^\/api\/v1\/chats\/([^/]+)$/.exec(path);
      if (oneChat) {
        const found = server.chats.find((c) => c.id === oneChat[1]);
        return found ? json(found) : json({ detail: "Not found" }, 404);
      }
      return json({});
    }),
  );
}

function Where() {
  const location = useLocation();
  return <output data-testid="where">{location.pathname + location.search}</output>;
}

/** The masthead's actions slot, where the page's own "New chat" lands. */
function Shell({ children }: { children: ReactNode }) {
  const [actions, setActions] = useState<HTMLElement | null>(null);
  return (
    <TopbarSlotsContext.Provider
      value={{ subtitle: null, actions, framed: true, setTitleHidden: () => {}, setTopbarHidden: () => {} }}
    >
      <div data-testid="masthead" ref={setActions} />
      {children}
    </TopbarSlotsContext.Provider>
  );
}

function renderAt(path: string, client: QueryClient = createQueryClient()) {
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Shell>
          <Routes>
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/chat/new" element={<ChatPage />} />
            <Route path="/chat/:chatId" element={<ChatRoute />} />
            <Route path="/workspaces/:workspaceId" element={<WorkspaceRoute />} />
          </Routes>
        </Shell>
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

const masthead = () => screen.getByTestId("masthead");

const rail = () => screen.getByRole("navigation", { name: "Chats" });

beforeEach(() => {
  server = defaultServer();
  stubServer();
  window.localStorage.clear();
  window.sessionStorage.clear();
  clearStorageMirror();
});

afterEach(() => {
  resetFrameBus();
  cleanup();
  vi.unstubAllGlobals();
});

describe("the rail, by workspace", () => {
  it("lists workspaces, the reader's own chats, and what others shared, in that order", async () => {
    renderAt("/chat/new");
    const sections = await waitFor(() => {
      const found = within(rail()).getAllByRole("region");
      expect(found.map((s) => s.getAttribute("aria-label"))).toEqual(["Workspaces", "Chats", "Shared with me"]);
      return found;
    });
    const [workspaces, chats, shared] = sections as [HTMLElement, HTMLElement, HTMLElement];
    const names = within(workspaces)
      .getAllByRole("button", { name: /^(Main|Q4 forecast)/ })
      .map((b) => b.textContent);
    // Main first, then the projects.
    expect(names).toEqual(["Main", "Q4 forecast"]);
    // A workspace of one is its chat, listed as a chat.
    expect(within(chats).getByRole("button", { name: /^Loose question/ })).toBeInTheDocument();
    expect(within(chats).queryByRole("button", { name: /Show chats/ })).toBeNull();
    expect(within(shared).getByRole("button", { name: /^Pricing study/ })).toBeInTheDocument();
  });

  it("unfolds a workspace to its chats, each with its server state", async () => {
    renderAt("/chat/new");
    const toggle = await within(rail()).findByRole("button", { name: "Show chats in Q4 forecast" });
    expect(within(rail()).queryByRole("button", { name: /^Revenue model/ })).toBeNull();
    fireEvent.click(toggle);
    const list = within(rail()).getByRole("list", { name: "Chats in Q4 forecast" });
    expect(within(list).getByRole("button", { name: /^Revenue model/ })).toBeInTheDocument();
    // A sleeping chat draws no mark at all.
    const asleep = within(list).getByRole("button", { name: /^Churn drivers/ });
    expect(within(asleep).queryByRole("img")).toBeNull();
  });

  describe("a workspace with more chats than the rail lists", () => {
    const many = (n: number) =>
      Array.from({ length: n }, (_, i) => chat(manyId(i), `Thread ${i}`, PROJECT));
    const manyId = (i: number) => `cccccccc-0000-4000-8000-${String(i).padStart(12, "0")}`;

    it("lists the first five and folds the rest behind Show more", async () => {
      server.chats = many(7);
      renderAt("/chat/new");
      fireEvent.click(await within(rail()).findByRole("button", { name: "Show chats in Q4 forecast" }));
      const list = within(rail()).getByRole("list", { name: "Chats in Q4 forecast" });
      expect(within(list).getAllByRole("button", { name: /^Thread / })).toHaveLength(5);
      expect(within(list).queryByRole("button", { name: /^Thread 5/ })).toBeNull();

      fireEvent.click(within(list).getByRole("button", { name: "Show more" }));
      expect(within(list).getAllByRole("button", { name: /^Thread / })).toHaveLength(7);

      fireEvent.click(within(list).getByRole("button", { name: "Show less" }));
      expect(within(list).getAllByRole("button", { name: /^Thread / })).toHaveLength(5);
    });

    it("never folds away a chat that is working or waiting on an answer", async () => {
      server.chats = many(8).map((c, i) =>
        i === 6
          ? { ...c, pending_turn: true, status: chatFact("working") }
          : i === 7
            ? { ...c, status: chatFact("waking") }
            : c,
      );
      renderAt("/chat/new");
      fireEvent.click(await within(rail()).findByRole("button", { name: "Show chats in Q4 forecast" }));
      const list = within(rail()).getByRole("list", { name: "Chats in Q4 forecast" });
      expect(within(list).getAllByRole("button", { name: /^Thread / }).map((b) => b.textContent)).toEqual([
        expect.stringContaining("Thread 0"),
        expect.stringContaining("Thread 1"),
        expect.stringContaining("Thread 2"),
        expect.stringContaining("Thread 3"),
        expect.stringContaining("Thread 4"),
        expect.stringContaining("Thread 6"),
        expect.stringContaining("Thread 7"),
      ]);
      expect(within(list).queryByRole("button", { name: /^Thread 5/ })).toBeNull();
    });

    it("says whether the fold is open, and which list it opens", async () => {
      server.chats = many(7);
      renderAt("/chat/new");
      fireEvent.click(await within(rail()).findByRole("button", { name: "Show chats in Q4 forecast" }));
      const list = within(rail()).getByRole("list", { name: "Chats in Q4 forecast" });
      const more = within(list).getByRole("button", { name: "Show more" });
      expect(more).toHaveAttribute("aria-expanded", "false");
      expect(more).toHaveAttribute("aria-controls", list.getAttribute("id"));
      fireEvent.click(more);
      expect(within(list).getByRole("button", { name: "Show less" })).toHaveAttribute("aria-expanded", "true");
    });

    it("offers no Show more when every chat fits", async () => {
      server.chats = many(5);
      renderAt("/chat/new");
      fireEvent.click(await within(rail()).findByRole("button", { name: "Show chats in Q4 forecast" }));
      const list = within(rail()).getByRole("list", { name: "Chats in Q4 forecast" });
      expect(within(list).getAllByRole("button", { name: /^Thread / })).toHaveLength(5);
      expect(within(list).queryByRole("button", { name: "Show more" })).toBeNull();
    });

    it("keeps the open chat listed when it sits below the fold", async () => {
      server.chats = many(8);
      renderAt(`/chat/${manyId(7)}`);
      await screen.findByRole("navigation", { name: "Workspace" });
      const list = await within(rail()).findByRole("list", { name: "Chats in Q4 forecast" });
      expect(await within(list).findByRole("button", { name: /^Thread 7/ })).toBeInTheDocument();
      expect(within(list).queryByRole("button", { name: /^Thread 6/ })).toBeNull();
      expect(within(list).getByRole("button", { name: "Show more" })).toBeInTheDocument();
    });
  });

  it("is the plain list of chats it always was when a workspace holds one chat", async () => {
    server.multi = false;
    server.workspaces = [workspace(ADOPTED, { layout: "adopted", can_add_chat: false })];
    server.chats = [chat(C_ALONE, "Loose question", ADOPTED)];
    renderAt("/chat/new");
    await within(rail()).findByRole("button", { name: /^Loose question/ });
    expect(within(rail()).queryByRole("heading")).toBeNull();
    expect(within(rail()).queryByRole("button", { name: "New workspace" })).toBeNull();
    // Nor is a main workspace made for a reader whose workspaces hold one chat.
    expect(server.calls.some((c) => c.path === "/api/v1/workspaces/main")).toBe(false);
  });

  it("shows the reader's main workspace before the list knows it exists", async () => {
    // A reader who never asked for it has none; the page asks, the server makes
    // it, and the rail draws it from that answer while the list catches up.
    server.workspaces = server.workspaces.filter((w) => w.kind !== "main");
    server.listLags = true;
    renderAt("/chat/new");
    const section = await within(rail()).findByRole("region", { name: "Workspaces" });
    expect(await within(section).findByRole("button", { name: /^Main/ })).toBeInTheDocument();
    expect(server.calls.some((c) => c.path === "/api/v1/workspaces/main")).toBe(true);
  });

  it("offers New workspace only where a workspace may hold several chats", async () => {
    renderAt("/chat/new");
    expect(await within(rail()).findByRole("button", { name: "New workspace" })).toBeInTheDocument();
  });
});

describe("what a workspace's menu offers", () => {
  async function menuOf(title: string): Promise<string[]> {
    const key = await within(rail()).findByRole("button", { name: `Actions for ${title}` });
    fireEvent.click(key);
    const menu = await screen.findByRole("menu", { name: `Actions for ${title}` });
    const labels = within(menu)
      .getAllByRole("menuitem")
      .map((item) => item.textContent ?? "");
    fireEvent.keyDown(menu, { key: "Escape" });
    return labels;
  }

  it("never offers to delete or rename the main workspace", async () => {
    // Even a policy answer that would allow it is not drawn: main is never deleted.
    server.workspaces[0] = workspace(MAIN, { kind: "main", can_delete: true, can_rename: true });
    renderAt("/chat/new");
    const labels = await menuOf("Main");
    expect(labels.some((l) => /Delete/.test(l))).toBe(false);
    expect(labels.some((l) => /Rename/.test(l))).toBe(false);
    expect(labels.some((l) => /Share/.test(l))).toBe(true);
  });

  it("offers rename and delete on a project the reader may change", async () => {
    renderAt("/chat/new");
    const labels = await menuOf("Q4 forecast");
    expect(labels.some((l) => /Rename/.test(l))).toBe(true);
    expect(labels.some((l) => /Delete/.test(l))).toBe(true);
  });

  it("offers neither on a workspace shared with the reader that the server says they may not change", async () => {
    renderAt("/chat/new");
    const labels = await menuOf("Pricing study");
    expect(labels.some((l) => /Rename/.test(l))).toBe(false);
    expect(labels.some((l) => /Delete/.test(l))).toBe(false);
  });

  it("deletes a project after the reader confirms", async () => {
    renderAt(`/workspaces/${PROJECT}`);
    const page = await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    expect(page).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    expect(server.calls.some((c) => c.method === "DELETE")).toBe(false);
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(server.calls).toContainEqual({ method: "DELETE", path: `/api/v1/workspaces/${PROJECT}`, body: null }),
    );
    // The page it was on is gone, so the reader is not left on it.
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/chat/new"));
  });

  it("renames a project naming the version it read", async () => {
    renderAt(`/workspaces/${PROJECT}`);
    await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    const field = screen.getByRole("textbox", { name: "Workspace title" });
    fireEvent.change(field, { target: { value: "Q4 plan" } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() =>
      expect(server.calls).toContainEqual({
        method: "PATCH",
        path: `/api/v1/workspaces/${PROJECT}`,
        body: { title: "Q4 plan", expected_version: 3 },
      }),
    );
    expect(await screen.findByRole("heading", { name: "Q4 plan", level: 1 })).toBeInTheDocument();
  });
});

describe("deleting a workspace other people can open", () => {
  const grant = (kind: string, id: string, role = "reader") => ({
    id: `sh-${id}`,
    principal: { kind, id },
    principalName: id,
    role,
    origin: "direct",
    grantingNodeId: null,
  });

  it("says who else loses it before the reader confirms", async () => {
    server.grants = [grant("user", ME, "owner"), grant("user", "bea"), grant("user", "sam"), grant("team", "analytics")];
    renderAt(`/workspaces/${PROJECT}`);
    await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/Shared with 2 people and 1 team\./)).toBeInTheDocument();
    expect(within(dialog).getByText(/Its chats and files move to Trash\./)).toBeInTheDocument();
  });

  it("says nothing about sharing for a workspace nobody else can open", async () => {
    server.grants = [grant("user", ME, "owner")];
    renderAt(`/workspaces/${PROJECT}`);
    await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() =>
      expect(server.calls.some((c) => c.path.endsWith(`/items/node-${PROJECT}/permissions`))).toBe(true),
    );
    expect(within(dialog).queryByText(/Shared with/)).toBeNull();
    expect(within(dialog).getByText("Its chats and files move to Trash.")).toBeInTheDocument();
  });
});

describe("a refused or repeated workspace write", () => {
  it.each([
    [
      "the project limit, in the server's words",
      { error: { code: "workspace_project_cap_reached", message: "You've reached the limit of 200 project workspaces." } },
      "You've reached the limit of 200 project workspaces.",
    ],
    [
      "any other conflict as a changed workspace",
      { error: { code: "client_id_in_use", message: "This client_id already made a workspace with another title" } },
      "This workspace changed since you opened it; try again.",
    ],
  ])("says why a create was refused: %s", async (_case, body, said) => {
    server.createRefusal = { status: 409, body };
    renderAt("/chat/new");
    fireEvent.click(await within(rail()).findByRole("button", { name: "New workspace" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), { target: { value: "Board deck" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(await within(dialog).findByText(said)).toBeInTheDocument();
    expect(screen.getByTestId("where").textContent).toBe("/chat/new");
  });

  it("says why a delete was refused and leaves the reader where they were", async () => {
    server.deleteRefusal = {
      status: 403,
      body: { code: "forbidden", message: "Only the owner can delete this workspace" },
    };
    renderAt(`/workspaces/${PROJECT}`);
    await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Only the owner can delete this workspace");
    expect(screen.getByTestId("where").textContent).toBe(`/workspaces/${PROJECT}`);
  });

  it("sends one client id for every try from one dialog, so a retry makes one workspace", async () => {
    server.createFailures = 1;
    renderAt("/chat/new");
    fireEvent.click(await within(rail()).findByRole("button", { name: "New workspace" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), { target: { value: "Board deck" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    await within(dialog).findByText(/could not be made|unavailable/i);
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(screen.getByTestId("where").textContent).toBe("/workspaces/66666666-6666-4666-8666-666666666666"),
    );
    const ids = server.calls
      .filter((c) => c.method === "POST" && c.path === "/api/v1/workspaces")
      .map((c) => (c.body as { client_id: string }).client_id);
    expect(ids).toHaveLength(2);
    expect(ids[0]).toBeTruthy();
    expect(ids[1]).toBe(ids[0]);
  });
});

describe("the machine a workspace runs on", () => {
  const LAB_CARD = {
    kind: "org_machine",
    org_machine_id: "om-lab",
    name: "A100 Lab",
    spec: { offering_name: "A100", provider: "runpod", region: "US-TX", gpu: { name: "A100", count: 1, memory_gb: 80 }, vcpu: 16, memory_gb: 125, disk_gb: 200, rate_per_minute_nanos: 31_500_000, storage_rate_per_minute_nanos: 0 },
    state: "running",
    step: null,
    step_started_at: null,
    step_expected_seconds: null,
    stop_reason: "",
  };
  const machine = (over: Record<string, unknown>) => ({
    id: "om-lab",
    name: "A100 Lab",
    card: LAB_CARD,
    use_mode: "assigned",
    can_use: true,
    can_manage: false,
    audience: [],
    ...over,
  });

  /** Name a workspace and create it, picking `pick` under "Run on" first, or
   *  with `shown` set, waiting for the field to show that choice untouched. */
  async function create(pick: string | null, shown?: string) {
    renderAt("/chat/new");
    fireEvent.click(await within(rail()).findByRole("button", { name: "New workspace" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), { target: { value: "Fine-tuning" } });
    if (shown) {
      await waitFor(() => expect(within(dialog).getByLabelText("Run on")).toHaveTextContent(shown));
    }
    if (pick) {
      await userEvent.click(await within(dialog).findByLabelText("Run on"));
      await userEvent.click(await screen.findByRole("option", { name: pick }));
    }
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(server.calls.some((c) => c.method === "POST" && c.path === "/api/v1/workspaces")).toBe(true));
    return server.calls.find((c) => c.method === "POST" && c.path === "/api/v1/workspaces")!.body as Record<string, unknown>;
  }

  it("pins a new workspace to the machine picked, offering only machines the reader may use", async () => {
    server.machines = [machine({}), machine({ id: "om-other", name: "Other", card: { ...LAB_CARD, org_machine_id: "om-other", name: "Other" }, can_use: false })];
    renderAt("/chat/new");
    fireEvent.click(await within(rail()).findByRole("button", { name: "New workspace" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(await within(dialog).findByLabelText("Run on"));
    expect(await screen.findByRole("option", { name: "Standard" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "A100 Lab · Running" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Other/ })).toBeNull();
    cleanup();

    const body = await create("A100 Lab · Running");
    expect(body).toMatchObject({ title: "Fine-tuning", machine_pin: "om-lab" });
  });

  it("sends no pin for the org's default", async () => {
    server.machines = [machine({})];
    const body = await create(null);
    expect(body).not.toHaveProperty("machine_pin");
  });

  it("starts on the org's default machine, sent as the reader's pick", async () => {
    server.machines = [machine({ org_default: true })];
    const body = await create(null, "A100 Lab · Running");
    expect(body).toMatchObject({ machine_pin: "om-lab" });
  });

  it("sends the default placement as null when the reader moves off the org's default machine", async () => {
    server.machines = [machine({ org_default: true })];
    const body = await create("Standard");
    expect(body).toHaveProperty("machine_pin", null);
  });

  it("leaves the choice to the server when the org's default is not a machine the reader is offered", async () => {
    server.machines = [
      machine({}),
      machine({ id: "om-other", name: "Other", card: { ...LAB_CARD, org_machine_id: "om-other", name: "Other" }, can_use: false, org_default: true }),
    ];
    const body = await create(null, "Standard");
    expect(body).not.toHaveProperty("machine_pin");
  });

  it("offers no default placement when nothing serves the org's chats, and starts on the first machine", async () => {
    server.placement = "none";
    server.machines = [machine({}), machine({ id: "om-b", name: "B Lab", card: { ...LAB_CARD, org_machine_id: "om-b", name: "B Lab" } })];
    renderAt("/chat/new");
    fireEvent.click(await within(rail()).findByRole("button", { name: "New workspace" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(server.calls.some((c) => c.path === "/api/v1/machines/current")).toBe(true));
    await waitFor(() => expect(within(dialog).getByLabelText("Run on")).toHaveTextContent("A100 Lab · Running"));
    await userEvent.click(await within(dialog).findByLabelText("Run on"));
    expect(await screen.findByRole("option", { name: "B Lab · Running" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Standard" })).toBeNull();
    cleanup();

    const body = await create(null, "A100 Lab · Running");
    expect(body).toMatchObject({ machine_pin: "om-lab" });
  });

  it("starts on the org's default machine when nothing else serves the org's chats", async () => {
    server.placement = "none";
    server.machines = [machine({}), machine({ id: "om-b", name: "B Lab", card: { ...LAB_CARD, org_machine_id: "om-b", name: "B Lab" }, org_default: true })];
    const body = await create(null, "B Lab · Running");
    expect(body).toMatchObject({ machine_pin: "om-b" });
  });

  it("says there is nowhere to run when nothing serves the org and the reader may use no machine", async () => {
    server.placement = "none";
    server.machines = [];
    renderAt("/chat/new");
    fireEvent.click(await within(rail()).findByRole("button", { name: "New workspace" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(NOWHERE_TO_RUN)).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Run on")).toBeNull();
  });

  it("asks only for a name when the reader may use no machine", async () => {
    server.machines = [];
    renderAt("/chat/new");
    fireEvent.click(await within(rail()).findByRole("button", { name: "New workspace" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(server.calls.some((c) => c.path === "/api/v1/org/machines")).toBe(true));
    expect(within(dialog).queryByLabelText("Run on")).toBeNull();
    expect(within(dialog).queryByText(NOWHERE_TO_RUN)).toBeNull();
  });

  it("carries the workspace's machine in its page header", async () => {
    server.workspaceMachines = {
      [PROJECT]: { card: LAB_CARD, pin: "om-lab", active_move: null, can_move: false, targets: [] },
    };
    renderAt(`/workspaces/${PROJECT}`);
    const heading = await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    const head = heading.closest(".ws-page__head") as HTMLElement;
    expect(await within(head).findByRole("button", { name: "A100 Lab, Running" })).toBeInTheDocument();
  });
});

describe("starting things", () => {
  it("opens the empty composer aimed at the workspace, and makes nothing yet", async () => {
    renderAt(`/workspaces/${PROJECT}`);
    const heading = await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    const page = heading.closest(".ws-page") as HTMLElement;
    // One New chat on a workspace's page: the masthead does not repeat it.
    expect(within(page).getAllByRole("button", { name: "New chat" })).toHaveLength(1);
    expect(within(masthead()).queryByRole("button", { name: "New chat" })).toBeNull();
    fireEvent.click(within(page).getByRole("button", { name: "New chat" }));
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe(`/chat/new?workspace=${PROJECT}`));
    // The composer's first send makes the chat in this workspace; until then
    // there is no empty chat in anybody's rail.
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => expect(surface.getAttribute("data-new-chat-workspace")).toBe(PROJECT));
    expect(screen.getByText("In Q4 forecast")).toBeInTheDocument();
    expect(server.calls.some((c) => c.method === "POST" && c.path === "/api/v1/chats")).toBe(false);
  });

  it("starts a chat in a workspace from its row in the rail", async () => {
    renderAt(`/chat/${C_PROJECT_A}`);
    fireEvent.click(await within(rail()).findByRole("button", { name: "New chat in Q4 forecast" }));
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe(`/chat/new?workspace=${PROJECT}`));
    expect(server.calls.some((c) => c.method === "POST" && c.path === "/api/v1/chats")).toBe(false);
  });

  it("offers no new chat on the row of a workspace that cannot take one", async () => {
    server.workspaces[1] = { ...server.workspaces[1]!, can_add_chat: false };
    renderAt(`/chat/${C_PROJECT_A}`);
    await within(rail()).findByRole("button", { name: "Actions for Q4 forecast" });
    expect(within(rail()).queryByRole("button", { name: "New chat in Q4 forecast" })).toBeNull();
    expect(within(rail()).getByRole("button", { name: "New chat in Main" })).toBeInTheDocument();
  });

  it("aims the header's New chat at the open chat's workspace", async () => {
    renderAt(`/chat/${C_PROJECT_A}`);
    await screen.findByRole("navigation", { name: "Workspace" });
    fireEvent.click(within(masthead()).getByRole("button", { name: "New chat" }));
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe(`/chat/new?workspace=${PROJECT}`));
    expect(server.calls.some((c) => c.method === "POST" && c.path === "/api/v1/chats")).toBe(false);
  });

  it("ignores an aim at a workspace that cannot take a chat", async () => {
    server.workspaces[1] = { ...server.workspaces[1]!, can_add_chat: false };
    renderAt(`/chat/new?workspace=${PROJECT}`);
    const surface = await screen.findByTestId("chat-surface");
    await within(rail()).findByRole("button", { name: /^Q4 forecast/ });
    expect(surface.getAttribute("data-new-chat-workspace")).toBe("");
    expect(screen.queryByText("In Q4 forecast")).toBeNull();
  });

  it("goes to the empty composer, creating nothing, from a chat that is its own workspace", async () => {
    renderAt(`/chat/${C_ALONE}`);
    await screen.findByTestId("chat-surface");
    await within(rail()).findByRole("button", { name: /^Loose question/ });
    fireEvent.click(within(masthead()).getByRole("button", { name: "New chat" }));
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/chat/new"));
    expect(server.calls.some((c) => c.method === "POST" && c.path === "/api/v1/chats")).toBe(false);
  });

  it("makes a project workspace from its name and opens it", async () => {
    renderAt("/chat/new");
    fireEvent.click(await within(rail()).findByRole("button", { name: "New workspace" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), { target: { value: "Board deck" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(screen.getByTestId("where").textContent).toBe("/workspaces/66666666-6666-4666-8666-666666666666"),
    );
    const post = server.calls.find((c) => c.method === "POST" && c.path === "/api/v1/workspaces");
    expect(post?.body).toMatchObject({ title: "Board deck" });
  });
});

describe("sharing a chat that sits in a workspace", () => {
  async function openMenu(rowName: RegExp, title: string): Promise<HTMLElement> {
    await within(rail()).findByRole("button", { name: rowName });
    fireEvent.click(within(rail()).getByRole("button", { name: `Actions for ${title}` }));
    return screen.findByRole("menu", { name: `Actions for ${title}` });
  }

  it("offers the workspace's share, not the chat's, for a chat in a workspace of several", async () => {
    renderAt(`/chat/${C_PROJECT_A}`);
    const menu = await openMenu(/^Revenue model/, "Revenue model");
    expect(within(menu).queryByRole("menuitem", { name: "Share…" })).toBeNull();
    fireEvent.click(within(menu).getByRole("menuitem", { name: "Share workspace…" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("Q4 forecast");
    // The dialog reads the WORKSPACE folder's grants, never the chat's.
    await waitFor(() =>
      expect(server.calls.some((c) => c.path.endsWith(`/items/node-${PROJECT}/permissions`))).toBe(true),
    );
    expect(server.calls.some((c) => c.path.includes(`/items/node-${C_PROJECT_A}`))).toBe(false);
  });

  it("still shares a chat that is its own workspace as the chat", async () => {
    server.chats = server.chats.map((c) => (c.id === C_ALONE ? { ...c, files_node_id: "node-alone" } : c));
    renderAt(`/chat/${C_ALONE}`);
    const menu = await openMenu(/^Loose question/, "Loose question");
    expect(within(menu).queryByRole("menuitem", { name: "Share workspace…" })).toBeNull();
    fireEvent.click(within(menu).getByRole("menuitem", { name: "Share…" }));
    await screen.findByRole("dialog");
    await waitFor(() =>
      expect(server.calls.some((c) => c.path.endsWith("/items/node-alone/permissions"))).toBe(true),
    );
  });
});

describe("the chat's Files pane", () => {
  // A wide window, where the pane is open unasked.
  beforeEach(() => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("prefers-reduced-motion") || query.includes("min-width: 1280px"),
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }));
  });

  it("is rooted at the workspace's shared tree when the chat read names one", async () => {
    server.chats = server.chats.map((c) =>
      c.id === C_PROJECT_A
        ? { ...c, files_node_id: "node-chat-a", workspace_layout: "native", workspace_files_node_id: "node-ws-files" }
        : c,
    );
    renderAt(`/chat/${C_PROJECT_A}`);
    expect((await screen.findByTestId("files-pane")).getAttribute("data-root")).toBe("node-ws-files");
  });

  it("keeps the chat's own folder where the server names no shared tree", async () => {
    server.chats = server.chats.map((c) => (c.id === C_PROJECT_A ? { ...c, files_node_id: "node-chat-a" } : c));
    renderAt(`/chat/${C_PROJECT_A}`);
    expect((await screen.findByTestId("files-pane")).getAttribute("data-root")).toBe("node-chat-a");
  });
});

describe("renaming the open chat", () => {
  it("is offered to a reader whose rung allows it", async () => {
    server.chats = server.chats.map((c) => (c.id === C_PROJECT_A ? { ...c, files_node_id: "node-a" } : c));
    server.items = { "node-a": { id: "node-a", name: "a.alkerachat", kind: "folder", capabilities: { can_write: true } } };
    renderAt(`/chat/${C_PROJECT_A}`);
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => expect(surface.getAttribute("data-can-rename")).toBe("yes"));
  });

  it("is not offered to a viewer", async () => {
    server.chats = server.chats.map((c) => (c.id === C_PROJECT_A ? { ...c, files_node_id: "node-a" } : c));
    server.items = { "node-a": { id: "node-a", name: "a.alkerachat", kind: "folder", capabilities: { can_write: false } } };
    renderAt(`/chat/${C_PROJECT_A}`);
    const surface = await screen.findByTestId("chat-surface");
    await screen.findByRole("navigation", { name: "Workspace" });
    expect(surface.getAttribute("data-can-rename")).toBe("no");
  });

  it("is not offered while the reader's rung has not answered", async () => {
    server.chats = server.chats.map((c) => (c.id === C_PROJECT_A ? { ...c, files_node_id: "node-a" } : c));
    server.items = { "node-a": "hang" };
    renderAt(`/chat/${C_PROJECT_A}`);
    const surface = await screen.findByTestId("chat-surface");
    await screen.findByRole("navigation", { name: "Workspace" });
    expect(surface.getAttribute("data-can-rename")).toBe("no");
  });
});

describe("where a new chat lands", () => {
  it("says so when a link aims at a workspace the reader cannot use, rather than land in Main", async () => {
    renderAt(`/chat/new?workspace=${UNKNOWN}`);
    await screen.findByTestId("chat-surface");
    expect(await screen.findByText("You don't have access to that workspace.")).toBeInTheDocument();
    expect(screen.queryByText("In Main")).toBeNull();
  });

  it("names the main workspace above the plain empty composer when chats land there", async () => {
    renderAt("/chat/new");
    await screen.findByTestId("chat-surface");
    expect(await screen.findByText("In Main")).toBeInTheDocument();
  });

  it("names no place where a workspace holds one chat", async () => {
    server.multi = false;
    renderAt("/chat/new");
    await screen.findByTestId("chat-surface");
    await within(rail()).findByRole("button", { name: /^Loose question/ });
    expect(screen.queryByText(/^In /)).toBeNull();
  });
});

describe("the rail remembers what it opened", () => {
  it("keeps a workspace unfolded after the reader moves to another", async () => {
    renderAt(`/chat/${C_PROJECT_A}`);
    await within(rail()).findByRole("list", { name: "Chats in Q4 forecast" });
    fireEvent.click(within(rail()).getByRole("button", { name: /^Main/ }));
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe(`/workspaces/${MAIN}`));
    expect(within(rail()).getByRole("list", { name: "Chats in Q4 forecast" })).toBeInTheDocument();
    expect(within(rail()).getByRole("list", { name: "Chats in Main" })).toBeInTheDocument();
  });
});

describe("a sleeping chat", () => {
  it("draws no line of its own while it rests", async () => {
    server.chats = server.chats.map((c) => (c.id === C_PROJECT_B ? { ...c, status: chatFact("asleep") } : c));
    renderAt(`/chat/${C_PROJECT_B}`);
    const surface = await screen.findByTestId("chat-surface");
    await screen.findByRole("navigation", { name: "Workspace" });
    expect(surface.getAttribute("data-waiting-label")).toBe("");
    expect(within(surface).queryByText(/A message wakes it/)).toBeNull();
  });

  it("says the server's waking sentence only on the working line once somebody opened it", async () => {
    server.chats = server.chats.map((c) =>
      c.id === C_PROJECT_B ? { ...c, wake_requested_at: "2026-10-04T10:00:00Z", status: chatFact("waking") } : c,
    );
    renderAt(`/chat/${C_PROJECT_B}`);
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => expect(surface.getAttribute("data-waiting-label")).toBe("Waking the chat."));
    expect(within(surface).queryByText("Waking the chat.")).toBeNull();
  });

  it("says Starting on the working line while a new chat's first session opens", async () => {
    server.chats = server.chats.map((c) =>
      c.id === C_PROJECT_B ? { ...c, pending_turn: true, status: chatFact("new_chat") } : c,
    );
    renderAt(`/chat/${C_PROJECT_B}`);
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => expect(surface.getAttribute("data-waiting-label")).toBe("Starting the chat."));
  });

  it("carries no sleep line on an awake chat", async () => {
    renderAt(`/chat/${C_PROJECT_A}`);
    const surface = await screen.findByTestId("chat-surface");
    await screen.findByRole("navigation", { name: "Workspace" });
    expect(within(surface).queryByText(/Asleep\.|Waking/)).toBeNull();
    expect(surface.getAttribute("data-waiting-label")).toBe("");
  });
});

describe("a workspace page's files and people", () => {
  it("draws nothing for a workspace whose every chat is asleep, never Awake", async () => {
    server.chats = server.chats.map((c) =>
      c.workspace_id === PROJECT ? { ...c, machine_status: "ready", session_state: "asleep", last_activity_at: "2026-10-04T09:00:00Z" } : c,
    );
    renderAt(`/workspaces/${PROJECT}`);
    const heading = await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    const line = heading.parentElement as HTMLElement;
    expect(within(line).queryByRole("img")).toBeNull();
    expect(within(line).queryByText("Awake")).toBeNull();
  });

  it("draws no Here now section when nobody else is in the workspace", async () => {
    renderAt(`/workspaces/${PROJECT}`);
    await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    expect(screen.queryByRole("heading", { name: "Here now" })).toBeNull();
    expect(screen.queryByText("Nobody else is here.")).toBeNull();
  });
});

describe("a workspace's own page", () => {
  it("lists its chats with their state, and opens its files in Files", async () => {
    renderAt(`/workspaces/${PROJECT}`);
    const heading = await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    const page = heading.closest(".ws-page") as HTMLElement;
    const chats = within(page).getByRole("region", { name: "Chats" });
    expect(within(chats).getByRole("button", { name: /Revenue model/ })).toBeInTheDocument();
    const asleep = within(chats).getByRole("button", { name: /Churn drivers/ });
    expect(within(asleep).queryByRole("img")).toBeNull();
    // Files is one key to the workspace's folder, not a listing of it.
    expect(within(page).getByRole("link", { name: "Open Files" })).toHaveAttribute("href", `/files/files-${PROJECT}`);
    expect(within(page).queryByRole("region", { name: "Files" })).toBeNull();
    expect(within(page).queryByRole("link", { name: "forecast.csv" })).toBeNull();
  });

  it("offers no new chat in a workspace the server says cannot take one", async () => {
    server.workspaces[1] = { ...server.workspaces[1]!, can_add_chat: false };
    renderAt(`/workspaces/${PROJECT}`);
    const heading = await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    const page = heading.closest(".ws-page") as HTMLElement;
    expect(within(page).queryByRole("button", { name: "New chat" })).toBeNull();
  });
});

describe("addresses", () => {
  it("resolves an old /chat/<id> link into its workspace", async () => {
    renderAt(`/chat/${C_PROJECT_A}`);
    const crumb = await screen.findByRole("navigation", { name: "Workspace" });
    expect(within(crumb).getByRole("link", { name: "Q4 forecast" })).toHaveAttribute(
      "href",
      `/workspaces/${PROJECT}`,
    );
    // The crumb names the workspace only; its other chats are in the rail.
    expect(within(crumb).getAllByRole("link")).toHaveLength(1);
    // And the rail has the workspace unfolded around it.
    expect(within(rail()).getByRole("list", { name: "Chats in Q4 forecast" })).toBeInTheDocument();
  });

  it("keeps the chat's faces mounted when the workspace above it is drawn", async () => {
    // The crumb arrives once the workspace list answers. Remounting the faces
    // then drops the reader's roster join, and the rejoin against a channel the
    // page already holds is never confirmed, so the reader vanishes from
    // everyone else's view of the chat.
    presenceMounts.count = 0;
    renderAt(`/chat/${C_PROJECT_A}`);
    await screen.findByRole("navigation", { name: "Workspace" });
    expect(presenceMounts.count).toBe(1);
  });

  it("is present in the workspace while one of its chats is open, and in none for a chat alone", async () => {
    presenceJoins.length = 0;
    renderAt(`/chat/${C_PROJECT_A}`);
    await screen.findByRole("navigation", { name: "Workspace" });
    expect(presenceJoins.at(-1)).toBe(PROJECT);
    cleanup();
    presenceJoins.length = 0;
    renderAt(`/chat/${C_ALONE}`);
    await within(rail()).findByRole("button", { name: /^Loose question/ });
    expect(presenceJoins.every((id) => id === null)).toBe(true);
  });

  it("is present in the workspace whose page is open", async () => {
    presenceJoins.length = 0;
    renderAt(`/workspaces/${PROJECT}`);
    await screen.findByRole("heading", { name: "Q4 forecast", level: 1 });
    expect(presenceJoins.at(-1)).toBe(PROJECT);
  });

  it("draws no workspace above a chat that is its own workspace", async () => {
    renderAt(`/chat/${C_ALONE}`);
    await screen.findByTestId("chat-surface");
    await within(rail()).findByRole("button", { name: /^Loose question/ });
    expect(screen.queryByRole("navigation", { name: "Workspace" })).toBeNull();
  });

  it("says a workspace that is not there is not there", async () => {
    renderAt(`/workspaces/${UNKNOWN}`);
    expect(await screen.findByText(WORKSPACE_GONE_TITLE)).toBeInTheDocument();
  });

  it("refuses an address that cannot name a workspace without asking the server", async () => {
    renderAt("/workspaces/not-an-id");
    await screen.findByText(/not found|doesn't exist|page/i);
    expect(server.calls.some((c) => c.path.startsWith("/api/v1/workspaces/not-an-id"))).toBe(false);
  });
});

describe("a workspace shared or unshared while the page is open", () => {
  it("appears once its chats do, without the workspace list following every file frame", async () => {
    const shared = server.workspaces.find((w) => w.id === SHARED)!;
    const sharedChat = server.chats.find((c) => c.id === C_SHARED)!;
    server.workspaces = server.workspaces.filter((w) => w.id !== SHARED);
    server.chats = server.chats.filter((c) => c.id !== C_SHARED);
    const client = renderAt("/chat/new");
    await within(rail()).findByRole("button", { name: /^Q4 forecast/ });
    expect(within(rail()).queryByRole("button", { name: /^Pricing study/ })).toBeNull();
    const listReads = () => server.calls.filter((c) => c.method === "GET" && c.path === "/api/v1/workspaces").length;

    // A box saving a file: a node frame. The chat list re-reads; nothing about
    // it disagrees with the workspace list, so that list is not asked again.
    const scheduler = createInvalidationScheduler(client, { debounceMs: 0 });
    const before = listReads();
    act(() => {
      scheduler.push({ type: "file_node.changed", entity: "file_node", entity_id: "n-1", version: 1, org_id: "org" });
      scheduler.flush();
    });
    await waitFor(() => expect(server.calls.filter((c) => c.path === "/api/v1/chats").length).toBeGreaterThan(1));
    expect(listReads()).toBe(before);

    // The owner shares the workspace: the same kind of frame, and now the chat
    // list carries a chat in a workspace the rail does not know.
    server.workspaces.push(shared);
    server.chats.push(sharedChat);
    act(() => {
      scheduler.push({ type: "file_node.changed", entity: "file_node", entity_id: "node-ws", version: 2, org_id: "org" });
      scheduler.flush();
    });
    expect(await within(rail()).findByRole("button", { name: /^Pricing study/ })).toBeInTheDocument();
    expect(listReads()).toBe(before + 1);
    scheduler.dispose();
  });
});

/** A person's change to a node: what a share made or revoked arrives as. */
const shareFrame = (n: number): RealtimeEventFrame => ({
  type: "file_node.changed",
  entity: "file_node",
  entity_id: "node-ws",
  version: n,
  org_id: "org",
});

describe("a share made or revoked on an open page", () => {
  it("adds a workspace shared with no chat in it, and takes it away when revoked", async () => {
    const shared = server.workspaces.find((w) => w.id === SHARED)!;
    server.workspaces = server.workspaces.filter((w) => w.id !== SHARED);
    server.chats = server.chats.filter((c) => c.id !== C_SHARED);
    renderAt("/chat/new");
    await within(rail()).findByRole("button", { name: /^Q4 forecast/ });

    server.workspaces.push(shared);
    act(() => publishFrame(shareFrame(1)));
    expect(await within(rail()).findByRole("button", { name: /^Pricing study/ })).toBeInTheDocument();

    server.workspaces = server.workspaces.filter((w) => w.id !== SHARED);
    // Past the throttle window, a revoke is a fresh change.
    await new Promise((resolve) => setTimeout(resolve, 10));
    act(() => publishFrame(shareFrame(2)));
    await waitFor(
      () => expect(within(rail()).queryByRole("button", { name: /^Pricing study/ })).toBeNull(),
      { timeout: 15_000 },
    );
  }, 20_000);

  it("never re-reads the workspace list for a box's saves", async () => {
    renderAt("/chat/new");
    await within(rail()).findByRole("button", { name: /^Q4 forecast/ });
    const reads = () => server.calls.filter((c) => c.method === "GET" && c.path === "/api/v1/workspaces").length;
    const before = reads();
    for (let n = 0; n < 5; n += 1) act(() => publishFrame({ ...shareFrame(n), reason: "live_saved" }));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(reads()).toBe(before);
  });

  it("re-reads the open chat when its workspace is no longer shared, and says so plainly", async () => {
    // Bea, a collaborator, started this chat in somebody else's workspace.
    server.workspaces[1] = { ...server.workspaces[1]!, owner_user_id: THEM };
    server.chats = server.chats.map((c) => (c.id === C_PROJECT_A ? { ...c, files_node_id: "node-a" } : c));
    server.items = { "node-a": { id: "node-a", name: "a.alkerachat", kind: "folder", capabilities: { can_write: true } } };
    renderAt(`/chat/${C_PROJECT_A}`);
    const surface = await screen.findByTestId("chat-surface");
    await waitFor(() => expect(surface.getAttribute("data-can-rename")).toBe("yes"));

    // The owner revokes: the workspace leaves this reader's list, and the
    // chat's folder no longer lets them write.
    server.workspaces = server.workspaces.filter((w) => w.id !== PROJECT);
    server.items = { "node-a": { id: "node-a", name: "a.alkerachat", kind: "folder", capabilities: { can_write: false } } };
    server.chats = server.chats.map((c) => (c.id === C_PROJECT_A ? { ...c, can_send: false, can_delete: false } : c));
    act(() => publishFrame(shareFrame(3)));

    await waitFor(() => expect(surface.getAttribute("data-can-rename")).toBe("no"));
    expect(surface.getAttribute("data-reason")).toBe("You no longer have edit access to this chat's workspace.");
  });
});

describe("somebody else's change", () => {
  it("reaches the rail through the event stream, with no reload", async () => {
    const client = renderAt("/chat/new");
    fireEvent.click(await within(rail()).findByRole("button", { name: "Show chats in Q4 forecast" }));
    const list = within(rail()).getByRole("list", { name: "Chats in Q4 forecast" });
    expect(within(list).queryByRole("button", { name: /^Board notes/ })).toBeNull();

    // A teammate starts a chat in the workspace and renames it: the server
    // announces an object change, which is all this session hears.
    server.chats.push(chat("aaaaaaaa-0000-4000-8000-0000000000aa", "Board notes", PROJECT, { owner_user_id: THEM }));
    server.workspaces[1] = { ...server.workspaces[1]!, title: "Q4 forecast v2" };
    const scheduler = createInvalidationScheduler(client, { debounceMs: 0 });
    act(() => {
      scheduler.push({
        type: "workspace_object.changed",
        entity: "workspace_object",
        entity_id: PROJECT,
        version: 4,
        org_id: "org",
      });
      scheduler.flush();
    });
    expect(await within(rail()).findByRole("button", { name: /^Board notes/ })).toBeInTheDocument();
    expect(await within(rail()).findByRole("button", { name: /^Q4 forecast v2/ })).toBeInTheDocument();
    scheduler.dispose();
  });
});
