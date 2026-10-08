// What the reader actually sees when the workspace refuses to run something.
//
// The daemon writes one sentence with the statement it would not run quoted
// beside it. This drives the whole browser half — the daemon's own events, the
// fold, the transcript mapping, the panel — and asserts two things: the words
// are on screen, and the quoted statement is inert. Their rows are stored LLM
// output, so a statement that arrives shaped like a markdown link is a real
// input, and it must render as characters, never as something to click.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ChatPanel } from "@alkera/ui";

import {
  cloneTurns,
  createConversationFoldState,
  foldHarnessEvent,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import { transcriptEntries } from "@/pages/workspace/chat/entries";

const NOOP = () => {};

/** The three events the daemon publishes for one refusal, folded and mapped
 *  exactly as the chat page does. */
function renderRefusal(note: string) {
  const state = createConversationFoldState();
  for (const event of [
    { event_type: "message.created", message_id: "note-1", role: "system" },
    {
      event_type: "part.created",
      message_id: "note-1",
      part: {
        part_id: "note-1-part",
        message_id: "note-1",
        type: "text",
        text: note,
        synthetic: true,
      },
    },
    { event_type: "message.completed", message_id: "note-1", finish_reason: "stop" },
  ] satisfies HarnessEvent[]) {
    foldHarnessEvent(state, event);
  }
  const entries = transcriptEntries(cloneTurns(state), {
    onOpenUrl: NOOP,
    onLinkClick: NOOP,
    onResourceOpen: NOOP,
    onSubagentOpen: NOOP,
    onPlanOpen: NOOP,
    onLineageOpen: NOOP,
    onKnowledgeOpen: NOOP,
    questionLive: null,
  });
  render(<ChatPanel entries={entries} />);
  return entries;
}

describe("a refusal in the browser", () => {
  it("shows the sentence and the statement it would not run", () => {
    renderRefusal(
      'This connection is read-only, so statements that modify data are refused.\n"DELETE FROM orders WHERE id = 7"',
    );
    expect(
      screen.getByText("This connection is read-only, so statements that modify data are refused."),
    ).toBeTruthy();
    expect(screen.getByText('"DELETE FROM orders WHERE id = 7"')).toBeTruthy();
  });

  it("shows the unprovable-statement sentence for SQL it could not parse", () => {
    renderRefusal(
      'I couldn\'t prove that query is read-only, so I won\'t run it.\n"just wipe the orders table"',
    );
    expect(
      screen.getByText("I couldn't prove that query is read-only, so I won't run it."),
    ).toBeTruthy();
    expect(screen.getByText('"just wipe the orders table"')).toBeTruthy();
  });

  it("reads as a notice, not as an error", () => {
    const entries = renderRefusal(
      'This connection is read-only, so statements that modify data are refused.\n"UPDATE t SET x = 1"',
    );
    expect(entries).toHaveLength(1);
    expect(entries[0].item.kind).toBe("notice");
    expect(entries[0].status).toBe("settled");
    if (entries[0].item.kind === "notice") expect(entries[0].item.level).toBe("neutral");
  });

  it("renders a link-shaped statement as characters, never as a link", () => {
    renderRefusal(
      'I couldn\'t prove that query is read-only, so I won\'t run it.\n"[orders](https://evil.example/steal) -- \'; DROP TABLE t; --"',
    );
    expect(
      screen.getByText(
        '"[orders](https://evil.example/steal) -- \'; DROP TABLE t; --"',
      ),
    ).toBeTruthy();
    expect(document.querySelectorAll("a")).toHaveLength(0);
    expect(document.body.innerHTML).not.toContain("evil.example/steal\"");
  });

  it("shows one line, not an empty one under it, when nothing is quoted", () => {
    // A refusal the machine writes with no statement beside it — a shell
    // command it could not read, a write whose target it never learned — is
    // one sentence, with no empty body paragraph under it.
    renderRefusal("This workspace is read-only; I won't run shell commands.");
    const notice = screen.getByText(
      "This workspace is read-only; I won't run shell commands.",
    ).parentElement;
    expect(notice).not.toBeNull();
    expect(notice?.querySelectorAll("p")).toHaveLength(1);
  });

  it("still shows the quoted statement when there is one", () => {
    // The other side of the same guard: an empty body is dropped, a real one
    // is not.
    renderRefusal(
      'This workspace is read-only; I won\'t run shell commands.\n"ls -la"',
    );
    const notice = screen.getByText(
      "This workspace is read-only; I won't run shell commands.",
    ).parentElement;
    expect(notice?.querySelectorAll("p")).toHaveLength(2);
    expect(screen.getByText('"ls -la"')).toBeTruthy();
  });

  it("names no surface the reader does not have", () => {
    renderRefusal(
      'This connection is read-only, so statements that modify data are refused.\n"DROP TABLE orders"',
    );
    const shown = document.body.textContent?.toLowerCase() ?? "";
    for (const word of ["vs code", "vscode", "terminal", "preferences", "settings"]) {
      expect(shown).not.toContain(word);
    }
  });
});
