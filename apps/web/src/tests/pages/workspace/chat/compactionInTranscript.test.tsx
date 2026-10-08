// Compaction, as the reader experiences it.
//
// A live run against a real box (three chats, three folds, 10 s / 9 s / 85 s)
// showed the whole event rendered as one grey `/compact  Compaction` row: it
// read as a slash command the reader had typed, in a surface that refuses
// slash commands outright; nothing appeared while the fold ran; the turns the
// summary replaced stayed at full brightness; and the summary — which IS the
// agent's memory of everything above it — was nowhere on screen.
//
// The event sequence below is the one that run recorded, down to the
// `compacting` phase being overwritten by the next phase microseconds later.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ChatPanel } from "@alkera/ui";
import { transcriptEntries, type EntryContext } from "@/pages/workspace/chat/entries";
import {
  cloneTurns,
  conversationAwaitsResponse,
  conversationContextTokens,
  createConversationFoldState,
  foldHarnessEvent,
  type ConversationFoldState,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";

const SUMMARY = "## Goal\nRemember PELICAN-7741 holds 8312 pallets on lane ALPHA-9.";

const ENTRY_CTX: EntryContext = {
  onOpenUrl: () => {},
  onResourceOpen: () => {},
  onPlanOpen: () => {},
  onLineageOpen: () => {},
  onKnowledgeOpen: () => {},
  questionLive: null,
};

/** Two answered turns, ~72k of context carried — the state the live chats were
 *  in when the harness decided to fold. */
function seedChat(): ConversationFoldState {
  const state = createConversationFoldState();
  for (const [user, assistant, text] of [
    ["u1", "a1", "Remember these three facts."],
    ["u2", "a2", "Now emit 300 rows of CSV."],
  ] as const) {
    foldHarnessEvent(state, { event_type: "message.created", message_id: user, role: "user" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: user,
      part: { part_id: `${user}-text`, message_id: user, type: "text", text },
    });
    foldHarnessEvent(state, {
      event_type: "message.created",
      message_id: assistant,
      role: "assistant",
    });
    foldHarnessEvent(state, {
      event_type: "part.created",
      message_id: assistant,
      part: { part_id: `${assistant}-text`, message_id: assistant, type: "text", text: "Noted." },
    });
  }
  foldHarnessEvent(state, {
    event_type: "message.completed",
    message_id: "a2",
    time: "2026-09-19T20:30:40Z",
    tokens: { input: 60_000, cache_read: 11_854, output: 900 },
  });
  return state;
}

/** The continuation the agent goes on to write after a fold, and the usage it
 *  reports — the first sighting of what the conversation now carries. */
function continuationReports(state: ConversationFoldState, tokens: Record<string, number>): void {
  foldHarnessEvent(state, { event_type: "message.created", message_id: "a9", role: "assistant" });
  foldHarnessEvent(state, {
    event_type: "message.completed",
    message_id: "a9",
    time: "2026-09-19T20:32:20Z",
    tokens,
  });
}

/** The harness announces the fold with a phase it overwrites 70µs later. */
function foldStarts(state: ConversationFoldState, at = "2026-09-19T20:31:54.699Z"): void {
  foldHarnessEvent(state, {
    event_type: "session.status_changed",
    status: "running",
    phase: "compacting",
    time: at,
  });
  foldHarnessEvent(state, {
    event_type: "session.status_changed",
    status: "running",
    phase: "awaiting_llm",
    time: at,
  });
}

const APPLIED: HarnessEvent = {
  event_type: "compaction.applied",
  event_id: "27fb2c9a355b535954bf",
  summary_text: SUMMARY,
  summarised_message_ids: ["u1", "a1"],
};

function renderTranscript(state: ConversationFoldState, working = false) {
  const entries = transcriptEntries(cloneTurns(state), ENTRY_CTX);
  return render(
    <div className="chat-root">
      <ChatPanel entries={entries} working={working} />
    </div>,
  );
}

describe("compaction in the transcript", () => {
  it("opens a compaction card the moment the fold starts, and holds it there", () => {
    const state = seedChat();
    foldStarts(state);

    renderTranscript(state, true);

    // The reader sees that something is happening.
    expect(screen.getByText("Compacting the conversation…")).toBeInTheDocument();
    // And the phase is not overwritten by the next one.
    expect(screen.getByRole("status")).toHaveTextContent("Compacting the conversation…");
  });

  it("never renders a slash-command row for a compaction", () => {
    const state = seedChat();
    foldStarts(state);
    foldHarnessEvent(state, APPLIED);

    const { container } = renderTranscript(state);

    expect(container.textContent).not.toContain("/compact");
    expect(screen.queryByText("Compaction")).not.toBeInTheDocument();
  });

  it("settles into the counts the fold bought, once the next reply reports its usage", async () => {
    const state = seedChat();
    foldStarts(state);
    foldHarnessEvent(state, APPLIED);

    // Before the continuation reports usage the card states what it knows and
    // does not invent the other half.
    renderTranscript(state).unmount();

    continuationReports(state, { input: 20_000, cache_read: 1_005, output: 300 });
    renderTranscript(state);

    expect(screen.getByText("Conversation compacted")).toBeInTheDocument();
    expect(screen.getByText("2 turns summarised")).toBeInTheDocument();
    expect(screen.getByText("72k → 21k tokens")).toBeInTheDocument();

    // The summary is readable in place — it is the agent's memory of the turns
    // above it, and in the live run there was no way to reach it at all.
    expect(screen.queryByText(/8312 pallets/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show summary" }));
    expect(screen.getByText(/8312 pallets/)).toBeInTheDocument();
  });

  it("marks the turns the summary replaced, and leaves the rest alone", () => {
    const state = seedChat();
    foldStarts(state);
    foldHarnessEvent(state, APPLIED);

    const turns = cloneTurns(state);
    expect(turns.find((turn) => turn.id === "u1")?.clearedReason).toBe("summarised");
    expect(turns.find((turn) => turn.id === "a1")?.clearedReason).toBe("summarised");
    expect(turns.find((turn) => turn.id === "u2")?.cleared).toBeUndefined();

    const { container } = renderTranscript(state);
    const dimmed = container.querySelectorAll("[data-dim]");
    expect(dimmed.length).toBeGreaterThan(0);
    expect(within(dimmed[0] as HTMLElement).getByText("summarised")).toBeInTheDocument();
    // The retained tail is not dimmed with it.
    expect(container.querySelectorAll("[data-dim]").length).toBeLessThan(
      container.querySelectorAll(".chat-block").length,
    );
  });

  it("opens the summary's own page when the shell offers one", async () => {
    const state = seedChat();
    foldStarts(state);
    foldHarnessEvent(state, APPLIED);

    const opened: string[] = [];
    const entries = transcriptEntries(cloneTurns(state), {
      ...ENTRY_CTX,
      onCompactionOpen: (part) => opened.push(part.id),
    });
    render(
      <div className="chat-root">
        <ChatPanel entries={entries} />
      </div>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Open summary" }));
    // The durable id the summary arrived under — what the detail route resolves
    // against — not the placeholder the running card was opened with.
    expect(opened).toEqual(["27fb2c9a355b535954bf"]);
  });
});

describe("the working state around a compaction", () => {
  it("keeps the composer in its working state while the fold runs", () => {
    const state = seedChat();
    expect(conversationAwaitsResponse(cloneTurns(state))).toBe(false);
    foldStarts(state);
    // The card above the composer says the fold is running, and a running fold
    // is work on EVERY source — including one that publishes no turn state of
    // its own, where the transcript is the only authority there is.
    expect(conversationAwaitsResponse(cloneTurns(state))).toBe(true);
  });

  it("hands a settled fold back to the machine's word, wherever it landed", () => {
    // A settled summary is a terminal marker on a turn that never completes,
    // and it lands on a turn of its OWN whether the fold ran at rest or
    // interrupted one — so by its own reading the transcript owes nothing
    // either way. Reading that turn as live is how a manual /compact lit the
    // working indicator forever. Whether a turn is nonetheless still going —
    // an automatic compaction is followed by a re-send of the same prompt — is
    // the box's word, which the chat store holds the turn open on
    // (`machineWorking`, pinned in chatStore.busy.test.ts). The fold draws no
    // distinction here precisely so there is ONE authority for it.
    const atRest = seedChat();
    foldStarts(atRest);
    foldHarnessEvent(atRest, APPLIED);
    expect(conversationAwaitsResponse(cloneTurns(atRest))).toBe(false);

    const midTurn = seedChat();
    foldHarnessEvent(midTurn, { event_type: "message.created", message_id: "u3", role: "user" });
    foldHarnessEvent(midTurn, {
      event_type: "part.created",
      message_id: "u3",
      part: { part_id: "u3-text", message_id: "u3", type: "text", text: "More CSV." },
    });
    foldHarnessEvent(midTurn, {
      event_type: "message.created",
      message_id: "a3",
      role: "assistant",
    });
    foldStarts(midTurn);
    expect(conversationAwaitsResponse(cloneTurns(midTurn))).toBe(true);
    foldHarnessEvent(midTurn, APPLIED);
    expect(conversationAwaitsResponse(cloneTurns(midTurn))).toBe(false);
  });
});

describe("the context a turn reports", () => {
  it("reads the window off the newest turn's usage, not the reply's size", () => {
    const state = seedChat();
    // input + both cache halves — the figure the harness's own overflow check
    // measures. The reply's own output tokens are not part of what is carried.
    expect(conversationContextTokens(cloneTurns(state))).toBe(71_854);

    continuationReports(state, { input: 20_000, cache_read: 1_005, output: 4_000 });
    expect(conversationContextTokens(cloneTurns(state))).toBe(21_005);
  });

  it("has nothing to say before a turn reports usage", () => {
    expect(conversationContextTokens(cloneTurns(createConversationFoldState()))).toBeNull();
  });
});
