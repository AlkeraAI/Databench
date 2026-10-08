// Answering a question ask — and a plan approval — is a write, and is treated
// as one, exactly as answering a permission ask already was.
//
// A 429 from the limiter, a 409 on an ask the transcript had already closed, or
// a dropped connection must never leave the card locked and escape to the
// page's unhandled-rejection channel. These cases pin what the two cards do:
// the refusal is said on the card, the keys stay live, and nothing reaches the
// page.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { Profiler } from "react";
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
const REQUEST = "req-q1";
const ASK = "Which warehouse should this model land in?";
const PICK = "The staging warehouse";

/** A real refusal as the transport builds one: the server's envelope read by
 *  the real error type, with `Retry-After` off real headers. Nothing here is a
 *  hand-shaped rejection that happens to match what the code looks for. */
function refusal(status: number, body: unknown, retryAfter?: string): ApiError {
  return new ApiError(
    status,
    body,
    `the request to /api/v1/chats/${CHAT}/answer failed`,
    new Headers(retryAfter === undefined ? {} : { "retry-after": retryAfter }),
  );
}

const THROTTLED = (): ApiError =>
  refusal(
    429,
    { detail: { code: "rate_limited", message: "You are answering very quickly." } },
    "3",
  );

function questionTurn(): ConversationTurn {
  return {
    id: "turn-1",
    author: "assistant",
    parts: [
      {
        kind: "question",
        id: "q1",
        requestId: REQUEST,
        questionKind: "question",
        questions: [
          {
            question: ASK,
            header: null,
            options: [{ label: PICK }, { label: "The production warehouse" }],
            multiple: false,
            custom: false,
          },
        ],
        status: "pending",
      },
    ],
  } as unknown as ConversationTurn;
}

function planTurn(): ConversationTurn {
  return {
    id: "turn-1",
    author: "assistant",
    parts: [
      {
        kind: "question",
        id: "q1",
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
            ],
            multiple: false,
            custom: false,
          },
        ],
        status: "pending",
      },
    ],
  } as unknown as ConversationTurn;
}

/** A source whose `answerQuestion` answers the nth attempt however the case
 *  wants, and records what was sent so a second attempt is visible. */
