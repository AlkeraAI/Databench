// Answering an ask from a browser tab, through the real chat surface and the
// real `CloudDataSource`.
//
// The dock puts the ask WHERE THE COMPOSER GOES, so a browser that cannot
// answer one is a browser that can neither release the turn nor type: the card
// rendered, the reader pressed an option, and the promise rejected unhandled
// against a shell that has no engine channel, while the machine sat on an ask
// with no timeout. This drives the whole path instead — card → controller →
// source → `POST /chats/{id}/answer` — and checks the relay shape the mirror
// validates comes out the other end.
//
// The first half arrives on the LIVE lane; the second is a COLD open of a chat
// the box parked on an ask, which the box re-offers under the same id (see
// transcriptParity for where the two shells part ways on a replayed ask).

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { keys } from "@/api/keys";
import { createQueryClient, queryClient } from "@/api/queryClient";
import type { DocHandle, DocMessage } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

const NOW = "2026-09-06T12:00:00Z";

const { answerInterrupt, installed } = vi.hoisted(() => ({
  answerInterrupt: vi.fn(async () => undefined),
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
  chatCaps: () => ({ opencodeActive: false }),
  refetchWhileErrored: () => false as const,
  refetchWhileNoChatDefault: () => false as const,
  refetchWhileErroredOrEmpty: () => false as const,
}));

import { ChatSurface } from "@/pages/workspace/chat/ChatSurface";

/** A chat document that only replays what a test pushes into it. */
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
  return {
    handle,
    /** The server's merged meta frame — where the chat's permission mode reaches
     *  a reader (`noteMetaMode`). */
    setMeta(meta: Record<string, unknown>): void {
      listeners.forEach((listener) =>
        listener({
          kind: "op",
          ephemeral: false,
          peerId: "srv:1",
          epoch: 1,
          seq: 0,
          payload: { op_id: "op-meta", intent: "set_meta", meta },
        } as unknown as DocMessage<unknown>),
      );
    },
    /** ``from`` is the first sequence number: a later append continues the
     *  lane rather than replaying it from one. */
    append(events: Record<string, unknown>[], from = 1): void {
      events.forEach((payload, i) =>
        listeners.forEach((listener) =>
          listener({
            kind: "op",
            ephemeral: false,
            peerId: "pub:1",
            epoch: 1,
            seq: from + i,
            payload: {
              op_id: `op-${i}-${String(payload.event_type)}`,
              intent: "append",
              events: [
                {
                  // An entry that names its own id (as the publisher's do) keeps
                  // it — the source de-duplicates by event id, so two appends
                  // about the same request must be told apart the way the wire
                  // tells them apart.
                  event_id:
                    typeof payload.event_id === "string"
                      ? payload.event_id
                      : `${String(payload.request_id ?? payload.message_id ?? i)}-${i}`,
                  role: "assistant",
                  kind: String(payload.event_type),
                  payload,
                },
              ],
            },
          } as unknown as DocMessage<unknown>),
        ),
      );
    },
  };
}

/** The real source over a stubbed REST surface, with a live ask already folded. */
/** The chat row as the server serves it in `mode`, verdict included, and the
 *  same mode on the document's meta. */
const SERVER_VERDICT: Record<string, string | null> = {
  default: null,
  read_only: "This workspace is read-only, so it won't run this.",
};
function servedIn(doc: { setMeta: (meta: Record<string, unknown>) => void }, mode: string, meta: Record<string, unknown> = {}) {
  queryClient.setQueryData(keys.chats.one("c1"), {
    id: "c1",
    title: "Chat",
    permission_mode: mode,
    approval_refusal: SERVER_VERDICT[mode] ?? null,
    created_at: NOW,
    updated_at: NOW,
  });
  doc.setMeta({ permission_mode: mode, ...meta });
}

