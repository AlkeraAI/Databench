// The turn watchdog on the shipping surface. A turn this webview started that
// will never finish has to say so instead of spinning forever -- and it re-arms
// on re-entry, retires itself the moment real activity comes back, and never
// fires while the daemon is still emitting.
//
// The judgement is about LIVENESS, not silence: a machine that has gone away
// loses the turn at once and says so in the workspace's words, a machine that
// says it is still working keeps the turn however quiet it is, and the bare
// silence floor sits past the machine's own wall-clock budget for a turn.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../../api/queryClient";

const { ds, state, subscribers } = vi.hoisted(() => {
  // The real subscribeChat always delivers a ChatEvent and the store reads
  // `event.replay`, so a test fires an event, never a bare call.
  const subscribers: Array<(event: { replay?: boolean }) => void> = [];
  // `turnState` is the MACHINE's word on the turn, which the cloud source hears
  // on the chat document's meta lane; null is "nobody has said", the editor's
  // answer as well as a document that has not spoken yet.
  const state = {
    turns: [] as unknown[],
    turnState: null as "working" | "idle" | null,
    // Which surface is reading. The browser and the editor hear the same
    // silence in different words, so the host is part of the fixture.
    host: "vscode" as "vscode" | "browser",
  };
  const ds = {
    turnState: () => state.turnState,
    listChats: async () => [{ id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" }],
    listModels: async () => [] as unknown[],
    listCommands: async () => [] as unknown[],
    resolveChatDefaults: async () => ({ model: null, effort: null }),
    listContext: async () => ({ total: 0, items: [] }),
    lineageRoots: async () => ({ total_nodes: 0, nodes: [] }),
    getChatTurns: async () => state.turns,
    searchFiles: async () => [] as unknown[],
    sendUserMessage: async () => ({ id: "m1", role: "user", content: "Hello" }),
    createChat: async () => ({ id: "c1", title: "Chat", updatedAt: "2026-06-10T00:00:00Z" }),
    getPermissionMode: async () => "default",
    subscribePermissionMode: () => () => {},
    subscribeChat: (_id: string, cb: (event: { replay?: boolean }) => void) => {
      subscribers.push(cb);
      return () => {
        const at = subscribers.indexOf(cb);
        if (at >= 0) subscribers.splice(at, 1);
      };
    },
  };
  return { ds, state, subscribers };
});

vi.mock("./data", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./data")>()),
  chatHost: () => ({
    kind: state.host,
    engine: { request: async () => ({}) },
    runCommand: async () => {},
    openFile: async () => {},
    subscribe: () => () => {},
    workspacePath: () => null,
    account: () => ({ email: null, webAppUrl: null }),
    onAccountChange: () => () => {},
    auth: { openBrowser: async () => {}, getState: () => undefined, setState: () => {}, on: () => () => {}, ready: () => {} },
  }),
  chatData: () => ds,
  refetchWhileErrored: () => false as const,
  refetchWhileNoChatDefault: () => false as const,
  refetchWhileErroredOrEmpty: () => false as const,
  chatCaps: () => ({ opencodeActive: false }),
}));

import { ChatSurface } from "./ChatSurface";
import { useChatStore } from "./chatStore";
import { STALL_TURN, WORKSPACE_LOST_TURN, WORKSPACE_LOST_TURN_QUIET } from "./controller";
import type { ConversationTurn } from "@alkera/chat-model";

/** What a stalled turn reads as, taken from the turn the controller appends. */
function lineOf(turn: ConversationTurn): string {
  const part = turn.parts[0];
  if (part.kind !== "system") throw new Error("a stalled turn no longer reads as a system line");
  return part.text;
}

function stallLine(): string {
  return lineOf(STALL_TURN);
}

function workspaceLostLine(): string {
  return lineOf(WORKSPACE_LOST_TURN);
}

/** The transcript a chat is left showing when its turn is still in flight. */
const IN_FLIGHT = [
  { id: "u1", author: "user", status: "done", parts: [{ id: "u1-t", kind: "text", text: "Hello" }] },
  { id: "a1", author: "assistant", status: "done", parts: [] },
];

