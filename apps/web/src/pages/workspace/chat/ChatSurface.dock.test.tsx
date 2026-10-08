// The dock holds one thing at a time, and the order is not negotiable: a
// permission the agent is blocked on outranks a question, and a question
// outranks the composer. Anything else lets a user type into a chat that is
// waiting on them, or answer the second ask while the first is still stuck.
//
// The ladder has to unwind too. Once both asks are settled the composer comes
// back on its own — a dock that stays stuck on a resolved card is a dead chat.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";

const ASK = "Which warehouse should this model land in?";
const ALLOW = { optionId: "allow_once", name: "Let it run once" };
const DENY = { optionId: "reject_once", name: "Stop here" };

const USER = { id: "u0", author: "user", status: "done", parts: [{ id: "u0-t", kind: "text", text: "Build it" }] };

function permissionPart(id: string, requestId: string, pattern: string) {
  return {
    id,
    kind: "permission",
    requestId,
    permissionKind: "bash",
    canonicalKind: "shell",
    patterns: [pattern],
    options: [ALLOW, DENY],
    status: "pending",
    prompting: true,
  };
}

/** The ask a plan-mode turn ends on: a question, answered with the stance to
 *  continue in. Nothing about it is a permission, so nothing about the chat's
 *  own stance may gate it — which is the point of the case that uses it. */
const PLAN_PART = {
  id: "q1",
  kind: "question",
  requestId: "req-plan",
  questionKind: "plan_approval",
  planMarkdown: "# Ship the loader\n\nStep one.",
  questions: [
    {
      question: "Approve this plan?",
      header: null,
      options: [
        { label: "Accept — run normally (ask before each change)" },
        { label: "Accept — auto mode (run automatically, pause for risky steps)" },
        { label: "Accept — bypass all permission prompts" },
      ],
      multiple: false,
      custom: false,
    },
  ],
  status: "pending",
};

const QUESTION_PART = {
  id: "q0",
  kind: "question",
  requestId: "req-question",
  questionKind: "question",
  questions: [
    {
      question: ASK,
      header: null,
      options: [{ label: "The staging warehouse" }, { label: "The production warehouse" }],
      multiple: false,
      custom: false,
    },
  ],
  status: "pending",
};

const { ds, state, hostRequest } = vi.hoisted(() => {
  const state = { turns: [] as unknown[] };
  const ds = {
    listChats: async () => [{ id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" }],
    listModels: async () => [
      { id: "model-alpha", displayName: "Model Alpha", efforts: [], defaultEffort: null },
    ],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listCommands: async () => [] as unknown[],
    listContext: async () => ({ total: 0, items: [] as unknown[] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] as unknown[] }),
    getChatTurns: vi.fn(async () => state.turns),
    searchFiles: async () => [] as unknown[],
    subscribeChat: () => () => {},
    subscribePermissionMode: () => () => {},
    getPermissionMode: async () => "default",
    setPermissionMode: vi.fn(async () => {}),
    setEffort: vi.fn(async () => {}),
    createChat: vi.fn(),
    sendUserMessage: vi.fn(async () => ({})),
    // Answering an ask is a source verb: the extension settles it on the local
    // daemon, the browser relays it to the machine that raised it.
    resolvePermission: vi.fn(async () => {}),
    answerQuestion: vi.fn(async () => {}),
    rejectQuestion: vi.fn(async () => {}),
    mayAllow: () => ({ allowed: true }),
  };
  const hostRequest = vi.fn(async () => ({}));
  return { ds, state, hostRequest };
});

vi.mock("./data", () => ({
  chatHost: () => ({
    engine: { request: hostRequest },
    runCommand: async () => {},
    openFile: async () => {},
    subscribe: () => () => {},
    workspacePath: () => null,
    account: () => ({ email: null, webAppUrl: null }),
    onAccountChange: () => () => {},
    auth: {
      openBrowser: async () => {},
      getState: () => undefined,
      setState: () => {},
      on: () => () => {},
      ready: () => {},
    },
  }),

  chatData: () => ds,
  refetchWhileErrored: () => false as const,
  refetchWhileNoChatDefault: () => false as const,
  refetchWhileErroredOrEmpty: () => false as const,
  chatCaps: () => ({ opencodeActive: true }),
  isSessionNotOpen: () => false,
  errorText: (err: unknown) =>
    typeof err === "object" && err !== null && typeof (err as { message?: unknown }).message === "string"
      ? (err as { message: string }).message
      : String(err),
  exportBlob: async () => {},
}));

import { ChatSurface } from "./ChatSurface";
import { useChatStore } from "./chatStore";

/** The permission ask, addressed by the name it announces. */
const permissionCard = (): HTMLElement | null => screen.queryByRole("region", { name: /run this command/i });
/** The question ask in the DOCK, which announces the question itself. The
 *  transcript's mirror of the same ask announces its state instead, so the two
 *  never collide. */
const questionCard = (): HTMLElement | null => screen.queryByRole("region", { name: ASK });
const composer = (): HTMLElement | null => screen.queryByRole("textbox", { name: /message/i });

function renderChat() {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:id" element={<ChatSurface />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  state.turns = [];
  vi.clearAllMocks();
  // The stance a case put the source in does not outlive it.
  ds.mayAllow = () => ({ allowed: true });
  useChatStore.setState({ byId: {}, composerPrefs: {} });
});