function chatWaitingOn(events: Record<string, unknown>[]) {
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
    } as never,
    openDoc: () => doc.handle as DocHandle<never>,
    acquire: () => () => undefined,
    clientId: () => "client-1",
  });
  installed.source = source;
  // Open the chat's live lane and push the turn onto it, as the publisher does.
  source.subscribeChat("c1", () => undefined);
  doc.append([
    { event_type: "message.created", message_id: "a1", role: "assistant", time: NOW },
    ...events,
  ]);
  return { source, doc };
}

function renderChat() {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:chatId" element={<ChatSurface chatId="c1" />} />
          <Route path="*" element={<p>Elsewhere</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const READ_ASK = {
  event_type: "permission.request",
  request_id: "perm-read",
  permission_kind: "network",
  canonical_kind: "network",
  prompting: true,
  patterns: ["https://api.example.com/*"],
  subject: { capability: "http", effect: "read", targets: [] },
  options: [
    { option_id: "allow_once", name: "Allow once" },
    { option_id: "reject_once", name: "Reject" },
  ],
};

const WRITE_ASK = {
  event_type: "permission.request",
  request_id: "perm-write",
  permission_kind: "bash",
  canonical_kind: "shell",
  prompting: true,
  patterns: ["dbt run"],
  options: [
    { option_id: "allow_once", name: "Allow once" },
    { option_id: "allow_always", name: "Always allow" },
    { option_id: "reject_once", name: "Reject" },
  ],
};

const QUESTION = {
  event_type: "question.request",
  request_id: "q-1",
  questions: [
    {
      question: "Which warehouse should I read from?",
      options: [{ label: "The staging warehouse" }, { label: "The production warehouse" }],
      multiple: false,
      custom: false,
    },
  ],
};

beforeEach(() => {
  queryClient.clear();
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
});

describe("a browser tab answers the asks a turn stops on", () => {
  it("relays a read-class permission the reader allows", async () => {
    chatWaitingOn([READ_ASK]);
    renderChat();

    const allow = await screen.findByRole("button", { name: /allow once/i });
    fireEvent.click(allow);

    await waitFor(() =>
      expect(answerInterrupt).toHaveBeenCalledWith("c1", {
        interrupt_id: "perm-read",
        option_id: "allow_once",
        reject: false,
      }),
    );
    // The optimistic resolution clears the dock, so the composer comes back
    // without waiting for the machine's echo.
    await waitFor(() => expect(screen.getByRole("textbox")).toBeInTheDocument());
  });

  it("keeps the draft a reader typed when an ask comes up, and gives it back after", async () => {
    // The composer was unmounted while an ask held the dock, and the text a
    // person had typed went with it: they answered the card and came back to
    // an empty box.
    const { doc } = chatWaitingOn([]);
    renderChat();
    const box = (await screen.findByRole("textbox")) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "and also the refunds view" } });
    expect(box.value).toBe("and also the refunds view");

    act(() => doc.append([READ_ASK], 2));
    const allow = await screen.findByRole("button", { name: /allow once/i });
    // Out of the reader's way while the ask has the dock: hidden, not gone.
    expect(screen.queryByRole("textbox")).toBeNull();

    fireEvent.click(allow);
    const again = (await screen.findByRole("textbox")) as HTMLTextAreaElement;
    expect(again.value).toBe("and also the refunds view");
  });

  it("relays a question's answers", async () => {
    chatWaitingOn([QUESTION]);
    renderChat();

    fireEvent.click(await screen.findByRole("radio", { name: /staging warehouse/i }));
    fireEvent.click(screen.getByRole("button", { name: /^submit$/i }));

    await waitFor(() =>
      expect(answerInterrupt).toHaveBeenCalledWith("c1", {
        interrupt_id: "q-1",
        answers: [["The staging warehouse"]],
        reject: false,
      }),
    );
    await waitFor(() => expect(screen.getByRole("textbox")).toBeInTheDocument());
  });

  // The mirror opens the session read-only and discards any answer that would
  // authorize a write. Offering Allow here would be a second control that lies:
  // the reader presses it, the machine logs an ignored relay, and the turn stays
  // stopped. Declining still releases it.
  it("shows a write-class ask as refused, and still lets the reader decline it", async () => {
    const { doc } = chatWaitingOn([WRITE_ASK]);
    servedIn(doc, "read_only");
    renderChat();

    expect(await screen.findByText(/this workspace is read-only/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /allow once/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /always allow/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /reject/i }));
    await waitFor(() =>
      expect(answerInterrupt).toHaveBeenCalledWith("c1", {
        interrupt_id: "perm-write",
        option_id: "reject_once",
        reject: false,
      }),
    );
  });
});

