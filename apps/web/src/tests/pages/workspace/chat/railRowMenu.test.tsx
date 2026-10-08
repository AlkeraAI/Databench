// Everything a chat carries in the rail, and the two ways a reader opens it.
//
// The rail is where chats are managed, not just picked: opening one, renaming
// it, making another out of it, saving it as a template, letting people in and
// throwing it away all happen from the row, without opening the chat first. The
// rows are one menu opened three ways — a press on the row's own key, a
// right-click anywhere on the row, and the keyboard's own menu request on the
// focused row — because a menu reachable only by right-click is a menu half the
// readers never find, and one reachable only by a key is one nobody right-
// clicks to.
//
// A chat with no node in the drive can be neither copied, shared nor saved, so
// those rows are absent rather than present and refusing.
//
// This file drives the REAL `ChatPage` with a scripted API; only the chat
// surface itself is stood in for, since the composition inside it has its own
// suite.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: (props: { chatId?: string }) => (
    <div data-testid="chat-surface" data-chat-id={props.chatId ?? ""} />
  ),
}));

import { ChatPage } from "@/pages/workspace/chat/ChatPage";
import { DELETE_CHAT_KEY } from "@/pages/workspace/chat/useDeleteChatAction";

const DRIVE = "drv_1";
const ME = "u1";

const MINE = {
  id: "c1",
  title: "Yesterday's orders",
  owner_user_id: ME,
  machine_id: "m1",
  machine_status: "ready" as const,
  created_at: "2026-09-06T12:00:00Z",
  updated_at: "2026-09-06T12:00:00Z",
  last_seq: 3,
  files_node_id: "nd_c1" as string | null,
  can_delete: true,
};
/** Shared with this reader, who may read it but not delete it. */
const THEIRS = {
  ...MINE,
  id: "c2",
  title: "Shared with me",
  owner_user_id: "u_other",
  files_node_id: "nd_c2" as string | null,
  can_delete: false,
};

/** The rows a chat with a node in the drive offers, in order. */
const ALL_ROWS = ["Open", "Rename", "Mark as unread", "Duplicate", "Save as template…", "Share…", "Delete"];

let asked: { method: string; path: string; body: unknown; query: string }[] = [];
/** What `GET /api/v1/objects/c1` answers, so a rename can name the version. */
let objectTitle = MINE.title;
/** Whether the listed chats have a node in the drive at all. */
let nodes = true;
/** Whether the shared chat's listed row leaves `can_delete` unsaid. */
let theirsUnsaid = false;

function stubApi(): void {
  asked = [];
  objectTitle = MINE.title;
  nodes = true;
  theirsUnsaid = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const asRequest = input instanceof Request ? input : null;
      const raw = asRequest ? asRequest.url : String(input);
      const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();
      const url = new URL(raw, "http://x");
      const at = url.pathname;
      const text = asRequest ? await asRequest.clone().text() : String(init?.body ?? "");
      const body: unknown = text === "" ? null : JSON.parse(text);
      asked.push({ method, path: at, body, query: url.search });
      const json = (payload: unknown, code = 200): Response =>
        new Response(payload === null ? null : JSON.stringify(payload), {
          status: code,
          headers: { "content-type": "application/json" },
        });

      if (at === "/api/v1/auth/me") return json({ id: ME, email: "dana@example.com" });
      if (at === "/api/v1/machines/current") {
        return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
      }
      if (at === "/api/v1/objects/c1") {
        if (method === "PUT") {
          objectTitle = (body as { title: string }).title;
          return json({ id: "c1", type: "chat", title: objectTitle, version: 5, spec: {} });
        }
        return json({ id: "c1", type: "chat", title: objectTitle, version: 4, spec: {} });
      }
      if (at === "/api/v1/chat-templates") {
        return json({ id: "tpl_1", title: "Yesterday's orders", version: 1 }, 201);
      }
      if (at === `/api/v1/files/drives/${DRIVE}/items/nd_c2/duplicate`) {
        return json(
          {
            item: {
              id: "nd_copy",
              driveId: DRIVE,
              kind: "folder",
              name: "Shared with me.alkerachat",
              parentId: "nd_chats",
              etag: "1",
              ctag: "1",
            },
            chatId: "c9",
            objectId: "c9",
          },
          201,
        );
      }
      if (at === "/api/v1/files/drives") {
        return json({ id: DRIVE, orgId: "org_1", rootId: "nd_root", homeId: "nd_home", quotaBytes: 1 });
      }
      if (at.endsWith("/permissions")) return json({ value: [] });
      if (at.endsWith("/org/members")) return json([]);
      if (at.endsWith("/teams")) return json([]);
      if (at.startsWith("/api/v1/files/drives")) {
        const node = at.split("/items/")[1]?.split("/")[0] ?? "nd_c1";
        return json({
          id: node,
          driveId: DRIVE,
          kind: "folder",
          name: `${node}.alkerachat`,
          nameDisplay: node === "nd_c2" ? THEIRS.title : MINE.title,
          parentId: "nd_root",
          etag: "1",
          ctag: "1",
          object: { id: node === "nd_c2" ? "c2" : "c1", type: "chat", title: node === "nd_c2" ? THEIRS.title : MINE.title },
          capabilities: { can_read: true, can_write: true, can_share: true, refusals: {} },
        });
      }
      if (/\/api\/v1\/chats\/[^/?]+$/.test(at)) {
        const row = at.endsWith("c2") ? THEIRS : MINE;
        return json({ ...row, files_node_id: nodes ? row.files_node_id : null });
      }
      if (at.startsWith("/api/v1/chats")) {
        if (method === "DELETE") return json(null, 204);
        return json({
          items: [
            { ...MINE, title: objectTitle, files_node_id: nodes ? MINE.files_node_id : null },
            theirsUnsaid
              ? { ...THEIRS, can_delete: undefined, files_node_id: THEIRS.files_node_id }
              : { ...THEIRS, files_node_id: nodes ? THEIRS.files_node_id : null },
          ],
          next_cursor: null,
        });
      }
      return json({});
    }),
  );
}

