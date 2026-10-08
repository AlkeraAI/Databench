// Answering a permission ask is a write, and is treated as one.
//
// A 403, a 404 or a dropped connection must be said on the card, never escape
// to the page's unhandled-rejection channel. These cases pin what the card does.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConversationTurn } from "@alkera/chat-model";
import { ApiError } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";
import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";
import type { ChatDataSource } from "@/pages/workspace/chat/data/ChatDataSource";
import {
  createBrowserChatHost,
  installChatRuntime,
  resetChatRuntime,
} from "@/pages/workspace/chat/data";

const CHAT = "c1";
const REQUEST = "req-1";

function askingTurn(): ConversationTurn {
  return {
    id: "turn-1",
    author: "assistant",
    parts: [
      {
        kind: "permission",
        id: "p1",
        requestId: REQUEST,
        permissionKind: "bash",
        canonicalKind: "shell",
        patterns: ["rm -rf build"],
        status: "pending",
        prompting: true,
        options: [
          { optionId: "allow_once", name: "Allow once" },
          { optionId: "reject_once", name: "Deny" },
        ],
      },
    ],
  } as unknown as ConversationTurn;
}

function sourceThat(resolve: () => Promise<void>): { source: ChatDataSource; calls: () => number } {
  let calls = 0;
  const impl = {
    caps: { opencodeActive: false, modelCatalog: false },
    getChatTurns: async () => [askingTurn()],
    subscribeChat: () => () => undefined,
    listChats: async () => [{ id: CHAT, title: "A chat", updatedAt: "2026-09-19T12:00:00Z" }],
    resolvePermission: async () => {
      calls += 1;
      return resolve();
    },
    mayAllow: () => ({ allowed: true }),
    getChatActivity: () => ({}),
    getSubagentChats: () => [],
    getSubagentLabels: () => ({}),
    turnState: () => null,
    getPermissionMode: async () => null,
    setPermissionMode: async () => undefined,
    subscribeComposerPrefs: () => () => undefined,
    listSlashCommands: async () => [],
    getModelCatalog: async () => [],
  };
  return { source: impl as unknown as ChatDataSource, calls: () => calls };
}

