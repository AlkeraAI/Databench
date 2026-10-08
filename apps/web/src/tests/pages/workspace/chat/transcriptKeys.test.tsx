// Every transcript entry the tape renders must have its own key.
//
// React's warning for a repeated key ends "may cause children to be duplicated
// and/or omitted" — on a chat transcript, that is a row of the conversation
// silently vanishing. The tape keys on the entry id, which is the folded part's
// id, so the guarantee has to hold in the fold and again where the entries are
// built.

import type { ReactElement } from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ChatPanel } from "@alkera/ui";

import {
  createConversationFoldState,
  foldHarnessEvent,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import { transcriptEntries, type EntryContext } from "@/pages/workspace/chat/entries";

const REAL_OPENCODE_EVENTS = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../vscode-extension/src/engine/__fixtures__/opencode-turn.json"),
    "utf8",
  ),
) as HarnessEvent[];

/** A real machine answering a real question, recorded off the chat document. */
const RECORDED_CLOUD_TURN = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../cli/tests/cloud/fixtures/recorded_turn_0f0bec47.json"),
    "utf8",
  ),
) as HarnessEvent[];

/** Two `sql.query` calls in one turn under the ids a publisher gave them — the
 *  shape of a multi-query answer, which is most of the interesting ones. */
function twoQueries(ids: string[]): HarnessEvent[] {
  const out: HarnessEvent[] = [
    { event_type: "message.created", message_id: "a1", role: "assistant" },
  ];
  for (const [index, id] of ids.entries()) {
    out.push({
      event_type: "tool.call",
      message_id: "a1",
      tool_call_id: id,
      tool_name: "sql.query",
      input: { sql: `select ${index}` },
      status: "running",
    });
    out.push({
      event_type: "tool.call_update",
      tool_call_id: id,
      status: "completed",
      output: { columns: ["n"], preview_rows: [[index]], row_count: 1 },
    });
  }
  return out;
}

/** Everything the render wrote to `console.error`, which is where React puts
 *  its duplicate-key warning. */
function renderErrors(node: ReactElement): string[] {
  const errors: unknown[][] = [];
  const spy = vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    errors.push(args);
  });
  try {
    render(node);
  } finally {
    spy.mockRestore();
  }
  return errors.map((args) => args.map(String).join(" "));
}

const ctx: EntryContext = {
  onLinkClick: vi.fn(),
  onResourceOpen: vi.fn(),
  onOpenUrl: vi.fn(),
  onSubagentOpen: vi.fn(),
  onLineageOpen: vi.fn(),
  onKnowledgeOpen: vi.fn(),
  onPlanOpen: vi.fn(),
  questionLive: null,
};

function idsOf(events: HarnessEvent[]): string[] {
  const state = createConversationFoldState();
  for (const event of events) foldHarnessEvent(state, event);
  return transcriptEntries(state.turns, ctx).map((entry) => entry.id);
}

function duplicates(ids: string[]): string[] {
  const seen = new Set<string>();
  return ids.filter((id) => (seen.has(id) ? true : (seen.add(id), false)));
}

/** The machine's own transcript note — a refusal, a promote's outcome — as it
 *  reaches the browser: a `system` message with one synthetic text part. */
function noteEvents(noteId: string, text: string): HarnessEvent[] {
  return [
    { event_type: "message.created", message_id: noteId, role: "system" },
    {
      event_type: "part.created",
      message_id: noteId,
      part: { part_id: `${noteId}-part`, message_id: noteId, type: "text", text, synthetic: true },
    },
  ];
}