function Where(): ReactElement {
  const location = useLocation();
  return <span data-testid="at">{location.pathname}</span>;
}

function renderPage(): void {
  const client = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Where />
        <Routes>
          <Route path="/chat/:chatId" element={<ChatPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/files/:nodeId" element={<p>Files</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The row a chat's name is drawn on, by its name. */
const row = async (name: string): Promise<HTMLElement> => {
  const button = await screen.findByRole("button", { name });
  return button.closest(".chat-page__row-wrap") as HTMLElement;
};
/** The row's own key onto the menu. */
const kebab = (of: HTMLElement, name: string): HTMLElement =>
  within(of).getByRole("button", { name: `Actions for ${name}` });
const menu = (): HTMLElement => screen.getByRole("menu");
const labels = (): string[] =>
  within(menu())
    .getAllByRole("menuitem")
    .map((item) => item.textContent ?? "");
const pick = (label: string): void => {
  fireEvent.click(within(menu()).getByRole("menuitem", { name: label }));
};

beforeEach(stubApi);
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("how a chat's actions are reached from its row", () => {
  it("the row carries the name, its light and one key onto the menu", async () => {
    renderPage();
    const mine = await row(MINE.title);
    const keys = within(mine).getAllByRole("button");
    expect(keys.map((key) => key.getAttribute("aria-label") ?? key.textContent)).toEqual([
      MINE.title,
      `Actions for ${MINE.title}`,
    ]);
  });

  it("the name spells itself out in full on the element that cuts it short", async () => {
    // The rail is narrower than most chat names, so the row shows as much as
    // fits and breaks off. Resting on it is how the rest is read, and the tip
    // hangs on the element that did the cutting rather than on the row around
    // it. It never abbreviates: the tip is the whole stored name.
    renderPage();
    for (const chat of [MINE, THEIRS]) {
      const name = (await row(chat.title)).querySelector(".chat-page__row-title");
      expect(name?.getAttribute("title")).toBe(chat.title);
    }
    // The row is still named by its text, not by a tip repeated into its
    // accessible name.
    const mine = await row(MINE.title);
    expect(within(mine).getByRole("button", { name: MINE.title }).getAttribute("title")).toBeNull();
  });

  it("the key is in the row before any pointer arrives, and the row is the one tab stop", async () => {
    // The key only SHOWS under the pointer or under focus, so it is a real
    // control the whole time. It is not a second Tab stop on every row: the
    // keyboard reaches the same menu from the row (Shift+F10 or the menu key),
    // so walking the rail costs one Tab per chat, not two.
    renderPage();
    const mine = await row(MINE.title);
    const key = kebab(mine, MINE.title);
    expect(key.getAttribute("aria-hidden")).toBeNull();
    expect((key as HTMLButtonElement).disabled).toBe(false);
    expect((key as HTMLButtonElement).tabIndex).toBe(-1);
    // And it opens the same menu the pointer would have opened.
    fireEvent.click(key);
    expect(labels()).toEqual(ALL_ROWS);
  });

  it("the row's key opens the menu", async () => {
    renderPage();
    const mine = await row(MINE.title);
    expect(screen.queryByRole("menu")).toBeNull();
    fireEvent.click(kebab(mine, MINE.title));
    expect(labels()).toEqual(ALL_ROWS);
  });

  it("a right-click on the row opens the same rows", async () => {
    renderPage();
    const mine = await row(MINE.title);
    fireEvent.contextMenu(mine);
    expect(labels()).toEqual(ALL_ROWS);
  });

  it("the keyboard's own menu request on the focused row opens them, with no mouse", async () => {
    renderPage();
    const mine = await row(MINE.title);
    fireEvent.keyDown(within(mine).getByRole("button", { name: MINE.title }), {
      key: "F10",
      shiftKey: true,
    });
    expect(labels()).toEqual(ALL_ROWS);
  });

  it("the key does not open the chat underneath it", async () => {
    renderPage();
    const theirs = await row(THEIRS.title);
    fireEvent.click(kebab(theirs, THEIRS.title));
    expect(screen.getByTestId("at").textContent).toBe("/chat/c1");
  });

  it("the menu is the product's shared one, and Delete reads as what it costs", async () => {
    renderPage();
    const mine = await row(MINE.title);
    fireEvent.contextMenu(mine);
    // The Files page's own panel: one primitive, not a second menu that merely
    // looks like it.
    expect(menu().classList.contains("alk-ctxmenu")).toBe(true);
    expect(within(menu()).getByRole("menuitem", { name: "Delete" }).getAttribute("data-tone")).toBe(
      "destructive",
    );
    expect(
      within(menu()).getByRole("menuitem", { name: "Rename" }).getAttribute("data-tone"),
    ).toBeNull();
  });

  it("Escape closes it and hands the row back its focus", async () => {
    renderPage();
    const mine = await row(MINE.title);
    const name = within(mine).getByRole("button", { name: MINE.title });
    name.focus();
    fireEvent.keyDown(name, { key: "F10", shiftKey: true });
    // The menu takes focus on open, so the return is a real hand-back.
    await waitFor(() => expect(menu().contains(document.activeElement)).toBe(true));

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(document.activeElement).toBe(name);
  });
});

describe("what the rows do", () => {
  it("Open leads to that chat", async () => {
    renderPage();
    const theirs = await row(THEIRS.title);
    fireEvent.contextMenu(theirs);
    pick("Open");
    await waitFor(() => expect(screen.getByTestId("at").textContent).toBe("/chat/c2"));
  });

  it("Rename edits the name in place and writes the new title", async () => {
    renderPage();
    const mine = await row(MINE.title);
    fireEvent.contextMenu(mine);
    pick("Rename");

    const field = (await screen.findByLabelText("Chat title")) as HTMLInputElement;
    expect(field.value).toBe(MINE.title);
    fireEvent.change(field, { target: { value: "Last week's orders" } });
    fireEvent.keyDown(field, { key: "Enter" });

    await waitFor(() =>
      expect(asked.filter((c) => c.method === "PUT" && c.path === "/api/v1/objects/c1")).toHaveLength(1),
    );
    // The version is read immediately before the write rather than guessed.
    expect(asked.find((c) => c.method === "PUT")?.body).toEqual({
      title: "Last week's orders",
      expected_version: 4,
    });
    await screen.findByRole("button", { name: "Last week's orders" });
  });

  it("Escape in the rename field writes nothing", async () => {
    renderPage();
    const mine = await row(MINE.title);
    fireEvent.contextMenu(mine);
    pick("Rename");
    const field = (await screen.findByLabelText("Chat title")) as HTMLInputElement;
    fireEvent.change(field, { target: { value: "Nope" } });
    fireEvent.keyDown(field, { key: "Escape" });

    await screen.findByRole("button", { name: MINE.title });
    expect(asked.filter((c) => c.method === "PUT")).toHaveLength(0);
  });

  it("a refused rename reads the server's own words out under the row", async () => {
    renderPage();
    const mine = await row(MINE.title);
    fireEvent.contextMenu(mine);
    pick("Rename");
    const field = await screen.findByLabelText("Chat title");
    fireEvent.change(field, { target: { value: "" } });
    fireEvent.change(field, { target: { value: "Renamed" } });
    // The version read answers, the write does not.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const asRequest = input instanceof Request ? input : null;
        const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();
        if (method === "PUT") {
          return new Response(JSON.stringify({ detail: "You cannot rename this chat." }), {
            status: 403,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(JSON.stringify({ id: "c1", type: "chat", title: MINE.title, version: 4, spec: {} }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    fireEvent.keyDown(field, { key: "Enter" });

    const notice = await screen.findByRole("alert");
    expect(notice.textContent).toBe("You cannot rename this chat.");
  });

  it("Delete asks in the product's dialog, and a cancelled one deletes nothing", async () => {
    // A row asks the same question the header asks, in the same dialog — never
    // the browser's own confirm, which cannot name a destructive action and
    // which a reader can switch off for the whole tab.
    const browserConfirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPage();
    const mine = await row(MINE.title);
    fireEvent.contextMenu(mine);
    pick("Delete");

    // Asked from a row, the question names THAT row's chat — the one about to
    // go is not the one the page is showing.
    expect(await screen.findByRole("dialog", { name: `Delete ${MINE.title}?` })).toBeTruthy();
    expect(browserConfirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(asked.filter((c) => c.method === "DELETE")).toHaveLength(0);
    browserConfirm.mockRestore();
  });

  it("a chat the reader may not delete offers no Delete, and one they may still does", async () => {
    renderPage();
    const theirs = await row(THEIRS.title);
    fireEvent.click(kebab(theirs, THEIRS.title));
    // Only Delete goes: the rest of what a reader may do to a shared chat stays.
    expect(labels()).toEqual(["Open", "Rename", "Mark as unread", "Copy to my drive", "Save as template…", "Share…"]);
    fireEvent.keyDown(menu(), { key: "Escape" });

    const mine = await row(MINE.title);
    fireEvent.contextMenu(mine);
    expect(labels()).toEqual(ALL_ROWS);
  });

  it("a chat whose row says nothing about deleting offers no Delete", async () => {
    // Fail closed: a server that left the flag unsaid has not said yes.
    theirsUnsaid = true;
    renderPage();
    const theirs = await row(THEIRS.title);
    fireEvent.click(kebab(theirs, THEIRS.title));
    expect(labels()).not.toContain("Delete");
  });

  it("a confirmed Delete removes that chat", async () => {
    renderPage();
    const mine = await row(MINE.title);
    fireEvent.contextMenu(mine);
    pick("Delete");

    fireEvent.click(await screen.findByRole("button", { name: DELETE_CHAT_KEY }));

    await waitFor(() =>
      expect(asked.filter((c) => c.method === "DELETE" && c.path === "/api/v1/chats/c1")).toHaveLength(1),
    );
  });

  it("Copy to my drive names the copy first, then opens it", async () => {
    renderPage();
    const theirs = await row(THEIRS.title);
    fireEvent.contextMenu(theirs);
    pick("Copy to my drive");

    const dialog = await screen.findByRole("dialog");
    expect((within(dialog).getByLabelText("Name") as HTMLInputElement).value).toBe(THEIRS.title);
    fireEvent.click(within(dialog).getByRole("button", { name: "Duplicate" }));

    await waitFor(() =>
      expect(asked.filter((c) => c.method === "POST" && c.path.endsWith("/duplicate"))).toHaveLength(1),
    );
    expect(asked.find((c) => c.path.endsWith("/duplicate"))?.body).toEqual({
      destinationId: null,
      name: THEIRS.title,
    });
    await waitFor(() => expect(screen.getByTestId("at").textContent).toBe("/chat/c9"));
  });

  it("Save as template… asks for the name and the brief, then saves that chat", async () => {
    renderPage();
    const mine = await row(MINE.title);
    fireEvent.contextMenu(mine);
    pick("Save as template…");

    const dialog = await screen.findByRole("dialog");
    // Named after the chat it is made from, and the chat is named by its
    // object's title rather than the `<uuid>.alkerachat` folder on disk.
    await waitFor(() =>
      expect((within(dialog).getByLabelText("Name") as HTMLInputElement).value).toBe(MINE.title),
    );
    fireEvent.change(within(dialog).getByLabelText("Brief"), {
      target: { value: "Run this for the new quarter." },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save template" }));

    await waitFor(() =>
      expect(asked.filter((c) => c.method === "POST" && c.path === "/api/v1/chat-templates")).toHaveLength(1),
    );
    expect(asked.find((c) => c.path === "/api/v1/chat-templates")?.body).toEqual({
      source_chat_id: "c1",
      brief: "Run this for the new quarter.",
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("Share… opens the sharing dialog over that row's chat, without leaving the page", async () => {
    renderPage();
    const theirs = await row(THEIRS.title);
    fireEvent.contextMenu(theirs);
    pick("Share…");

    const dialog = await screen.findByRole("dialog");
    // The chat's name, never the `<uuid>.alkerachat` the node is stored under.
    expect(within(dialog).getByText(`Share “${THEIRS.title}”`)).toBeInTheDocument();
    await waitFor(() =>
      expect(asked.some((c) => c.path === `/api/v1/files/drives/${DRIVE}/items/nd_c2/permissions`)).toBe(true),
    );
    expect(screen.getByTestId("at").textContent).toBe("/chat/c1");
  });
});

describe("a chat with no node in the drive", () => {
  it("offers only what can be done without one", async () => {
    nodes = false;
    renderPage();
    const mine = await row(MINE.title);
    fireEvent.contextMenu(mine);
    expect(labels()).toEqual(["Open", "Rename", "Mark as unread", "Delete"]);
  });
});
