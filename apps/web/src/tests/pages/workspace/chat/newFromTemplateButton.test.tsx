// "New from template", beside "New chat".
//
// A template is a folder, so the key leads to the folder templates are filed in
// rather than to a picker of its own: everything a reader wants to do with one
// — read its brief, start a chat from it, rename it, share it — is already in
// the file browser.
//
// The id of that folder is asked for, never derived from a name: a member can
// rename or move their own, and a stranger's folder of the same name could be
// found first. And it is asked for with `ensure`, because a reader who has
// never saved a template has no such folder yet — reading the `null` and
// landing them on a listing that is not there is the failure this replaces.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { TopbarSlotsContext } from "@/app/Topbar";

vi.mock("@/pages/workspace/chat/ChatSurface", () => ({
  ChatSurface: () => <div data-testid="chat-surface" />,
}));

import { ChatPage, NEW_FROM_TEMPLATE } from "@/pages/workspace/chat/ChatPage";

const DRIVE = "drv_1";
const TEMPLATES = "nd_templates";

let asked: { method: string; path: string; query: string }[] = [];
/** Whether the drive read answers at all — a page whose drive has not landed
 *  has no id to ensure a folder in. */
let driveReady = true;
/** How many places reads have been answered, so the test can tell the ensure
 *  apart from a plain read. */
let placesAnswers = 0;

function stubApi(): void {
  asked = [];
  driveReady = true;
  placesAnswers = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const asRequest = input instanceof Request ? input : null;
      const raw = asRequest ? asRequest.url : String(input);
      const method = (init?.method ?? asRequest?.method ?? "GET").toUpperCase();
      const url = new URL(raw, "http://x");
      asked.push({ method, path: url.pathname, query: url.search });
      const json = (payload: unknown, code = 200): Response =>
        new Response(payload === null ? null : JSON.stringify(payload), {
          status: code,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/v1/auth/me") return json({ id: "u1", email: "dana@example.com" });
      if (url.pathname === "/api/v1/machines/current") {
        return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
      }
      if (url.pathname === "/api/v1/files/drives") {
        if (!driveReady) return json({ detail: "no drive" }, 503);
        return json({ id: DRIVE, orgId: "org_1", rootId: "nd_root", homeId: "nd_home", quotaBytes: 1 });
      }
      if (url.pathname === `/api/v1/files/drives/${DRIVE}/places`) {
        placesAnswers += 1;
        // A read with no `ensure` answers what exists; the ensure answers the
        // folder it made. The fixture says so by answering `null` to the read.
        const ensured = url.searchParams.get("ensure");
        return json({
          homeId: "nd_home",
          chatsId: "nd_chats",
          chatTemplatesId: ensured?.includes("chatTemplates") ? TEMPLATES : null,
        });
      }
      if (url.pathname.startsWith("/api/v1/chats")) {
        return json({ items: [], next_cursor: null });
      }
      return json({});
    }),
  );
}

function Where(): ReactElement {
  const location = useLocation();
  return <span data-testid="at">{location.pathname}</span>;
}

/** The page's topbar keys portal into the shell's masthead slot; the test
 *  stands the slot up so they render somewhere queryable. */
function renderPage(): void {
  const actions = document.createElement("div");
  document.body.appendChild(actions);
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat/new"]}>
        <TopbarSlotsContext.Provider
          value={{
            subtitle: null,
            actions,
            framed: true,
            setTitleHidden: () => {},
            setTopbarHidden: () => {},
          }}
        >
          <Where />
          <Routes>
            <Route path="/chat/:chatId" element={<ChatPage />} />
            <Route path="/files/:nodeId" element={<p>Files</p>} />
          </Routes>
        </TopbarSlotsContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const key = (): Promise<HTMLElement> => screen.findByRole("button", { name: NEW_FROM_TEMPLATE });
/** The key once the drive read has landed — until then there is no drive id to
 *  ensure a folder in, and the key says so by being unpressable. */
const readyKey = async (): Promise<HTMLElement> => {
  const button = await key();
  await waitFor(() => expect(button).toBeEnabled());
  return button;
};

beforeEach(stubApi);
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("the New from template key", () => {
  it("sits beside New chat", async () => {
    renderPage();
    await key();
    expect(screen.getByRole("button", { name: "New chat" })).toBeInTheDocument();
  });

  it("asks for the templates folder to be there, and opens it", async () => {
    renderPage();
    fireEvent.click(await readyKey());

    await waitFor(() => expect(screen.getByTestId("at").textContent).toBe(`/files/${TEMPLATES}`));
    const call = asked.find((c) => c.path === `/api/v1/files/drives/${DRIVE}/places`);
    expect(call?.method).toBe("GET");
    // The folder is made if it is not there: a reader who has never saved a
    // template is not sent to a listing that does not exist.
    expect(call?.query).toContain("ensure=chatTemplates");
  });

  it("asks once however fast the key is pressed twice", async () => {
    renderPage();
    const button = await readyKey();
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => expect(screen.getByTestId("at").textContent).toBe(`/files/${TEMPLATES}`));
    expect(placesAnswers).toBe(1);
  });

  it("cannot be pressed while there is no drive to ask about", async () => {
    driveReady = false;
    renderPage();
    const button = await key();
    expect(button).toBeDisabled();
    fireEvent.click(button);

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(asked.some((c) => c.path.endsWith("/places"))).toBe(false);
    expect(screen.getByTestId("at").textContent).toBe("/chat/new");
  });
});
