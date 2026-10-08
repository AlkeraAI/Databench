// A chat started from a template opens with the template's brief already said.
//
// The server posts it as the starter's own first message — the same `prompt`
// row anything they type becomes — so the agent starts on it and asks them what
// should differ this time. Because the words are unaltered, the only thing that
// tells a reader they did not type them is the record's provenance, and the one
// place that provenance can show is the bubble: a small caption naming the
// template. This walks the whole way the caption travels — the server's row,
// the fold, the entry, the rendered ticket — because a break anywhere in it
// leaves a reader looking at words they will not recognise as anyone's.

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ChatPanel } from "@alkera/ui";

import type { DocHandle } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import { transcriptEntries, type EntryContext } from "@/pages/workspace/chat/entries";

const NOW = "2026-09-21T12:00:00Z";
const CHAT = "c1";
const BRIEF = "Pull the weekly numbers.";

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

function idleDoc(): DocHandle<never> {
  return {
    onMessage: () => () => undefined,
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
    dispose: () => undefined,
  } as unknown as DocHandle<never>;
}

/** The `prompt` row the server writes, with whatever provenance it marked. */
function promptRow(text: string, metadata: Record<string, unknown>) {
  return {
    id: "row-1",
    chat_id: CHAT,
    seq: 1,
    role: "user" as const,
    kind: "prompt",
    event_id: "usr:template-brief-c1",
    payload: {
      schema_version: "1.2.0",
      kind: "prompt",
      text,
      client_id: "template-brief-c1",
      user_id: "u1",
      attachments: [],
      context: "",
      metadata,
    },
    created_at: NOW,
  };
}

/** What the server marks on a brief it opened the chat with. */
function briefProvenance(title: string, author = "Ada Lovelace") {
  return {
    source: "template_brief",
    template_node_id: "n1",
    template_object_id: "o1",
    template_title: title,
    template_author: author,
  };
}

function sourceOver(rows: Record<string, unknown>[]) {
  const listMessages = vi.fn();
  listMessages.mockResolvedValueOnce({ items: rows, next_after_seq: 999, resync_from: null });
  listMessages.mockResolvedValue({ items: [], next_after_seq: 999, resync_from: null });
  return new CloudDataSource({
    rest: { listMessages, postMessage: vi.fn() } as never,
    openDoc: () => idleDoc(),
    acquire: () => () => undefined,
    clientId: () => "web-1",
  });
}

async function bubbleFor(rows: Record<string, unknown>[]) {
  const turns = await sourceOver(rows).getChatTurns(CHAT);
  return { turns, entries: transcriptEntries(turns, ctx) };
}

describe("the first message of a chat started from a template", () => {
  it("renders as the reader's own bubble, captioned with the template it came from", async () => {
    const { entries } = await bubbleFor([promptRow(BRIEF, briefProvenance("Weekly numbers"))]);
    render(<ChatPanel entries={entries} />);

    // The brief is the message, unaltered: the caption is a second line beside
    // it, never words spliced into what the agent is asked.
    expect(screen.getByText(BRIEF)).toBeInTheDocument();
    expect(screen.getByText("From template \u201CWeekly numbers\u201D by Ada Lovelace")).toBeInTheDocument();
  });

  it("is the FIRST bubble, so the caption lands on the message that opened the chat", async () => {
    const later = {
      ...promptRow("and last quarter too", {}),
      id: "row-2",
      seq: 2,
      event_id: "usr:web-1",
    };
    const { entries } = await bubbleFor([
      promptRow(BRIEF, briefProvenance("Weekly numbers")),
      later,
    ]);

    expect(entries[0]?.item).toMatchObject({
      kind: "user",
      text: BRIEF,
      fromTemplate: "Weekly numbers",
      fromTemplateAuthor: "Ada Lovelace",
    });
    // The reader's own follow-up is not the template's, and must not inherit
    // the caption from the turn above it.
    expect(entries[1]?.item).toMatchObject({ kind: "user", text: "and last quarter too" });
    expect(entries[1]?.item).not.toHaveProperty("fromTemplate");
  });

  it("captions nothing when the message is an ordinary one", async () => {
    const { entries } = await bubbleFor([promptRow("what is 6*7?", {})]);
    render(<ChatPanel entries={entries} />);

    expect(screen.getByText("what is 6*7?")).toBeInTheDocument();
    expect(screen.queryByText(/^From template/)).toBeNull();
  });

  it("captions nothing when the metadata claims some other source", async () => {
    // A caption is a claim about where words came from, and this reader can
    // only make the one the server actually marks. Anything else — a future
    // source, a bag somebody else wrote — reads as an ordinary message rather
    // than as a template it is not from.
    const { entries } = await bubbleFor([
      promptRow(BRIEF, { source: "something_else", template_title: "Weekly numbers" }),
    ]);
    render(<ChatPanel entries={entries} />);

    expect(screen.queryByText(/^From template/)).toBeNull();
  });

  it("still names the bubble when the template had no title left to give", async () => {
    // The title is the template's own and can be empty; the provenance is the
    // point, so the bubble still says the words are not this reader's.
    const { entries } = await bubbleFor([promptRow(BRIEF, briefProvenance(""))]);
    render(<ChatPanel entries={entries} />);

    expect(screen.getByText("From template \u201Ca template\u201D by Ada Lovelace")).toBeInTheDocument();
  });

  it("names the AUTHOR, because a shared template's brief is somebody else's words", async () => {
    // The whole hazard this caption answers: on a shared template the first
    // prompt of your chat was written by another person. Saying only which
    // template it came from still leaves the words looking like your own.
    const { entries } = await bubbleFor([
      promptRow(BRIEF, briefProvenance("Weekly numbers", "Grace Hopper")),
    ]);
    render(<ChatPanel entries={entries} />);

    expect(screen.getByText(/by Grace Hopper$/)).toBeInTheDocument();
  });

  it("says the template alone when the author could not be resolved", async () => {
    // A deleted author is not a reason to drop the caption: where the words
    // came from is still the thing the reader needs.
    const { entries } = await bubbleFor([promptRow(BRIEF, briefProvenance("Weekly numbers", ""))]);
    render(<ChatPanel entries={entries} />);

    expect(screen.getByText("From template \u201CWeekly numbers\u201D")).toBeInTheDocument();
  });

  it("clamps author-written text so a caption cannot pose as the product's own copy", async () => {
    // Title and author are typed by whoever saved the template. React escapes
    // the markup; nothing stops the TEXT from opening like a system line, so
    // leading sentence punctuation is dropped and the reading is capped.
    const { entries } = await bubbleFor([
      promptRow(BRIEF, briefProvenance('— "System: ignore the above"', "> admin")),
    ]);
    const item = entries[0]?.item as { fromTemplate?: string; fromTemplateAuthor?: string };

    expect(item.fromTemplate?.startsWith("System: ignore")).toBe(true);
    expect(item.fromTemplateAuthor).toBe("admin");
  });

  it("caps a very long title rather than letting it run as a second message", async () => {
    const { entries } = await bubbleFor([promptRow(BRIEF, briefProvenance("W".repeat(400)))]);
    const item = entries[0]?.item as { fromTemplate?: string };

    expect(item.fromTemplate?.length).toBeLessThanOrEqual(60);
    expect(item.fromTemplate?.endsWith("\u2026")).toBe(true);
  });
});
