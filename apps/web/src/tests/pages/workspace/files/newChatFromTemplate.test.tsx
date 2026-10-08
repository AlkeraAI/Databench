/**
 * Starting a chat from a template, from the file browser.
 *
 * A template is not a chat: opening one must never drop the reader into a
 * conversation, and starting one must hand the chat surface the TEMPLATE's node
 * so the new chat begins with the brief and the files the template holds. Files
 * names the source and routes; it does not create the chat itself, which is why
 * the proof here is where the router lands and what it carries.
 *
 * Driven through the real page, by the two gestures that reach it: the row's
 * menu, and the chooser a double-click opens.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "file",
    name: "notes.md",
    nameDisplay: "notes.md",
    nameEncoding: "utf-8",
    pathBytes: "/home/notes.md",
    path: "/home/notes.md",
    etag: "et_1",
    ctag: "ct_1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: {
      can_read: true,
      can_write: true,
      can_share: true,
      can_delete: true,
      can_rename: true,
      can_download: true,
    },
    ...over,
  } as Item;
}

const HOME = item({ id: "nd_home", kind: "folder", name: "home", nameDisplay: "home" });

/** The template, as the listing carries it: a folder whose facet says what it is,
 *  named after a filesystem name nobody typed. */
const TEMPLATE = item({
  id: "nd_tpl",
  kind: "folder",
  parentId: "nd_home",
  name: "6c2a1b90-0000-4000-8000-000000000002.alkerachat.template",
  nameDisplay: "6c2a1b90-0000-4000-8000-000000000002.alkerachat.template",
  object: {
    type: "chat_template",
    id: "tpl_4",
    title: "Warehouse audit",
    web_url: "/templates/tpl_4",
    metadata: { files_node_id: "nd_tpl_files" },
  },
} as unknown as Partial<Item>);

/** A chat, so the negative half has something to be asserted against. */
const CHAT = item({
  id: "nd_chat",
  kind: "folder",
  parentId: "nd_home",
  name: "Q3 review.alkerachat",
  nameDisplay: "Q3 review.alkerachat",
  object: {
    type: "chat",
    id: "cht_9",
    title: "Q3 review",
    web_url: "/chat/cht_9",
    metadata: { files_node_id: "nd_chat_files" },
  },
} as unknown as Partial<Item>);

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      void init;
      const url = input instanceof Request ? input.url : String(input);
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/search")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) {
        return answer({ value: [HOME], nextMarker: null });
      }
      if (url.includes("/children")) return answer({ value: [TEMPLATE, CHAT], nextMarker: null });
      if (url.includes("/permissions")) return answer({ value: [] });
      if (url.includes("/items/")) return answer(HOME);
      return answer({});
    }),
  );
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        media: query,
        matches: false,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
}

function Where() {
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

function mount(at = "/files/nd_home") {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[at]}>
        <Where />
        <Routes>
          <Route path="/files" element={<FilesScreen />} />
          <Route path="/files/:nodeId" element={<FilesScreen />} />
          <Route path="/chat" element={<p>a new chat</p>} />
          <Route path="/chat/:chatId" element={<p>the chat</p>} />
          <Route path="/templates/:templateId" element={<p>the template</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** The row's own menu, reached the way a person reaches it. */
async function menuOn(title: string): Promise<HTMLElement> {
  const row = await waitFor(() =>
    within(screen.getByRole("treegrid")).getByRole("row", { name: new RegExp(title) }),
  );
  fireEvent.contextMenu(within(row).getAllByRole("gridcell")[0] as HTMLElement);
  return screen.findByRole("menu");
}

beforeEach(stubApi);
afterEach(() => vi.unstubAllGlobals());

describe("New chat from template", () => {
  it("routes to a new chat carrying the template's node as the source", async () => {
    mount();
    const menu = await menuOn("Warehouse audit");

    fireEvent.click(within(menu).getByRole("menuitem", { name: "New chat from template" }));

    // The TEMPLATE's node — not its page id, and not the working folder its files
    // live in: the chat surface reads the node to copy the brief and the files.
    await waitFor(() =>
      expect(screen.getByTestId("where")).toHaveTextContent("/chat?source=nd_tpl"),
    );
    expect(screen.getByText("a new chat")).toBeInTheDocument();
  });

  it("is not in the menu on a chat, which is not a template", async () => {
    mount();
    const menu = await menuOn("Q3 review");

    // The negative half: a conversation does not start from a conversation, and
    // a row saying so on every chat is noise rather than teaching.
    expect(within(menu).queryByRole("menuitem", { name: /New chat from template/ })).toBeNull();
    expect(screen.getByTestId("where")).toHaveTextContent("/files/nd_home");
  });

  it("opens the template from Open template, asking nothing first", async () => {
    mount();
    const menu = await menuOn("Warehouse audit");

    fireEvent.click(within(menu).getByRole("menuitem", { name: /^Open template/ }));

    // A person who picked this row has already said which of the two they meant,
    // so the chooser must not put the question back in front of them — and what
    // they picked is the template itself: the brief, and the button that starts
    // a chat from it.
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/templates/tpl_4"));
    expect(screen.getByText("the template")).toBeInTheDocument();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("a double-click opens the template, as Open template does", async () => {
    const user = userEvent.setup();
    mount();

    await user.dblClick(await screen.findByText("Warehouse audit"));

    // The template's page carries the brief and the button that starts a chat
    // from it; it never opens as a chat, which it is not.
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/templates/tpl_4"));
    expect(screen.queryByRole("menu")).toBeNull();
  });
});
