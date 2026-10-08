// Where a plan opens under the chat-ui frontend: in VS Code the approval is
// handed to the host's document seam and the router does not move; in the
// browser preview there is no editor to receive it, so the same click keeps the
// stacked plan page with its trail back.

import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { QuestionConversationPart } from "@alkera/chat-model";

const { ds, hostMock } = vi.hoisted(() => {
  const ds = {
    listChats: vi.fn(async () => [{ id: "c1", title: "Data audit", permissionMode: "default" }]),
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
    fetchBlob: vi.fn(),
  };
  const hostMock = {
    kind: "vscode" as string,
    engine: { request: vi.fn(async () => ({})) },
    auth: { request: vi.fn(async () => ({})) },
    subscribe: () => () => {},
    runCommand: vi.fn(async () => {}),
    openFile: vi.fn(async () => {}),
    openPlanDocument: vi.fn(async () => {}),
    workspacePath: () => null,
    account: () => ({ email: null, webAppUrl: null }),
    onAccountChange: () => () => {},
  };
  return { ds, hostMock };
});

vi.mock("@/pages/workspace/chat/data", () => ({
  chatData: () => ds,
  chatCaps: () => ({ opencodeActive: true }),
  chatHost: () => hostMock,
  errorText: (err: unknown) => String(err),
  exportBlob: async () => {},
  // The chat store reads this on its send path. This mock replaces the whole
  // module, so any named export it omits is missing at the call site.
  isSessionNotOpen: () => false,
  refetchWhileErrored: () => false as const,
  refetchWhileErroredOrEmpty: () => false as const,
  refetchWhileNoChatDefault: () => false as const,
}));

import { useChatController } from "@/pages/workspace/chat/controller";

let locationNow = "";
function LocationProbe(): null {
  const location = useLocation();
  locationNow = `${location.pathname}${location.search}`;
  return null;
}

function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/chat/c1"]}>
          <LocationProbe />
          {children}
        </MemoryRouter>
      </QueryClientProvider>
    );
  };
}

function planApproval(): QuestionConversationPart {
  return {
    id: "q1",
    kind: "question",
    requestId: "q1",
    questionKind: "plan_approval",
    questions: [
      { question: "Ship the migration in two steps?", header: null, options: [], multiple: false, custom: false },
    ],
    status: "pending",
    planMarkdown: "# The plan",
  };
}

async function openPlanFromChat(): Promise<void> {
  const { result } = renderHook(() => useChatController({ chatId: "c1" }), { wrapper: makeWrapper() });
  // The document title flows from the cached chat list; wait for it so the
  // asserted request carries the chat's real name, not the placeholder.
  await waitFor(() => expect(result.current.identity.currentTitle).toBe("Data audit"));
  act(() => result.current.nav.openPlan(planApproval()));
}

beforeEach(() => {
  locationNow = "";
  hostMock.openPlanDocument.mockClear();
});

describe("openPlan under the chat-ui frontend", () => {
  it("routes to the host document seam, leaving the conversation in place", async () => {
    hostMock.kind = "vscode";
    await openPlanFromChat();
    expect(hostMock.openPlanDocument).toHaveBeenCalledExactlyOnceWith({
      title: "Data audit plan",
      markdown: "# The plan",
    });
    expect(locationNow).toBe("/chat/c1");
  });

  it("falls back to the stacked plan page in the browser, trail attached", async () => {
    hostMock.kind = "browser";
    await openPlanFromChat();
    expect(hostMock.openPlanDocument).not.toHaveBeenCalled();
    // The portal's own path for the same surface — the editor's `/editor/plan/…`
    // is a route only the webview declares.
    expect(locationNow).toBe(`/chat/c1/plan/q1?backTo=${encodeURIComponent("/chat/c1")}`);
  });
});