// --- the asks a workspace box parks ------------------------------------------
// What the mirror puts on the chat document for one in-folder write in
// `default` mode, entry for entry (`test_cloud_write_fence.py`, "a parked ask
// is announced"): the harness's own `permission.request` — appended BEFORE the
// box's policy has run, so it never says whether a person is being asked — and
// then, once the ask is parked on the readers, the mirror's copy of it tagged
// `prompting`, under its own event id. The tag is the browser's only word that
// the controls are live: the untagged entry alone raises none (an ask the
// policy is about to answer would flash a card), the tagged one raises them.

const BOX_WRITE_ASK = {
  event_type: "permission.request",
  event_id: "ev-req-w",
  time: NOW,
  session_id: "c1",
  request_id: "req-w",
  tool_call_id: "call-req-w",
  permission_kind: "edit",
  canonical_kind: "edit",
  patterns: [],
  subject: {
    capability: "fs",
    effect: "write",
    operation: "edit",
    raw: "notes.txt",
    targets: [{ kind: "file", name: "notes.txt" }],
    classifier: "opencode-tool",
  },
  options: [
    { option_id: "allow_once", name: "Allow" },
    { option_id: "reject_once", name: "Reject" },
  ],
  insertions: 1,
  deletions: 0,
  preview: {
    kind: "diff",
    content: "--- /dev/null\n+++ notes.txt\n@@ -0,0 +1 @@\n+OK\n",
    title: "notes.txt",
    path: "notes.txt",
  },
};

const BOX_WRITE_ASK_ANNOUNCED = {
  ...BOX_WRITE_ASK,
  event_id: "ev-req-w-prompting",
  prompting: true,
};

describe("a default-mode chat on a workspace box", () => {
  it("raises the write ask the box parks, with Allow and Reject, and relays the reader's Allow", async () => {
    const { doc } = chatWaitingOn([]);
    servedIn(doc, "default");
    renderChat();
    const composer = await screen.findByRole("textbox");
    expect(composer).toBeInTheDocument();

    // The harness's own append: the diff is on record, the decision is not
    // yet anyone's — no controls, and the composer is still the composer.
    doc.append([BOX_WRITE_ASK]);
    await waitFor(() => expect(screen.getByRole("textbox")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /^allow\b/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^reject\b/i })).toBeNull();

    // The mirror parked it on the readers: the card, and nothing that reads
    // as a turn still working or a run to stop.
    doc.append([BOX_WRITE_ASK_ANNOUNCED]);
    const allow = await screen.findByRole("button", { name: /^allow\b/i });
    expect(screen.getByRole("button", { name: /^reject\b/i })).toBeInTheDocument();
    expect(screen.queryByText(/this workspace is read-only/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /^stop$/i })).toBeNull();
    expect(screen.queryByText(/working/i)).toBeNull();

    fireEvent.click(allow);
    await waitFor(() =>
      expect(answerInterrupt).toHaveBeenCalledWith("c1", {
        interrupt_id: "req-w",
        option_id: "allow_once",
        reject: false,
      }),
    );
    expect(answerInterrupt).toHaveBeenCalledTimes(1);
    // The decision made, the composer is back before the machine echoes.
    await waitFor(() => expect(screen.getByRole("textbox")).toBeInTheDocument());
  });

  it("keeps a write ask the box announced unapprovable while the chat is read-only", async () => {
    // The tag says "yours to answer"; the mode still says what an answer may
    // approve. A read-only chat's Allow would be a button the box discards.
    const { doc } = chatWaitingOn([]);
    servedIn(doc, "read_only");
    renderChat();
    doc.append([BOX_WRITE_ASK, BOX_WRITE_ASK_ANNOUNCED]);

    expect(await screen.findByText(/this workspace is read-only/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^allow\b/i })).toBeNull();
    expect(screen.getByRole("button", { name: /^reject\b/i })).toBeInTheDocument();
  });
});