function mount(props: Partial<Parameters<typeof ChatSurface>[0]> = {}) {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/chat/${CHAT}`]}>
        <ChatSurface chatId={CHAT} {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const rejections: unknown[] = [];
const noteRejection = (event: PromiseRejectionEvent): void => {
  rejections.push(event.reason);
};

beforeEach(() => {
  rejections.length = 0;
  window.addEventListener("unhandledrejection", noteRejection);
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("no network in this test"))));
});

afterEach(() => {
  window.removeEventListener("unhandledrejection", noteRejection);
  cleanup();
  resetChatRuntime();
  vi.unstubAllGlobals();
});

describe("answering a permission ask", () => {
  it("says why a refused answer did not land, and offers to send it again", async () => {
    const forbidden = sourceThat(() => Promise.reject(new ApiError(403, null)));
    installChatRuntime({ source: forbidden.source, host: createBrowserChatHost() });
    mount();

    const allow = await screen.findByRole("button", { name: /Allow once/ });
    await userEvent.click(allow);

    const said = await screen.findByText("You are not allowed to answer this request.");
    expect(said).toBeTruthy();
    // The ask is still there to answer, and nothing was reported to the page.
    expect(screen.getByRole("button", { name: /Allow once/ })).toBeTruthy();
    await waitFor(() => expect(rejections).toHaveLength(0));

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(forbidden.calls()).toBe(2));
  });

  it("settles the card when the ask is no longer there to answer", async () => {
    const gone = sourceThat(() => Promise.reject(new ApiError(404, null)));
    installChatRuntime({ source: gone.source, host: createBrowserChatHost() });
    mount();

    await userEvent.click(await screen.findByRole("button", { name: /Allow once/ }));

    await waitFor(() => expect(screen.queryByRole("button", { name: /Allow once/ })).toBeNull());
    expect(screen.queryByText("Retry")).toBeNull();
    expect(rejections).toHaveLength(0);
  });

  it("settles the card when the server says the ask was answered already, never a Retry that 409s again", async () => {
    // On a reload the snapshot can carry an ask the server has closed, so Allow
    // answers 409 ask_already_answered. A recorded decision is nothing to retry.
    const answered = sourceThat(() =>
      Promise.reject(
        new ApiError(409, {
          detail: { code: "ask_already_answered", message: "ask 'p1' has already been answered" },
        }),
      ),
    );
    installChatRuntime({ source: answered.source, host: createBrowserChatHost() });
    mount();

    await userEvent.click(await screen.findByRole("button", { name: /Allow once/ }));

    await waitFor(() => expect(screen.queryByRole("button", { name: /Allow once/ })).toBeNull());
    expect(screen.queryByText("Retry")).toBeNull();
    expect(screen.queryByText("The answer could not be sent.")).toBeNull();
    expect(rejections).toHaveLength(0);
  });

  it("keeps the reason and a Retry for a conflict that is not a recorded answer", async () => {
    const busy = sourceThat(() =>
      Promise.reject(new ApiError(409, { detail: { code: "chat_busy", message: "later" } })),
    );
    installChatRuntime({ source: busy.source, host: createBrowserChatHost() });
    mount();

    await userEvent.click(await screen.findByRole("button", { name: /Allow once/ }));

    await screen.findByText("The answer could not be sent.");
    expect(screen.getByRole("button", { name: /Allow once/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("says the limiter refused this attempt, and the same key sends it again", async () => {
    // A 429 is not a refusal of the answer: the ask is still outstanding and
    // the answer is still deliverable. The generic sentence would have the
    // reader hunting for what is wrong with their answer instead of waiting a
    // beat, and the card must keep the keys the second press needs.
    let refuseOnce = true;
    const limited = sourceThat(() => {
      if (!refuseOnce) return Promise.resolve();
      refuseOnce = false;
      return Promise.reject(
        new ApiError(
          429,
          { detail: { code: "rate_limited", message: "You are answering very quickly." } },
          "the request to /api/v1/chats/c1/answer failed",
          new Headers({ "retry-after": "3" }),
        ),
      );
    });
    installChatRuntime({ source: limited.source, host: createBrowserChatHost() });
    mount();

    await userEvent.click(await screen.findByRole("button", { name: /Allow once/ }));

    expect(await screen.findByText("Answering too quickly. Try again in a moment.")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Allow once/ })).toBeTruthy();
    await waitFor(() => expect(rejections).toHaveLength(0));

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(limited.calls()).toBe(2));
  });

  it("names the network rather than the reader when the tab is offline", async () => {
    const offline = sourceThat(() => Promise.reject(new TypeError("Failed to fetch")));
    installChatRuntime({ source: offline.source, host: createBrowserChatHost() });
    const online = vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
    mount();

    await userEvent.click(await screen.findByRole("button", { name: /Allow once/ }));
    expect(await screen.findByText("You are offline. The answer was not sent.")).toBeTruthy();
    online.mockRestore();
  });

  it("refuses a second press while the first answer is still on its way", async () => {
    const held: { release: () => void } = { release: () => undefined };
    const slow = sourceThat(() => new Promise<void>((resolve) => (held.release = resolve)));
    installChatRuntime({ source: slow.source, host: createBrowserChatHost() });
    mount();

    const allow = await screen.findByRole("button", { name: /Allow once/ });
    await userEvent.click(allow);
    await waitFor(() => expect(allow).toBeDisabled());
    await userEvent.click(allow);
    await userEvent.click(screen.getByRole("button", { name: /Deny/ }));
    expect(slow.calls()).toBe(1);
    held.release();
  });

  it("answers each ask of a stack on its own, with nothing of the last one's state", async () => {
    // Two asks stacked ("1 / 2"). The first is refused once and then retried;
    // the second must open clean — no Retry, no refusal line carried over —
    // and its answer must name ITS id.
    const answered: string[] = [];
    let refuseOnce = true;
    const stacked = {
      ...sourceThat(() => Promise.resolve()).source,
      getChatTurns: async () => [
        {
          ...askingTurn(),
          parts: [
            ...askingTurn().parts,
            { ...askingTurn().parts[0], id: "p2", requestId: "req-2", patterns: ["rm -rf dist"] },
          ],
        } as unknown as ConversationTurn,
      ],
      resolvePermission: async (_chat: string, requestId: string) => {
        if (refuseOnce) {
          refuseOnce = false;
          throw new ApiError(403, null);
        }
        answered.push(requestId);
      },
    } as unknown as ChatDataSource;
    installChatRuntime({ source: stacked, host: createBrowserChatHost() });
    mount();

    expect(await screen.findByLabelText("Request 1 of 2")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: /Allow once/ }));
    await screen.findByText("You are not allowed to answer this request.");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(answered).toEqual([REQUEST]));

    // The second ask, on its own: nothing of the first's refusal survives.
    await waitFor(() => expect(screen.queryByLabelText("Request 1 of 2")).toBeNull());
    expect(screen.getByRole("button", { name: /Allow once/ })).toBeEnabled();
    expect(screen.queryByText("Retry")).toBeNull();
    expect(screen.queryByText("You are not allowed to answer this request.")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /Allow once/ }));
    await waitFor(() => expect(answered).toEqual([REQUEST, "req-2"]));
  });

  it("does not offer an enabled decision on a chat whose machine is gone", async () => {
    const unreachable = sourceThat(() => Promise.resolve());
    installChatRuntime({ source: unreachable.source, host: createBrowserChatHost() });
    mount({ unavailable: true, unavailableReason: "This workspace is not reachable." });

    const allow = await screen.findByRole("button", { name: /Allow once/ });
    expect(allow).toBeDisabled();
    expect(screen.getByRole("button", { name: /Deny/ })).toBeDisabled();
    expect(screen.getByText("This workspace is not reachable.")).toBeTruthy();
    await userEvent.click(allow);
    expect(unreachable.calls()).toBe(0);
  });
});
