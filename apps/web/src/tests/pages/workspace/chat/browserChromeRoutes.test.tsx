// Where the chat chrome's keys land in a BROWSER tab.
//
// The chrome is shared verbatim by both shells, but the extension's route table
// (`webview/vscodeApp.tsx`: `/sidecar`, `/editor/*`) is not the browser SPA's
// (`App.tsx`), which ends with `<Route path="*" element={<NotFoundPage />} />`.
//
// So this file mounts the real `ChatSurface` over the browser's OWN route set
// and pins which keys a tab shows, and that none of them lands on the
// catch-all.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PERMISSION_MODE_VALUES } from "@alkera/chat-model";

import { createQueryClient } from "@/api/queryClient";

const { ds, runCommand } = vi.hoisted(() => ({
  // The one host verb this file watches: the editor command dispatch, which a
  // page has nothing behind.
  runCommand: vi.fn(async () => undefined),
  ds: {
    listChats: async () => [{ id: "c1", title: "Chat", updatedAt: "2026-09-06T12:00:00Z" }],
    listModels: async () => [],
    // The new-chat seed the server resolves from this reader's saved default —
    // the only authority on the stance a chat that does not exist yet will run
    // in, and what the home composer's chip states.
    resolveChatDefaults: async () => ({ model: null, effort: null, permissionMode: "read_only" }),
    listCommands: async () => [],
    runCommand: async () => ({ kind: "cli_only", command: null, payload: {}, message: "" }),
    listContext: async () => ({ total: 0 }),
    lineageRoots: async () => ({}),
    // One turn that names a file, so the card that renders a path is on screen.
    getChatTurns: async () => [
      {
        id: "a1",
        author: "assistant",
        status: "completed",
        parts: [
          {
            kind: "tool",
            id: "t1",
            callId: "call-1",
            name: "read",
            state: "completed",
            input: { filePath: "models/marts/orders.sql" },
            output: { content: "select 1" },
          },
        ],
      },
    ],
    searchFiles: async () => [],
    sendUserMessage: async () => ({ id: "m1", role: "user", content: "x" }),
    createChat: async () => ({ id: "c1", title: null, updatedAt: "" }),
    // What the cloud source answers: the mirror opens every session read-only.
    getPermissionMode: async () => "read_only",
    setPermissionMode: vi.fn(async (_chatId: string, _mode: string) => {}),
    subscribePermissionMode: () => () => {},
    subscribeChat: () => () => {},
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
  },
}));

// The browser's host is the REAL one the /chat route installs, with only its
// command dispatch spied on: a hand-written literal is how a fixture comes to
// carry verbs the shell does not have (an `openFile` that resolves, say), and
// then agrees with a page that offers a control nobody can use.
vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  const { PERMISSION_MODE_VALUES: cloudModes } = await import("@alkera/chat-model");
  const host = {
    ...real.createBrowserChatHost({
      account: () => ({ email: "analyst@tideline.example", webAppUrl: null }),
    }),
    runCommand,
  };
  return {
    ...real,
    chatHost: () => host,
    chatData: () => ds,
    refetchWhileErrored: () => false as const,
    refetchWhileNoChatDefault: () => false as const,
    refetchWhileErroredOrEmpty: () => false as const,
    // The browser source's real capabilities: no harness to ask, a catalogue
    // it does serve, the stance a new chat starts in, and the stances a reader
    // may move it to — the last taken from the source itself rather than
    // copied, so a list the product widens cannot leave this fixture behind.
    chatCaps: () => ({
      opencodeActive: false,
      modelCatalog: true,
      permissionModes: cloudModes,
      fixedPermissionMode: "read_only",
    }),
  };
});

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";


afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

/** The portal's API, answering every call with an empty 200 — enough for the
 *  logout mutation to settle, and it records what was asked of it.
 *
 *  Two reads answer with something: the chat's row and the caller's drive. The
 *  header's Share key stands on both — a chat with no node in no drive has
 *  nothing to share and carries no key — so an empty 200 there would take the
 *  key off the header for a reason that has nothing to do with what this file
 *  asks about. */