function renderChat({ unavailable = false }: { unavailable?: boolean } = {}) {
  const qc = createQueryClient({ retry: false });
  const tree = (busy: boolean) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <Routes>
          <Route path="/chat/:id" element={<ChatSurface unavailable={busy} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(tree(unavailable));
  return {
    ...view,
    /** The shell's machine read changing under a turn, as the 15 s poll does. */
    setUnavailable: (next: boolean) => view.rerender(tree(next)),
  };
}

// findBy* polls through the faked clock and would hang -- flush by hand and
// read with getBy*/queryBy*.
const flush = async (): Promise<void> => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
};

const stall = () => screen.queryByText(stallLine());
const workspaceLost = () => screen.queryByText(workspaceLostLine());
const working = () => screen.queryByText(/^working/i);

/** Past the silence floor. The floor is deliberately longer than the machine's
 *  own wall-clock budget for a turn, so this is minutes, not seconds. */
const PAST_THE_FLOOR = 305_000;

async function sendHello(): Promise<void> {
  const box = screen.getByRole("textbox", { name: /^message /i });
  await act(async () => {
    fireEvent.change(box, { target: { value: "Hello" } });
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await vi.advanceTimersByTimeAsync(0);
  });
}

const wait = async (ms: number): Promise<void> => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

beforeEach(() => {
  subscribers.length = 0;
  state.turns = [];
  state.turnState = null;
  state.host = "vscode";
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  state.turns = [];
  state.turnState = null;
  useChatStore.setState({ byId: {}, composerPrefs: {} });
});

