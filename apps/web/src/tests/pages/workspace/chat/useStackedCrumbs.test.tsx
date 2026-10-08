// The trail a stacked page hands to <Breadcrumbs>. The route walk itself is
// pinned in crumbTrail.test.ts; what this file pins is the hook around it — who
// each level is named after, and where a click lands.
//
// The name is the hard part. The chat list holds root chats only, so a subagent
// level is named by the spawn card in the chat that started it, and that card
// arrives on a transcript read that can settle long after first paint.

import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConversationTurn } from "@alkera/chat-model";

const { ds, turnsById, rootChats } = vi.hoisted(() => {
  const turnsById: Record<string, unknown[]> = {};
  const rootChats = [{ id: "a", title: "Data audit", updatedAt: "2026-06-10T00:00:00Z" }];
  const ds = {
    listChats: vi.fn(async () => rootChats),
    getChatTurns: vi.fn(async (id: string) => turnsById[id] ?? []),
  };
  return { ds, turnsById, rootChats };
});

vi.mock("@/pages/workspace/chat/data", () => ({
  chatData: () => ds,
  chatCaps: () => ({ opencodeActive: true }),
  chatHost: () => ({
    kind: "vscode",
    engine: { request: vi.fn(async () => ({})) },
    auth: { openBrowser: vi.fn(async () => {}) },
    subscribe: () => () => {},
    runCommand: vi.fn(async () => {}),
    openFile: vi.fn(async () => {}),
    workspacePath: () => null,
    account: () => ({ email: null, webAppUrl: null }),
    onAccountChange: () => () => {},
  }),
  errorText: (err: unknown) => String(err),
  exportBlob: async () => {},
  refetchWhileErrored: () => false as const,
  refetchWhileErroredOrEmpty: () => false as const,
  refetchWhileNoChatDefault: () => false as const,
}));

import { stackedTarget } from "@/pages/workspace/chat/controller"; // same-author-ok: mechanical import-path update for the controller/ split
import { useStackedCrumbs } from "@/pages/workspace/chat/crumbTrail";

/** The parent's spawn card — the only place a subagent chat is ever named. */
function spawnCard(childSessionId: string, description: string): ConversationTurn {
  return {
    id: `turn-${childSessionId}`,
    author: "assistant",
    parts: [
      {
        id: `part-${childSessionId}`,
        kind: "tool",
        callId: `call-${childSessionId}`,
        name: "spawn_agent",
        state: "completed",
        childSessionId,
        input: { agent: "explore", prompt: "Read every join in the warehouse", description },
      },
    ],
  };
}

function Probe({ current }: { current?: string }) {
  const crumbs = useStackedCrumbs(current);
  const location = useLocation();
  return (
    <>
      <p data-testid="here">{`${location.pathname}${location.search}`}</p>
      <ol>
        {crumbs.map((crumb, index) => (
          <li key={`${index}-${crumb.label}`}>
            {crumb.onGo ? <button onClick={crumb.onGo}>{crumb.label}</button> : crumb.label}
          </li>
        ))}
      </ol>
    </>
  );
}

