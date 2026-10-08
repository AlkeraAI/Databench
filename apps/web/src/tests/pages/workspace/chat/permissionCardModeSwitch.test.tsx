// The permission mode moves while an ask is up, from the card that holds the
// dock, as it does on Slack. Choosing a mode answers nothing on this side: the
// machine decides the ask again under the new mode, and whatever it decides
// reaches the reader the way every resolution does — on the chat's own lane.
// Driven through the real chat surface over the real `CloudDataSource`.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { keys } from "@/api/keys";
import { createQueryClient, queryClient } from "@/api/queryClient";
import type { DocHandle, DocMessage } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

const NOW = "2026-09-29T12:00:00Z";

const { answerInterrupt, setPermissionMode, installed } = vi.hoisted(() => ({
  answerInterrupt: vi.fn(async () => undefined),
  setPermissionMode: vi.fn(async (chatId: string, mode: string) => ({
    id: chatId,
    title: "Chat",
    permission_mode: mode,
    approval_refusal: mode === "read_only" || mode === "plan" ? "won't run this" : null,
    created_at: "2026-09-29T12:00:00Z",
    updated_at: "2026-09-29T12:00:00Z",
  })),
  installed: { source: null as unknown },
}));

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/pages/workspace/chat/data")>()),
  chatHost: () => ({
    kind: "browser" as const,
    engine: {
      request: () => Promise.reject(new Error("the browser portal has no engine channel")),
    },
    runCommand: async () => {},
    openFile: async () => {},
    subscribe: () => () => {},
    workspacePath: () => null,
    openPlanDocument: async () => {},
    account: () => ({ email: "analyst@tideline.example", webAppUrl: null }),
    onAccountChange: () => () => {},
    saveFile: async () => {},
    auth: { openBrowser: () => {} },
  }),
  chatData: () => installed.source,
  chatCaps: () => (installed.source as CloudDataSource).caps,
  refetchWhileErrored: () => false as const,
  refetchWhileNoChatDefault: () => false as const,
  refetchWhileErroredOrEmpty: () => false as const,
}));

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";

function fakeDoc() {
  const listeners = new Set<(m: DocMessage<unknown>) => void>();
  const handle: DocHandle<unknown> = {
    onMessage: (listener) => {
      listeners.add(listener);
      return () => void listeners.delete(listener);
    },
    onPhase: () => () => undefined,
    getPhase: () => ({
      phase: "live",
      epoch: 1,
      seq: 0,
      peerId: "p:1",
      canWrite: false,
      pending: 0,
      error: null,
    }),
    sendOp: () => Promise.reject(new Error("a reader never writes to a chat document")),
    dispose: () => listeners.clear(),
  };
  let seq = 0;
  const emit = (payload: Record<string, unknown>) =>
    listeners.forEach((listener) => listener(payload as unknown as DocMessage<unknown>));
  return {
    handle,
    setMeta(meta: Record<string, unknown>): void {
      emit({
        kind: "op",
        ephemeral: false,
        peerId: "srv:1",
        epoch: 1,
        seq: 0,
        payload: { op_id: `op-meta-${seq}`, intent: "set_meta", meta },
      });
    },
    append(events: Record<string, unknown>[]): void {
      for (const payload of events) {
        seq += 1;
        emit({
          kind: "op",
          ephemeral: false,
          peerId: "pub:1",
          epoch: 1,
          seq,
          payload: {
            op_id: `op-${seq}`,
            intent: "append",
            events: [
              {
                event_id: typeof payload.event_id === "string" ? payload.event_id : `ev-${seq}`,
                role: "assistant",
                kind: String(payload.event_type),
                payload,
              },
            ],
          },
        });
      }
    },
  };
}