function sourceThat(
  turn: () => ConversationTurn,
  answer: (attempt: number) => Promise<void>,
): { source: ChatDataSource; sent: string[][][]; notes: (string | null | undefined)[] } {
  const sent: string[][][] = [];
  const notes: (string | null | undefined)[] = [];
  const impl = {
    caps: { opencodeActive: false, modelCatalog: false },
    getChatTurns: async () => [turn()],
    subscribeChat: () => () => undefined,
    listChats: async () => [{ id: CHAT, title: "A chat", updatedAt: "2026-09-19T12:00:00Z" }],
    answerQuestion: async (_chatId: string, _requestId: string, answers: string[][], note?: string | null) => {
      sent.push(answers);
      notes.push(note);
      return answer(sent.length);
    },
    resolvePermission: async () => undefined,
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
  return { source: impl as unknown as ChatDataSource, sent, notes };
}

/** What the reader could see at each commit: whether the card was saying a
 *  refusal, and whether its submit key was locked at that same moment. */
let commits: { refusal: boolean; locked: boolean }[] = [];

/** Read the DOM as each commit left it. `onRender` runs inside the commit, after
 *  the DOM is written and before any passive effect: a state an effect repairs
 *  one commit later is on record here, however fast the runner is. */
function recordCommit(): void {
  const submit = document.querySelector<HTMLButtonElement>("button.chat-question-primary");
  commits.push({
    refusal: document.querySelector(".chat-question-failure") !== null,
    locked: submit?.disabled ?? false,
  });
}

function mount() {
  const qc = createQueryClient({ retry: false });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/chat/${CHAT}`]}>
        <Profiler id="chat" onRender={recordCommit}>
          <ChatSurface chatId={CHAT} />
        </Profiler>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// What the page's own global handler would report. The browser raises it as a
// `window` event (`app/boot/globalHandlers.ts` listens for exactly this); under
// jsdom the same floated rejection surfaces on the node process instead, so
// both are watched and the assertion is on the pair.
const floated: unknown[] = [];
const onWindow = (event: PromiseRejectionEvent): void => {
  floated.push(event.reason);
};
const onProcess = (reason: unknown): void => {
  floated.push(reason);
};

/** Let every floated rejection be reported before asking whether one was. */
async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
  floated.length = 0;
  commits = [];
  window.addEventListener("unhandledrejection", onWindow);
  process.on("unhandledRejection", onProcess);
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("no network in this test"))));
});

afterEach(() => {
  window.removeEventListener("unhandledrejection", onWindow);
  process.off("unhandledRejection", onProcess);
  cleanup();
  resetChatRuntime();
  vi.unstubAllGlobals();
});

/** Pick the first option and submit the set. */
async function answerTheQuestion(): Promise<void> {
  await userEvent.click(await screen.findByRole("radio", { name: PICK }));
  await userEvent.click(screen.getByRole("button", { name: /^submit$/i }));
}

describe("answering a question ask", () => {
  it("says the limiter refused this attempt, keeps the keys, and reports nothing to the page", async () => {
    const throttled = sourceThat(questionTurn, (attempt) =>
      attempt === 1 ? Promise.reject(THROTTLED()) : Promise.resolve(),
    );
    installChatRuntime({ source: throttled.source, host: createBrowserChatHost() });
    mount();

    await answerTheQuestion();

    // Asked first, and on its own: the refusal must not reach the page's
    // unhandled-rejection channel whether or not the card goes on to say it.
    await flush();
    expect(floated).toEqual([]);
    expect(await screen.findByText("Answering too quickly. Try again in a moment.")).toBeTruthy();

    // The ask is still the reader's to settle: their pick is still made, and
    // the same answer goes out again on a second press.
    const submit = screen.getByRole("button", { name: /^submit$/i });
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    await waitFor(() => expect(throttled.sent).toEqual([[[PICK]], [[PICK]]]));
    await flush();
    expect(floated).toEqual([]);
  });

  it("says the reader may not answer, on a refusal that is not the limiter", async () => {
    const forbidden = sourceThat(questionTurn, () =>
      Promise.reject(refusal(403, { detail: { code: "forbidden", message: "no" } })),
    );
    installChatRuntime({ source: forbidden.source, host: createBrowserChatHost() });
    mount();

    await answerTheQuestion();

    expect(await screen.findByText("You are not allowed to answer this request.")).toBeTruthy();
    expect(screen.getByRole("button", { name: /^submit$/i })).toBeEnabled();
    // The sentence and the unlocked key arrive together: no commit ever told
    // the reader to answer again beside a key they could not press. Unlocking
    // in an effect left one such commit, and a loaded runner read the DOM there.
    expect(commits.some((commit) => commit.refusal)).toBe(true);
    expect(commits.filter((commit) => commit.refusal && commit.locked)).toEqual([]);
    await flush();
    expect(floated).toEqual([]);
  });

  it("says the ask is closed when the transcript already holds its answer", async () => {
    const already = sourceThat(questionTurn, () =>
      Promise.reject(
        refusal(409, {
          detail: { code: "ask_already_answered", message: "ask 'q1' has already been answered" },
        }),
      ),
    );
    installChatRuntime({ source: already.source, host: createBrowserChatHost() });
    mount();

    await answerTheQuestion();

    expect(await screen.findByText("This ask is no longer waiting for an answer.")).toBeTruthy();
    await flush();
    expect(floated).toEqual([]);
  });

  it("says the answer did not go when the request never reached the server", async () => {
    const dropped = sourceThat(questionTurn, () => Promise.reject(new TypeError("Failed to fetch")));
    installChatRuntime({ source: dropped.source, host: createBrowserChatHost() });
    mount();

    await answerTheQuestion();

    expect(await screen.findByText("The answer could not be sent.")).toBeTruthy();
    expect(screen.getByRole("button", { name: /^submit$/i })).toBeEnabled();
    expect(commits.filter((commit) => commit.refusal && commit.locked)).toEqual([]);
    await flush();
    expect(floated).toEqual([]);
  });
});

describe("approving a plan", () => {
  it("shows the answer it took at once, with nothing left to press twice, while the answer is in flight", async () => {
    // The relay has taken the answer but the machine has not echoed it yet.
    const inFlight = sourceThat(planTurn, () => new Promise<void>(() => undefined));
    installChatRuntime({ source: inFlight.source, host: createBrowserChatHost() });
    mount();

    await userEvent.click(await screen.findByRole("button", { name: /approve & start/i }));

    expect(await screen.findByText("Plan approved")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /approve & start/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /reject plan/i })).toBeNull();
    expect(screen.queryByRole("radio")).toBeNull();
    await userEvent.keyboard("{Enter}");
    await flush();
    expect(inFlight.sent).toEqual([[["Accept — run normally (ask before each change)"]]]);
  });

  it("says the limiter refused it, keeps the approval pressable, and reports nothing to the page", async () => {
    const throttled = sourceThat(planTurn, (attempt) =>
      attempt === 1 ? Promise.reject(THROTTLED()) : Promise.resolve(),
    );
    installChatRuntime({ source: throttled.source, host: createBrowserChatHost() });
    mount();

    const approve = await screen.findByRole("button", { name: /approve & start/i });
    await userEvent.click(approve);

    await flush();
    expect(floated).toEqual([]);
    expect(await screen.findByText("Answering too quickly. Try again in a moment.")).toBeTruthy();

    const again = screen.getByRole("button", { name: /approve & start/i });
    expect(again).toBeEnabled();
    await userEvent.click(again);
    await waitFor(() => expect(throttled.sent).toHaveLength(2));
    await flush();
    expect(floated).toEqual([]);
  });

  it("says a rejection the server refused did not land, and keeps the reject key", async () => {
    const forbidden = sourceThat(planTurn, () =>
      Promise.reject(refusal(403, { detail: { code: "forbidden", message: "no" } })),
    );
    installChatRuntime({ source: forbidden.source, host: createBrowserChatHost() });
    mount();

    await userEvent.click(await screen.findByRole("button", { name: /reject plan/i }));

    expect(await screen.findByText("You are not allowed to answer this request.")).toBeTruthy();
    expect(screen.getByRole("button", { name: /reject plan/i })).toBeEnabled();
    await flush();
    expect(floated).toEqual([]);
  });
});

describe("a note on a plan answer", () => {
  const APPROVE = "Accept — run normally (ask before each change)";

  it("goes with the approval, trimmed, and stands under the answered card", async () => {
    const taken = sourceThat(planTurn, () => Promise.resolve());
    installChatRuntime({ source: taken.source, host: createBrowserChatHost() });
    mount();

    const field = await screen.findByRole("textbox", { name: "Add a note for the model (optional)" });
    await userEvent.type(field, "  Skip the migration for now. ");
    await userEvent.click(screen.getByRole("button", { name: /approve & start/i }));

    await waitFor(() => expect(taken.sent).toEqual([[[APPROVE]]]));
    expect(taken.notes).toEqual(["Skip the migration for now."]);
    expect(await screen.findByText("Approved with a note: Skip the migration for now.")).toBeTruthy();
  });

  it("goes with the rejection as the reason the model gets", async () => {
    const taken = sourceThat(planTurn, () => Promise.resolve());
    installChatRuntime({ source: taken.source, host: createBrowserChatHost() });
    mount();

    await userEvent.type(await screen.findByRole("textbox", { name: /add a note/i }), "Split step two.");
    await userEvent.click(screen.getByRole("button", { name: /reject plan/i }));

    await waitFor(() => expect(taken.sent).toHaveLength(1));
    expect(taken.sent[0]?.[0]?.[0]).toMatch(/rejected this plan/);
    expect(taken.notes).toEqual(["Split step two."]);
    expect(await screen.findByText("Rejected: Split step two.")).toBeTruthy();
  });

  it("sends no note when the field is left blank", async () => {
    const taken = sourceThat(planTurn, () => Promise.resolve());
    installChatRuntime({ source: taken.source, host: createBrowserChatHost() });
    mount();

    await userEvent.type(await screen.findByRole("textbox", { name: /add a note/i }), "   ");
    await userEvent.click(screen.getByRole("button", { name: /approve & start/i }));

    await waitFor(() => expect(taken.sent).toHaveLength(1));
    expect(taken.notes).toEqual([undefined]);
    expect(screen.queryByText(/with a note/)).toBeNull();
  });

  it("reads back from the transcript, so a reload and everyone else in the chat see it", async () => {
    const answered = (): ConversationTurn => {
      const turn = planTurn();
      Object.assign(turn.parts[0] as object, {
        status: "answered",
        answers: [[APPROVE]],
        note: "Skip the migration for now.",
      });
      return turn;
    };
    installChatRuntime({ source: sourceThat(answered, () => Promise.resolve()).source, host: createBrowserChatHost() });
    mount();

    expect(await screen.findByText("Approved with a note: Skip the migration for now.")).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: /add a note/i })).toBeNull();
  });
});