function renderTrail(route: string, current?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <Probe current={current} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Every crumb, outermost first. */
const trail = (): string[] => screen.queryAllByRole("listitem").map((item) => item.textContent ?? "");
/** The crumbs that navigate. */
const clickable = (): string[] => screen.queryAllByRole("button").map((item) => item.textContent ?? "");
const here = (): string => screen.getByTestId("here").textContent ?? "";

const CHAT_A = "/chat/a";
const SUBAGENT_B = stackedTarget("/editor/chat/b", CHAT_A);

afterEach(() => {
  cleanup();
  for (const key of Object.keys(turnsById)) delete turnsById[key];
  vi.clearAllMocks();
  ds.getChatTurns.mockImplementation(async (id: string) => turnsById[id] ?? []);
  ds.listChats.mockImplementation(async () => rootChats);
});

describe("useStackedCrumbs", () => {
  it("names a subagent from its owner's spawn card, and only ancestors navigate", async () => {
    turnsById.a = [spawnCard("b", "Trace the joins")];
    renderTrail(SUBAGENT_B);

    await waitFor(() => expect(trail()).toEqual(["Data audit", "Trace the joins"]));
    // The level you are on is text; every level above it is a link back.
    expect(clickable()).toEqual(["Data audit"]);
    // "Chats" is a press away in the sidebar, so it never spends a slot here.
    expect(screen.queryByText("Chats")).toBeNull();
    expect(ds.getChatTurns).toHaveBeenCalledWith("a");
  });

  it("shows a root chat as one crumb and reads no transcript for it", async () => {
    renderTrail(CHAT_A);

    await waitFor(() => expect(trail()).toEqual(["Data audit"]));
    expect(clickable()).toEqual([]);
    expect(ds.getChatTurns).not.toHaveBeenCalled();
  });

  it("clicking an ancestor navigates to that level with its own trail intact", async () => {
    turnsById.a = [spawnCard("b", "Trace the joins")];
    renderTrail(stackedTarget("/editor/blobs/b", SUBAGENT_B), "Row counts");

    // `current` names the last level, over the "Results" the route implies.
    await waitFor(() => expect(trail()[0]).toBe("Data audit"));
    expect(trail()).toHaveLength(3);
    expect(trail()[2]).toBe("Row counts");
    expect(clickable()).toHaveLength(2);

    // Picked by position, so what the middle level is CALLED stays the other
    // tests' business. Its target still carries the chat it was opened from, so
    // the trail survives the jump instead of collapsing to a bare page. Only the
    // clickable levels are read past here — a real page hands the hook its own
    // `current`, and the probe keeps the one it was given.
    act(() => screen.getAllByRole("button")[1].click());
    expect(here()).toBe(SUBAGENT_B);
    await waitFor(() => expect(clickable()).toEqual(["Data audit"]));

    act(() => screen.getByRole("button", { name: "Data audit" }).click());
    expect(here()).toBe(CHAT_A);
    await waitFor(() => expect(clickable()).toEqual([]));
  });

  it("names the subagent when the owner's transcript resolves after first paint", async () => {
    let deliver!: (turns: ConversationTurn[]) => void;
    ds.getChatTurns.mockReturnValue(new Promise((resolve) => {
      deliver = resolve as (turns: ConversationTurn[]) => void;
    }));
    renderTrail(SUBAGENT_B);

    await waitFor(() => expect(trail()[0]).toBe("Data audit"));
    expect(trail()).not.toContain("Trace the joins");

    await act(async () => {
      deliver([spawnCard("b", "Trace the joins")]);
    });
    await waitFor(() => expect(trail()).toEqual(["Data audit", "Trace the joins"]));
  });

  // Opening Results from a stacked subagent chat, which is what the chrome's
  // quick link does there. The hook reads the transcript of whichever chat sits
  // one level up, so at this depth it reads the subagent's own transcript and
  // the spawn card that names it is never fetched.
  it("names a subagent that a drill-in has pushed off the end of the trail", async () => {
    turnsById.a = [spawnCard("b", "Trace the joins")];
    renderTrail(stackedTarget("/editor/blobs/b", SUBAGENT_B), "Results");

    await waitFor(() => expect(trail()).toEqual(["Data audit", "Trace the joins", "Results"]));
  });

  // A deep link arrives with no `backTo` at all — a reopened editor tab, a
  // pasted route. The page still belongs to a chat, and the trail says which.
  it("puts a deep-linked page under the chat it belongs to", async () => {
    renderTrail("/editor/blobs/a");

    await waitFor(() => expect(trail()).toEqual(["Data audit", "Results"]));
    expect(clickable()).toEqual(["Data audit"]);

    act(() => screen.getByRole("button", { name: "Data audit" }).click());
    expect(here()).toBe(CHAT_A);
  });

  // The spawn card is the only place a subagent is ever named, so a transcript
  // that does not carry one leaves the level nameless. It stays a level: losing
  // the crumb would strand the reader with no way back up.
  it("keeps a subagent's level and its way back when nothing names it", async () => {
    turnsById.a = [];
    renderTrail(SUBAGENT_B);

    await waitFor(() => expect(ds.getChatTurns).toHaveBeenCalledWith("a"));
    expect(trail()).toHaveLength(2);
    expect(clickable()).toEqual(["Data audit"]);

    act(() => screen.getByRole("button", { name: "Data audit" }).click());
    expect(here()).toBe(CHAT_A);
  });

  it("shows the stack's depth before the chat list answers, then fills the names in", async () => {
    turnsById.a = [spawnCard("b", "Trace the joins")];
    let deliver!: (chats: typeof rootChats) => void;
    ds.listChats.mockReturnValue(
      new Promise<typeof rootChats>((resolve) => {
        deliver = resolve;
      }),
    );
    renderTrail(SUBAGENT_B);

    // Both levels are already there, and the last one still does not navigate.
    expect(trail()).toHaveLength(2);
    expect(clickable()).toHaveLength(1);
    expect(trail()).not.toContain("Data audit");
    // The owner's transcript is only worth reading once the list has said which
    // levels it cannot name.
    expect(ds.getChatTurns).not.toHaveBeenCalled();

    await act(async () => {
      deliver(rootChats);
    });
    await waitFor(() => expect(trail()).toEqual(["Data audit", "Trace the joins"]));
  });
});