function openChat() {
  const doc = fakeDoc();
  const source = new CloudDataSource({
    rest: {
      listChats: async () => ({
        items: [
          {
            id: "c1",
            title: "Chat",
            machine_id: "m1",
            machine_status: "ready",
            permission_mode: "default",
            created_at: NOW,
            updated_at: NOW,
            last_seq: 0,
          },
        ],
        next_cursor: null,
      }),
      createChat: vi.fn(),
      listMessages: async () => ({ items: [], next_after_seq: 0, resync_from: null }),
      postMessage: vi.fn(),
      answerInterrupt,
      setPermissionMode,
      getChat: vi.fn(async () => ({
        id: "c1",
        title: "Chat",
        permission_mode: "default",
        approval_refusal: null,
        created_at: NOW,
        updated_at: NOW,
      })),
    } as never,
    openDoc: () => doc.handle as DocHandle<never>,
    acquire: () => () => undefined,
    clientId: () => "client-1",
  });
  installed.source = source;
  source.subscribeChat("c1", () => undefined);
  doc.append([{ event_type: "message.created", message_id: "a1", role: "assistant", time: NOW }]);
  // The row as the server serves it, verdict included.
  queryClient.setQueryData(keys.chats.one("c1"), {
    id: "c1",
    title: "Chat",
    permission_mode: "default",
    approval_refusal: null,
    created_at: NOW,
    updated_at: NOW,
  });
  doc.setMeta({ permission_mode: "default" });
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatSurface chatId="c1" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return doc;
}

const WRITE_ASK = {
  event_type: "permission.request",
  event_id: "ev-req-w",
  request_id: "req-w",
  permission_kind: "edit",
  canonical_kind: "edit",
  patterns: ["notes.txt"],
  subject: {
    capability: "fs",
    effect: "write",
    operation: "edit",
    raw: "notes.txt",
    targets: [{ kind: "file", name: "notes.txt" }],
  },
  options: [
    { option_id: "allow_once", name: "Allow once" },
    { option_id: "reject_once", name: "Reject" },
  ],
};

const ANNOUNCED = { ...WRITE_ASK, event_id: "ev-req-w-prompting", prompting: true };

async function switchModeFromTheCard(to: RegExp): Promise<void> {
  const trigger = await screen.findByRole("button", { name: /permission mode: default/i });
  fireEvent.click(trigger);
  fireEvent.click(await screen.findByRole("menuitemradio", { name: to }));
}

beforeEach(() => {
  queryClient.clear();
  vi.clearAllMocks();
  // The source keeps the chat row in the app's own cache; each case opens a
  // chat of its own.
  queryClient.clear();
});

afterEach(() => {
  cleanup();
});

describe("the permission mode moves while an ask is up", () => {
  it("switches from the card without answering, and the machine's resolution clears it", async () => {
    const doc = openChat();
    doc.append([WRITE_ASK, ANNOUNCED]);
    expect(await screen.findByRole("button", { name: /^allow\b/i })).toBeInTheDocument();

    await switchModeFromTheCard(/read-only/i);

    await waitFor(() => expect(setPermissionMode).toHaveBeenCalledWith("c1", "read_only"));
    // The switch is not an answer: the reader refused nothing.
    expect(answerInterrupt).not.toHaveBeenCalled();

    // The machine decided the ask again under the new mode and refused it.
    doc.append([
      {
        event_type: "permission.resolved",
        request_id: "req-w",
        option_id: "reject_once",
        decided_by: "policy",
      },
    ]);
    await waitFor(() => expect(screen.getByRole("textbox")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /^allow\b/i })).toBeNull();
  });

  it("keeps the card when the new mode still asks", async () => {
    const doc = openChat();
    doc.append([WRITE_ASK, ANNOUNCED]);
    await screen.findByRole("button", { name: /^allow\b/i });

    await switchModeFromTheCard(/^auto/i);
    await waitFor(() => expect(setPermissionMode).toHaveBeenCalledWith("c1", "auto"));

    // Nothing resolved it: the same ask is still the reader's to answer.
    expect(screen.getByRole("button", { name: /^allow\b/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /permission mode: auto/i })).toBeInTheDocument();
    expect(answerInterrupt).not.toHaveBeenCalled();
  });
});
