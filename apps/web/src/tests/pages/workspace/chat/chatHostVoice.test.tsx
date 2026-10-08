// Two things the chat surface must take from the shell around it rather than
// from the editor it was born in: what the empty composer invites, and which
// palette it paints in.
//
// Both were the editor's, hard-coded. The portal's composer read "Ask Alkera to
// plan, build, or run something…" — the coding agent's voice in a product about
// answers — and `useHostDark` called anything that was not `body.vscode-light`
// dark, which in a browser tab is always: a dark chat panel with dark tool
// cards inside an otherwise light product.

import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { createBrowserChatHost } from "@/pages/workspace/chat/data/browserChatHost";
import { useHostDark } from "@/pages/workspace/chat/useHostDark";

const { ds, host } = vi.hoisted(() => {
  const ds = {
    listChats: vi.fn(async () => [{ id: "c1", title: "Daily citations", permissionMode: "default" }]),
    listModels: async () => [],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listCommands: async () => [],
    getChatTurns: async () => [],
    getChatMessages: async () => [],
    searchFiles: async () => [],
    subscribeChat: () => () => {},
    subscribePermissionMode: () => () => {},
    getPermissionMode: async () => "default",
    setPermissionMode: async () => {},
    setEffort: async () => {},
    createChat: vi.fn(),
    sendUserMessage: vi.fn(async () => {}),
    cancelTurn: vi.fn(async () => ({ stopped: true })),
    fetchBlob: vi.fn(),
  };
  return { ds, host: { current: null as unknown } };
});

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/pages/workspace/chat/data")>();
  return {
    ...actual,
    chatData: () => ds,
    chatCaps: () => ({ opencodeActive: false }),
    chatHost: () => host.current,
  };
});

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";

/** The REAL browser host — the one the portal installs — over a stub engine. */
function browserHost() {
  return {
    ...createBrowserChatHost({ account: () => ({ email: null, webAppUrl: null }) }),
    engine: { request: vi.fn(async () => ({})) },
    auth: { openBrowser: vi.fn(), request: vi.fn(async () => ({})) },
    openPlanDocument: vi.fn(async () => undefined),
  };
}

/** The editor's host: no empty state of its own, so the composer keeps its own
 *  default copy. */
function editorHost() {
  return {
    ...browserHost(),
    kind: "vscode",
    emptyState: undefined,
    openFile: vi.fn(async () => undefined),
  };
}

function renderSurface() {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatSurface />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  host.current = browserHost();
  document.documentElement.removeAttribute("data-alkera-color-scheme");
  document.body.className = "";
  document.body.removeAttribute("data-alkera-color-scheme");
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("what the empty composer invites", () => {
  it("asks the portal's reader about their data", async () => {
    renderSurface();
    const box = await screen.findByRole("textbox", { name: /message/i });
    expect(box).toHaveAttribute("placeholder", "Ask a question about your data…");
  });

  it("leaves the editor's own invitation alone", async () => {
    host.current = editorHost();
    renderSurface();
    const box = await screen.findByRole("textbox", { name: /message/i });
    expect(box.getAttribute("placeholder")).toMatch(/plan, build, or run/);
  });
});

describe("which palette the chat paints in", () => {
  it("is light in a light portal", async () => {
    document.documentElement.setAttribute("data-alkera-color-scheme", "light");
    renderSurface();
    await screen.findByRole("textbox", { name: /message/i });
    const root = document.querySelector(".chat-root");
    expect(root).not.toBeNull();
    expect(root).not.toHaveAttribute("data-theme", "dark");
  });

  it("is dark in a dark portal", async () => {
    renderSurface();
    await screen.findByRole("textbox", { name: /message/i });
    expect(document.querySelector(".chat-root")).toHaveAttribute("data-theme", "dark");
  });
});

describe("the host-scheme hook", () => {
  it("is light under the editor's light theme class", () => {
    document.body.className = "vscode-light";
    const { result } = renderHook(() => useHostDark());
    expect(result.current).toBe(false);
  });

  it("is dark under the editor's dark theme class", () => {
    document.body.className = "vscode-dark";
    const { result } = renderHook(() => useHostDark());
    expect(result.current).toBe(true);
  });

  it("is light under the portal's light scheme, on the root or a sub-tree", () => {
    document.documentElement.setAttribute("data-alkera-color-scheme", "light");
    expect(renderHook(() => useHostDark()).result.current).toBe(false);

    document.documentElement.removeAttribute("data-alkera-color-scheme");
    document.body.setAttribute("data-alkera-color-scheme", "light");
    expect(renderHook(() => useHostDark()).result.current).toBe(false);
  });

  it("follows the portal when the scheme changes under it", async () => {
    const { result } = renderHook(() => useHostDark());
    expect(result.current).toBe(true);

    await act(async () => {
      document.documentElement.setAttribute("data-alkera-color-scheme", "light");
    });

    await waitFor(() => expect(result.current).toBe(false));
  });
});