// --- a cold open of a chat holding an ask ------------------------------------
// A pending ask is state of the chat, not of the process that raised it: the
// box that opens the chat next re-offers the same ask and resolves the answer.
// So a reader who opens the chat cold — a reload mid-ask, a colleague joining,
// a return after the box slept — sees the card with its controls, however old
// the machine's `working` stamp is, and their answer goes out under the same
// request id. Retiring the replayed ask was how a reload mid-turn orphaned the
// next ask for the whole of the old wall-clock budget.

const BOX_ANNOUNCED_WRITE_ASK = {
  ...BOX_WRITE_ASK,
  event_id: "ev-req-w-prompting",
  prompting: true,
};

/** A machine row as `GET /chats/{id}/messages` serves it: the entry the box
 *  published, event one level in. */
function machineRow(seq: number, payload: Record<string, unknown>) {
  return {
    id: `row-${seq}`,
    seq,
    event_id: String(payload.event_id),
    role: "assistant",
    kind: String(payload.event_type),
    payload: {
      event_id: String(payload.event_id),
      role: "assistant",
      kind: String(payload.event_type),
      payload,
    },
    created_at: NOW,
  };
}

/** The real source over a transcript the reader opens COLD: nothing on the live
 *  lane, everything from the durable read, and a `working` stamp the machine
 *  wrote long ago (or never). */
function chatReopenedOn(rows: ReturnType<typeof machineRow>[]) {
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
            created_at: NOW,
            updated_at: NOW,
            last_seq: rows.length,
          },
        ],
        next_cursor: null,
      }),
      createChat: vi.fn(),
      listMessages: async () => ({ items: rows, next_after_seq: null, resync_from: null }),
      postMessage: vi.fn(),
      answerInterrupt,
    } as never,
    openDoc: () => doc.handle as DocHandle<never>,
    acquire: () => () => undefined,
    clientId: () => "client-1",
  });
  installed.source = source;
  source.subscribeChat("c1", () => undefined);
  // The machine's last word on the turn, stamped an hour ago: far past what a
  // replay would trust — exactly the reload-mid-ask case.
  servedIn(doc, "default", {
    turn_state: { state: "working", at: "2026-09-06T11:00:00Z" },
    turn_state_at: "2026-09-06T11:00:00Z",
  });
  return { source, doc };
}