function scriptApi(): ReturnType<typeof vi.fn> {
  const json = (payload: unknown): Response =>
    new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const raw = input instanceof Request ? input.url : String(input);
    const { pathname } = new URL(raw, "http://localhost");
    if (pathname === "/api/v1/chats/c1") {
      return json({ id: "c1", title: "A chat", files_node_id: "nd_chat", can_delete: true });
    }
    if (pathname === "/api/v1/files/drives") return json({ id: "drv_1", rootId: "nd_root" });
    return json({});
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Reports the router's current location, so a key's landing place is read off
 *  the URL rather than guessed from what rendered. */
function Where() {
  const location = useLocation();
  return <span data-testid="at">{location.pathname}</span>;
}

/** The chat over the routes the browser SPA actually declares for it. */
function renderInBrowserRoutes() {
  const qc = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Where />
        <Routes>
          <Route path="/chat/:chatId" element={<ChatSurface chatId="c1" />} />
          <Route path="/chat" element={<ChatSurface />} />
          {/* The stacked surfaces the chat's own keys reach. */}
          <Route path="/chat/:chatId/results" element={<p>Results</p>} />
          <Route path="/chat/:chatId/result/:handle" element={<p>A result</p>} />
          <Route path="/chat/:chatId/plan/:partId" element={<p>A plan</p>} />
          <Route path="/chat/:chatId/compaction/:partId" element={<p>A compaction</p>} />
          <Route path="/graph" element={<p>A graph</p>} />
          <Route path="/objects/:objectId" element={<p>An object</p>} />
          {/* The portal pages the chrome's own keys reach. */}
          <Route path="/knowledge" element={<p>Knowledge base</p>} />
          <Route path="/files" element={<p>Files</p>} />
          <Route path="/login" element={<p>Sign in</p>} />
          {/* App.tsx's own last line. */}
          <Route path="*" element={<p>Page not found</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const notFound = (): HTMLElement | null => screen.queryByText("Page not found");

/** The chrome has rendered: the chat's identity row names this chat. Every key
 *  the tests below press or miss sits on that row. */
const chrome = (): Promise<HTMLElement> =>
  screen.findByRole("navigation", { name: "Chat trail" });

describe("the chat chrome's keys, pressed in a browser tab", () => {
  // A chat page sits inside the portal's own app shell, which already carries
  // the nav that starts a chat, so a New chat key in the chat's own header is a
  // second door to one place.
  it("carries no New chat key: the app shell around the page already starts one", async () => {
    renderInBrowserRoutes();
    await chrome();
    expect(screen.queryByRole("button", { name: /new chat/i })).toBeNull();
  });

  // And no Back arrow beside the title. The rail of the org's chats sits on the
  // page around this one, and a chat drilled in from another names every level
  // above it in its own trail, so an arrow that climbs one rung is a third way
  // to do what two other controls already do.
  it("carries no Back key: the page around the chat is the way out of it", async () => {
    renderInBrowserRoutes();
    await chrome();
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
    // And nothing took its place: the chat the reader opened is still the one
    // on screen.
    expect(screen.getByTestId("at")).toHaveTextContent("/chat/c1");
    expect(notFound()).toBeNull();
  });

  it("offers no Artifacts key while it is hidden (the results page stays on /objects)", async () => {
    renderInBrowserRoutes();
    await chrome();
    expect(screen.queryByRole("button", { name: /artifacts/i })).toBeNull();
  });

  it("carries no Knowledge key beside the chat's title", async () => {
    // The portal's knowledge page is reached from the nav; next to a chat's
    // title it was a pill with a count, and the count cost a catalogue read on
    // every chat opened. The page itself is untouched — this is the header.
    scriptApi();
    renderInBrowserRoutes();
    await chrome();
    expect(screen.queryByRole("button", { name: /knowledge/i })).toBeNull();
    // And with no key there is nothing to fold away into the overflow menu.
    expect(screen.queryByRole("menuitem", { name: /^Knowledge/ })).toBeNull();
  });

  it("offers no Lineage key at all, because the portal has no lineage surface", async () => {
    // `App.tsx` routes no `/lineage` — there is no cloud lineage data source —
    // so the honest chrome carries no key rather than one reporting a count and
    // then doing nothing.
    renderInBrowserRoutes();
    await chrome();
    expect(screen.queryByRole("button", { name: /^lineage/i })).toBeNull();
  });

  // The gear's menu listed the signed-in address and one Sign out row. Both are
  // the portal's own account menu, on every page of the app shell this chat is
  // rendered inside — so the chat header named the same account a second time
  // and offered a second door to the same place.
  it("carries no account-and-settings gear: the app shell owns the account menu", async () => {
    scriptApi();
    renderInBrowserRoutes();
    await chrome();
    expect(screen.queryByRole("button", { name: /settings/i })).toBeNull();
    expect(screen.queryByRole("menuitem")).toBeNull();
  });

  // The two keys that act on THIS chat stay: nothing else in the portal deletes
  // the open chat or shares its drive node, so dropping them would take the
  // action away rather than de-duplicate it.
  it("still offers the keys that act on this chat: Share and Delete chat", async () => {
    scriptApi();
    renderInBrowserRoutes();
    expect(await screen.findByRole("button", { name: /delete chat/i })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /^share$/i })).toBeInTheDocument();
  });
});

describe("a file the agent read, shown in a browser tab", () => {
  // The card renders the path as a button wherever the shell can open it. A
  // page cannot, and the browser host says so by having no `openFile` at all —
  // so the same card renders the path as the label it is, not a button that
  // hands the click to a no-op.
  it("names the path as text, not as a control that goes nowhere", async () => {
    scriptApi();
    renderInBrowserRoutes();
    // The activity group's step opens on its own head; the file band is inside.
    const head = (await screen.findAllByText(/orders\.sql/))[0].closest("button");
    expect(head).not.toBeNull();
    fireEvent.click(head as HTMLElement);

    // The card's file band is on screen…
    const band = await waitFor(() => {
      const found = document.querySelector(".chat-tool-band--path");
      expect(found).not.toBeNull();
      return found as Element;
    });
    // …as a label, not the button it becomes where a shell can open a file.
    expect(band.tagName).toBe("DIV");
    expect(screen.queryByRole("button", { name: /^Open .*orders\.sql$/ })).toBeNull();
  });
});

describe("what a browser tab asks before there is a transcript", () => {
  // The editor's coding prompts talk about "the file I have open", which a
  // reader in a browser does not have.
  it("asks about the reader's data, and names no file they do not have", async () => {
    render(
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <MemoryRouter initialEntries={["/chat"]}>
          <Routes>
            <Route path="/chat" element={<ChatSurface />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Ask a question about your data")).toBeInTheDocument();
    expect(screen.queryByText(/What should Alkera build\?/i)).toBeNull();
    expect(document.body.textContent).not.toMatch(/file I have open/i);
    expect(document.body.textContent).not.toMatch(/codebase/i);
  });
});

describe("the permission mode a browser tab shows", () => {
  // A cloud chat opens read-only, so that is what the pill states before anyone
  // touches it — a chip that flashed "Default" over a session which auto-rejects
  // every mutation is a safety control contradicting the agent it describes.
  it("opens on the read-only stance the session starts in", async () => {
    renderInBrowserRoutes();
    const pill = await screen.findByRole("button", { name: /permission mode/i });
    expect(pill.getAttribute("aria-label")).toBe("Permission mode: Read-only");
    expect((pill as HTMLButtonElement).disabled).toBe(false);
  });

  // The reader may lift their own chat out of read-only, and the switch has to
  // reach the source — a pill that changed only its own label would leave the
  // machine in the stance the reader thinks they left.
  it("switches the session's stance through the source", async () => {
    renderInBrowserRoutes();
    const pill = await screen.findByRole("button", { name: /permission mode/i });
    fireEvent.click(pill);
    const option = await screen.findByRole("option", { name: /^Default/ });
    fireEvent.click(option);
    await waitFor(() => expect(ds.setPermissionMode).toHaveBeenCalledWith("c1", "default"));
  });

  // The menu is the editor's, option for option and in the editor's order: the
  // reader who learned these stances in the extension must not find the browser
  // missing the two an unattended run needs, nor the judged middle between them.
  it("offers the editor's five stances, in the editor's order", async () => {
    renderInBrowserRoutes();
    const pill = await screen.findByRole("button", { name: /permission mode/i });
    fireEvent.click(pill);
    await screen.findByRole("option", { name: /^Default/ });
    const expected = ["Default", "Plan", "Auto", "Read-only", "Bypass permissions"];
    const labels = screen
      .getAllByRole("option")
      .map((option) => expected.find((name) => (option.textContent ?? "").startsWith(name)));
    expect(labels).toEqual(expected);
  });

  // Picking it must reach the source with the wire's own word: a chip that
  // moved and a session that did not is how a reader ends up believing the
  // prompts are off over a machine still asking.
  it("switches the session into bypass through the source", async () => {
    renderInBrowserRoutes();
    const pill = await screen.findByRole("button", { name: /permission mode/i });
    fireEvent.click(pill);
    const option = await screen.findByRole("option", { name: /^Bypass permissions/ });
    fireEvent.click(option);
    await waitFor(() => expect(ds.setPermissionMode).toHaveBeenCalledWith("c1", "bypass"));
  });

  // The FIRST control a reader meets is the one on the chat home, before any
  // chat exists to ask about, and it must agree with the chat one page later.
  // What settles it is the server's own answer for the next chat, never this
  // surface's floor, which is only a guess.
  it("names the stance the server resolved for the next chat, where there is none to ask about", async () => {
    render(
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <MemoryRouter initialEntries={["/chat"]}>
          <Routes>
            <Route path="/chat" element={<ChatSurface />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const pill = await screen.findByRole("button", { name: /permission mode/i });
    expect(pill.getAttribute("aria-label")).toBe("Permission mode: Read-only");
  });

  // The keyboard is the other door onto the same control, and it must not walk
  // past whatever the menu offers — a cycle that reached a stance the menu
  // withholds would land the session somewhere the reader could not get back
  // from by pointing at it.
  it("Shift+Tab cycles only the stances this source offers", async () => {
    renderInBrowserRoutes();
    const field = await screen.findByRole("textbox");
    for (let i = 0; i < 6; i += 1) fireEvent.keyDown(field, { key: "Tab", shiftKey: true });
    const modes = ds.setPermissionMode.mock.calls.map((call) => call[1]);
    expect(modes.length).toBeGreaterThan(0);
    expect(
      modes.every((mode) => (PERMISSION_MODE_VALUES as readonly string[]).includes(mode)),
    ).toBe(true);
    expect(modes).toContain("bypass");
  });
});

describe("sharing a chat from its header", () => {
  // Where the key LEADS is the only question this file asks, and the answer is
  // now nowhere: sharing happens over the chat's own node, in place. What the
  // dialog then does is pinned in `chatHeaderShare.test.tsx`.
  it("the Share key never navigates away from the chat", async () => {
    scriptApi();
    renderInBrowserRoutes();
    const key = await screen.findByRole("button", { name: "Share" });
    fireEvent.click(key);
    // A beat for any navigation the key might have performed to land.
    await waitFor(() => expect(screen.getByTestId("at")).toHaveTextContent("/chat/c1"));
    expect(screen.queryByText("Files")).toBeNull();
    expect(notFound()).toBeNull();
  });
});