describe("transcript entry keys", () => {
  it("are unique across a recorded opencode turn", () => {
    expect(duplicates(idsOf(REAL_OPENCODE_EVENTS))).toEqual([]);
  });

  it("stay unique when a note's part is seen before its system message", () => {
    // The realtime document keeps a retained window: a reconnect can deliver a
    // note's PART with its `message.created` already evicted, so the part folds
    // as ordinary assistant prose. The REST catch-up that follows replays BOTH,
    // and the note must become one system row — never a second part under the
    // same id in a second turn.
    const [created, part] = noteEvents("note-1", "Could not promote: no tool result with that id.");
    const ids = idsOf([part, created, part]);

    expect(duplicates(ids)).toEqual([]);
    expect(ids.filter((id) => id === "note-1-part")).toHaveLength(1);
  });

  it("stay unique when a note is replayed whole", () => {
    const note = noteEvents("note-2", "Saved 120 rows to result 8f1.");
    expect(duplicates(idsOf([...note, ...note]))).toEqual([]);
  });

  it("gives a repeated part id its own row rather than dropping one", () => {
    // Whatever a publisher sends, two entries must never share a key: React's
    // answer to that is to duplicate or omit one, and an omitted transcript row
    // is a conversation with a hole in it.
    const turns = [
      {
        id: "t1",
        author: "assistant" as const,
        parts: [
          { id: "p-dup", kind: "text" as const, text: "first" },
          { id: "p-dup", kind: "text" as const, text: "second" },
        ],
      },
    ];
    const entries = transcriptEntries(turns, ctx);
    expect(entries).toHaveLength(2);
    expect(duplicates(entries.map((entry) => entry.id))).toEqual([]);
  });
});

describe("the transcript renders a recorded turn without a React key warning", () => {
  it("mounts the recorded opencode turn cleanly", () => {
    const errors: unknown[][] = [];
    const spy = vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
      errors.push(args);
    });
    try {
      const state = createConversationFoldState();
      for (const event of REAL_OPENCODE_EVENTS) foldHarnessEvent(state, event);
      render(
        <ChatPanel entries={transcriptEntries(state.turns, ctx)} />,
      );
    } finally {
      spy.mockRestore();
    }
    expect(errors.map((args) => String(args[0]))).toEqual([]);
  });

  it("mounts the recorded cloud turn cleanly", () => {
    const state = createConversationFoldState({ agentHost: "workspace" });
    for (const event of RECORDED_CLOUD_TURN) foldHarnessEvent(state, event);
    expect(renderErrors(<ChatPanel entries={transcriptEntries(state.turns, ctx)} />)).toEqual([]);
  });

  it("keeps both queries on the card when the publisher gave them no call id", () => {
    // `Encountered two children with the same key, ""`: an activity card keys
    // its steps by the tool call id, and an id field that is PRESENT but empty
    // sails past every `?? fallback`, so two queries in one answer would render
    // under the same key, and React's remedy for that is to drop one.
    const state = createConversationFoldState();
    for (const event of twoQueries(["", ""])) foldHarnessEvent(state, event);
    const entries = transcriptEntries(state.turns, ctx);

    const steps = (entries[0].item as { steps: { id: string }[] }).steps;
    expect(steps).toHaveLength(2);
    expect(duplicates(steps.map((step) => step.id))).toEqual([]);
    expect(steps.some((step) => step.id === "")).toBe(false);
    expect(renderErrors(<ChatPanel entries={entries} />)).toEqual([]);
  });

  it("keeps both queries on the card when the publisher REPEATED a call id", () => {
    const state = createConversationFoldState();
    for (const event of twoQueries(["call-1", "call-1"])) foldHarnessEvent(state, event);
    const entries = transcriptEntries(state.turns, ctx);

    const steps = (entries[0].item as { steps: { id: string }[] }).steps;
    expect(duplicates(steps.map((step) => step.id))).toEqual([]);
    expect(renderErrors(<ChatPanel entries={entries} />)).toEqual([]);
  });

  it("renders a result whose columns repeat a name without one going missing", () => {
    // Two aggregates aliased the same is legal SQL, so the grid cannot key its
    // headers by the column NAME.
    const errors: unknown[][] = [];
    const spy = vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
      errors.push(args);
    });
    try {
      const state = createConversationFoldState();
      foldHarnessEvent(state, { event_type: "message.created", message_id: "a1", role: "assistant" });
      foldHarnessEvent(state, {
        event_type: "tool.call",
        message_id: "a1",
        tool_call_id: "prt_1",
        tool_name: "sql.query",
        input: { sql: "select count() as n, count() as n from citations" },
        status: "running",
      });
      foldHarnessEvent(state, {
        event_type: "tool.call_update",
        tool_call_id: "prt_1",
        status: "completed",
        output: { columns: ["n", "n"], preview_rows: [[1, 2]], row_count: 1 },
      });
      render(
        <ChatPanel entries={transcriptEntries(state.turns, ctx)} />,
      );
    } finally {
      spy.mockRestore();
    }
    expect(errors.map((args) => String(args[0]))).toEqual([]);
    expect(screen.getAllByRole("columnheader", { name: "n" })).toHaveLength(2);
  });
});