describe("ChatSurface dock precedence", () => {
  it("holds the permission ask over both the question and the composer", async () => {
    state.turns = [
      USER,
      { id: "a0", author: "assistant", status: "running", parts: [permissionPart("p0", "req-shell", ".venv/bin/dbt run")] },
      { id: "a1", author: "assistant", status: "running", parts: [QUESTION_PART] },
    ];
    renderChat();

    await waitFor(() => expect(permissionCard()).toBeInTheDocument());
    expect(questionCard()).toBeNull();
    expect(composer()).toBeNull();
    // The ask lives in the dock and nowhere else: the transcript does not carry
    // a second copy of it.
    expect(screen.getAllByRole("region", { name: /run this command/i })).toHaveLength(1);
  });

  it("holds the question ask over the composer", async () => {
    state.turns = [USER, { id: "a1", author: "assistant", status: "running", parts: [QUESTION_PART] }];
    renderChat();

    await waitFor(() => expect(questionCard()).toBeInTheDocument());
    expect(composer()).toBeNull();
    expect(permissionCard()).toBeNull();
  });

  // A plan-mode chat refuses every write, so its source withholds the approval
  // on any permission ask. The plan approval is not one: it is what the reader
  // is waiting for, and a stance that reached it would leave a plan that can
  // only be rejected — in the one stance whose whole purpose is to produce one.
  it("keeps the plan approval answerable in a stance that approves nothing", async () => {
    ds.mayAllow = () => ({
      allowed: false,
      refusal: "Plan mode explores and proposes a plan, so it won't run this.",
    });
    state.turns = [USER, { id: "a1", author: "assistant", status: "running", parts: [PLAN_PART] }];
    renderChat();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /approve & start/i })).toBeInTheDocument(),
    );
    expect(screen.getByRole("radio", { name: /auto/i })).toBeInTheDocument();
    expect(composer()).toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: /auto/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /approve & start/i }));
    });

    // The wire's own label rides back, so the box switches the chat into auto.
    expect(ds.answerQuestion).toHaveBeenCalledWith("c1", "req-plan", [
      ["Accept — auto mode (run automatically, pause for risky steps)"],
    ]);
  });

  it("counts the queue when more than one permission is waiting", async () => {
    state.turns = [
      USER,
      {
        id: "a0",
        author: "assistant",
        status: "running",
        parts: [
          permissionPart("p0", "req-first", ".venv/bin/dbt run"),
          permissionPart("p1", "req-second", ".venv/bin/dbt test"),
        ],
      },
    ];
    renderChat();

    await waitFor(() => expect(permissionCard()).toBeInTheDocument());
    expect(screen.getByLabelText(/request 1 of 2/i)).toBeInTheDocument();
    // One ask at a time: the second subject is not on screen yet.
    expect(permissionCard()).toHaveTextContent(/dbt run/);
    expect(permissionCard()).not.toHaveTextContent(/dbt test/);
  });

  it("gives the composer back once the permission and the question are both settled", async () => {
    state.turns = [
      USER,
      { id: "a0", author: "assistant", status: "running", parts: [permissionPart("p0", "req-shell", ".venv/bin/dbt run")] },
      { id: "a1", author: "assistant", status: "running", parts: [QUESTION_PART] },
    ];
    renderChat();
    await waitFor(() => expect(permissionCard()).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: new RegExp(ALLOW.name, "i") }));
    });
    expect(ds.resolvePermission).toHaveBeenCalledWith("c1", "req-shell", ALLOW.optionId);

    // The permission clears and the question takes the dock — still no composer.
    await waitFor(() => expect(questionCard()).toBeInTheDocument());
    expect(permissionCard()).toBeNull();
    expect(composer()).toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByRole("radio", { name: /staging warehouse/i }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^submit$/i }));
    });

    await waitFor(() => expect(composer()).toBeInTheDocument());
    expect(questionCard()).toBeNull();
    expect(ds.answerQuestion).toHaveBeenCalledWith("c1", "req-question", [
      ["The staging warehouse"],
    ]);
  });
});