describe("a cold open of a chat whose box is waiting on an ask", () => {
  it("draws the announced ask with its controls and relays the answer under the same id", async () => {
    chatReopenedOn([
      machineRow(1, { event_type: "message.created", event_id: "ev-a1", message_id: "a1", role: "assistant", time: NOW }),
      machineRow(2, BOX_WRITE_ASK),
      machineRow(3, BOX_ANNOUNCED_WRITE_ASK),
    ]);
    renderChat();

    const allow = await screen.findByRole("button", { name: /\ballow\b/i });
    expect(screen.getByRole("button", { name: /\breject\b/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^stop$/i })).toBeNull();
    fireEvent.click(allow);

    await waitFor(() =>
      expect(answerInterrupt).toHaveBeenCalledWith("c1", {
        interrupt_id: "req-w",
        option_id: "allow_once",
        reject: false,
      }),
    );
    await waitFor(() => expect(screen.getByRole("textbox")).toBeInTheDocument());
  });

  it("keeps an ask nobody announced retired, so a policy-decided ask never flashes a card", async () => {
    chatReopenedOn([
      machineRow(1, { event_type: "message.created", event_id: "ev-a1", message_id: "a1", role: "assistant", time: NOW }),
      machineRow(2, BOX_WRITE_ASK),
    ]);
    renderChat();

    expect(await screen.findByRole("textbox")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /\ballow\b/i })).toBeNull();
    expect(answerInterrupt).not.toHaveBeenCalled();
  });

  it("keeps an ask the transcript resolves retired", async () => {
    chatReopenedOn([
      machineRow(1, { event_type: "message.created", event_id: "ev-a1", message_id: "a1", role: "assistant", time: NOW }),
      machineRow(2, BOX_WRITE_ASK),
      machineRow(3, BOX_ANNOUNCED_WRITE_ASK),
      machineRow(4, {
        event_type: "permission.resolved",
        event_id: "ev-req-w-resolved",
        request_id: "req-w",
        option_id: "reject_once",
        decided_by: "user",
        time: NOW,
      }),
    ]);
    renderChat();

    expect(await screen.findByRole("textbox")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /\ballow\b/i })).toBeNull();
  });

  it("keeps an ask retired when a reader's answer was recorded while no box held it", async () => {
    // The answer route records a decision given while the chat was asleep as
    // the ask's resolution, under the READER's role rather than the machine's
    // (`POST /chats/{id}/answer`). A cold open reads that row like any other
    // resolution: the card is answered, the composer is back.
    const recorded = {
      event_type: "permission.resolved",
      event_id: "answer-req-w",
      request_id: "req-w",
      option_id: "allow_once",
      decided_by: "user",
      time: NOW,
    };
    chatReopenedOn([
      machineRow(1, { event_type: "message.created", event_id: "ev-a1", message_id: "a1", role: "assistant", time: NOW }),
      machineRow(2, BOX_WRITE_ASK),
      machineRow(3, BOX_ANNOUNCED_WRITE_ASK),
      {
        id: "row-4",
        seq: 4,
        event_id: "answer-req-w",
        role: "user",
        kind: "permission.resolved",
        payload: { event_id: "answer-req-w", role: "user", kind: "permission.resolved", payload: recorded },
        created_at: NOW,
      },
    ]);
    renderChat();

    expect(await screen.findByRole("textbox")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /\ballow\b/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /\breject\b/i })).toBeNull();
  });

  it("retires the replayed ask when its resolution lands on the live lane", async () => {
    const { doc } = chatReopenedOn([
      machineRow(1, { event_type: "message.created", event_id: "ev-a1", message_id: "a1", role: "assistant", time: NOW }),
      machineRow(2, BOX_WRITE_ASK),
      machineRow(3, BOX_ANNOUNCED_WRITE_ASK),
    ]);
    renderChat();
    expect(await screen.findByRole("button", { name: /\ballow\b/i })).toBeInTheDocument();

    // A colleague answered it from their tab: the box settles the ask and the
    // echo reaches this reader on the socket.
    doc.append([
      {
        event_type: "permission.resolved",
        event_id: "ev-req-w-resolved",
        request_id: "req-w",
        option_id: "allow_once",
        decided_by: "user",
        time: NOW,
      },
    ]);

    await waitFor(() => expect(screen.queryByRole("button", { name: /\ballow\b/i })).toBeNull());
    expect(await screen.findByRole("textbox")).toBeInTheDocument();
    expect(answerInterrupt).not.toHaveBeenCalled();
  });

  it("draws a replayed question with its options and relays the answer under the same id", async () => {
    chatReopenedOn([
      machineRow(1, { event_type: "message.created", event_id: "ev-a1", message_id: "a1", role: "assistant", time: NOW }),
      machineRow(2, { ...QUESTION, event_id: "ev-q-1", time: NOW }),
    ]);
    renderChat();

    fireEvent.click(await screen.findByRole("radio", { name: /staging warehouse/i }));
    fireEvent.click(screen.getByRole("button", { name: /^submit$/i }));

    await waitFor(() =>
      expect(answerInterrupt).toHaveBeenCalledWith("c1", {
        interrupt_id: "q-1",
        answers: [["The staging warehouse"]],
        reject: false,
      }),
    );
    await waitFor(() => expect(screen.getByRole("textbox")).toBeInTheDocument());
  });
});