describe("ChatSurface turn watchdog", () => {
  it("says the agent stopped responding once nobody has said anything at all", async () => {
    renderChat();
    await flush();
    await sendHello();

    // Nothing ever arrives on the chat subscription, so the turn's clock never
    // refreshes.
    expect(stall()).toBeNull();
    await wait(PAST_THE_FLOOR);

    expect(stall()).toBeInTheDocument();
    // The turn is over: the working display goes with it.
    expect(working()).toBeNull();
  });

  it("never calls a cloud turn dead for being quiet, however long it runs", async () => {
    // A cloud turn runs on a box that may spend hours inside one tool call,
    // and nothing the browser can measure says whether it ended. The machine's
    // status is told in the banner above the transcript; cancelling the turn
    // under it frees the composer, so the next question goes out under a turn
    // that is still running and the real answer lands beneath it.
    state.host = "browser";
    renderChat();
    await flush();
    await sendHello();

    await wait(20 * 60_000);

    expect(screen.queryByText(lineOf(WORKSPACE_LOST_TURN_QUIET))).toBeNull();
    expect(stall()).toBeNull();
    expect(workspaceLost()).toBeNull();
    expect(working()).toBeInTheDocument();
  });

  it("does not call a turn dead at a minute — the box's own budget is longer", async () => {
    renderChat();
    await flush();
    await sendHello();

    // The measured shape of the defect: a single model step on a small box
    // emitting nothing for well over a minute.
    await wait(90_000);

    expect(stall()).toBeNull();
    expect(workspaceLost()).toBeNull();
    expect(working()).toBeInTheDocument();
  });

  it("re-arms on re-entering a chat whose turn is still in flight", async () => {
    state.turns = IN_FLIGHT;
    const first = renderChat();
    await flush();
    await sendHello();
    first.unmount();

    // Back in the same chat: the turn still reads as running, and a daemon that
    // died while the reader was away still has to stall.
    renderChat();
    await flush();
    expect(working()).toBeInTheDocument();

    await wait(PAST_THE_FLOOR);
    expect(stall()).toBeInTheDocument();
  });

  it("retires the stall the moment the daemon speaks again", async () => {
    renderChat();
    await flush();
    await sendHello();
    await wait(PAST_THE_FLOOR);
    expect(stall()).toBeInTheDocument();

    // The daemon recovers and a live event folds a fresh transcript in. The
    // notice has to clear on its own, not wait for a send or a chat switch.
    state.turns = [
      { id: "u1", author: "user", status: "done", parts: [{ id: "u1-t", kind: "text", text: "Hello" }] },
      {
        id: "a1",
        author: "assistant",
        status: "done",
        completedAt: "2026-06-10T00:01:00Z",
        parts: [{ id: "a1-t", kind: "text", text: "Recovered reply" }],
      },
    ];
    await act(async () => {
      subscribers.forEach((cb) => cb({ replay: false }));
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(stall()).toBeNull();
    expect(screen.getByText("Recovered reply")).toBeInTheDocument();
  });

  it("holds off while the daemon keeps emitting", async () => {
    renderChat();
    await flush();
    await sendHello();

    // A beat every 30s across more than a minute: each one refreshes the clock.
    for (let i = 0; i < 4; i += 1) {
      await wait(30_000);
      await act(async () => {
        subscribers.forEach((cb) => cb({ replay: false }));
        await vi.advanceTimersByTimeAsync(0);
      });
    }

    expect(stall()).toBeNull();
  });
});

describe("a turn the machine says it is still working on", () => {
  it("is not declared dead after 90 seconds of silence", async () => {
    renderChat();
    await flush();
    await sendHello();
    // The machine says so once the turn it was handed is under way. Said
    // before anything was sent, the word alone opens the turn (a reopened
    // chat mid-turn), and there would be no Send to press.
    state.turnState = "working";

    await wait(90_000);

    expect(stall()).toBeNull();
    expect(workspaceLost()).toBeNull();
    // Still the reader's turn in flight: the composer is not freed, so the next
    // question cannot be sent for the answer to land underneath.
    expect(working()).toBeInTheDocument();
  });

  it("survives past the silence floor and keeps the answer under its own prompt", async () => {
    renderChat();
    await flush();
    await sendHello();
    // The machine says so once the turn it was handed is under way. Said
    // before anything was sent, the word alone opens the turn (a reopened
    // chat mid-turn), and there would be no Send to press.
    state.turnState = "working";

    // Far past the floor: the machine's word, not the clock, is what decides.
    await wait(PAST_THE_FLOOR * 2);
    expect(stall()).toBeNull();
    expect(working()).toBeInTheDocument();

    // The answer finally lands. It reaches a transcript that never showed a
    // crash notice and never took a second prompt.
    state.turns = [
      { id: "u1", author: "user", status: "done", parts: [{ id: "u1-t", kind: "text", text: "Hello" }] },
      {
        id: "a1",
        author: "assistant",
        status: "done",
        completedAt: "2026-06-10T00:06:00Z",
        parts: [{ id: "a1-t", kind: "text", text: "The late answer" }],
      },
    ];
    state.turnState = "idle";
    await act(async () => {
      subscribers.forEach((cb) => cb({ replay: false }));
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(stall()).toBeNull();
    expect(screen.getByText("The late answer")).toBeInTheDocument();
    expect(screen.getAllByText("Hello")).toHaveLength(1);
  });

  it("outlives a machine the shell cannot reach, and names it when the silence finally runs out", async () => {
    const view = renderChat();
    await flush();
    await sendHello();
    await wait(30_000);
    expect(workspaceLost()).toBeNull();

    // The 15 s machine poll comes back "unreachable" under the turn. Four
    // missed heartbeats is 60 seconds of quiet, not a dead turn — the box may
    // be busy — so the reader keeps their answer and the banner does the
    // telling. Reading it as a verdict is what cancelled a running turn.
    view.setUnavailable(true);
    await wait(6_000);

    expect(workspaceLost()).toBeNull();
    expect(working()).toBeInTheDocument();

    // Only the silence floor ends it, and then in the workspace's words: the
    // reader owns no backend and no gateway; the box they do own went quiet.
    await wait(PAST_THE_FLOOR);

    expect(workspaceLost()).toBeInTheDocument();
    expect(stall()).toBeNull();
    expect(working()).toBeNull();
  });
});
