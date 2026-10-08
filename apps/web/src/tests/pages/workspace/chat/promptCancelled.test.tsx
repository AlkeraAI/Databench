// A message a Stop dropped, from the box's event to what the reader sees.
//
// The box reports the drop by the transcript id the reader's copy already
// carries (`usr:<client_id>`), the fold stamps the SERVER's state onto the turn
// already holding those words, and the tape renders one line under them plus
// the control that sends them again. The same tape backs the browser portal and
// the editor's webview, so what is asserted here is what both show.

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ChatPanel } from "@alkera/ui";

import {
  cloneTurns,
  createConversationFoldState,
  foldHarnessEvent,
  foldRelayedPrompt,
  type ConversationFoldState,
} from "@/pages/workspace/chat/data/harnessEventFold";
import { transcriptEntries, type EntryContext } from "@/pages/workspace/chat/entries";

const SAID = "Deploy the staging stack";

function baseCtx(): EntryContext {
  return {
    onLinkClick: vi.fn(),
    onResourceOpen: vi.fn(),
    onOpenUrl: vi.fn(),
    onSubagentOpen: vi.fn(),
    onLineageOpen: vi.fn(),
    onKnowledgeOpen: vi.fn(),
    onPlanOpen: vi.fn(),
    questionLive: null,
  };
}

/** A chat holding one message the server took, ready to be dropped. */
function tookThePrompt(): ConversationFoldState {
  const state = createConversationFoldState({ agentHost: "workspace" });
  foldRelayedPrompt(state, { id: "usr:c1", text: SAID, at: "2026-09-21T19:04:00Z" });
  return state;
}

function mount(state: ConversationFoldState, ctx: EntryContext): void {
  render(<ChatPanel entries={transcriptEntries(cloneTurns(state), ctx)} />);
}

const stopped = {
  event_type: "prompt.cancelled",
  message_id: "usr:c1",
  client_id: "c1",
  reason: "stopped",
};

describe("a message a Stop dropped", () => {
  it("reads as not sent, saying why, with the reader's words intact", () => {
    const state = tookThePrompt();
    foldHarnessEvent(state, stopped);
    mount(state, baseCtx());
    expect(screen.getByText(SAID)).toBeInTheDocument();
    expect(screen.getByText("Not sent (stopped)")).toBeInTheDocument();
  });

  it("says nothing of the sort while the message still stands", () => {
    // The tape is quiet about a message nobody dropped — the line is driven by
    // the box's event, not printed on every message the reader sent.
    mount(tookThePrompt(), baseCtx());
    expect(screen.getByText(SAID)).toBeInTheDocument();
    expect(screen.queryByText(/not sent/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /resend/i })).toBeNull();
  });

  it("sends the original words again, through the shell's own send path", async () => {
    const onResend = vi.fn();
    const state = tookThePrompt();
    foldHarnessEvent(state, stopped);
    mount(state, { ...baseCtx(), onResend });
    await userEvent.click(screen.getByRole("button", { name: "Resend" }));
    expect(onResend).toHaveBeenCalledExactlyOnceWith(SAID);
  });

  it("names the drop without saying why when the box gave no reason it knows", () => {
    // A word this build has never heard of must not reach the reader raw, and
    // must not stop the message reading as dropped either.
    const state = tookThePrompt();
    foldHarnessEvent(state, { ...stopped, reason: "machine_replaced" });
    mount(state, baseCtx());
    expect(screen.getByText("Not sent")).toBeInTheDocument();
    expect(screen.queryByText(/machine_replaced/)).toBeNull();
  });

  it("offers no Resend where the shell would refuse the send", () => {
    const state = tookThePrompt();
    foldHarnessEvent(state, stopped);
    mount(state, baseCtx());
    expect(screen.getByText("Not sent (stopped)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /resend/i })).toBeNull();
  });

  it("leaves a message the Stop did not reach untouched", () => {
    // Two messages were waiting; the box named one. The other is still the
    // reader's live message and carries no line at all.
    const state = tookThePrompt();
    foldRelayedPrompt(state, { id: "usr:c2", text: "And run the smoke test" });
    foldHarnessEvent(state, stopped);
    mount(state, { ...baseCtx(), onResend: vi.fn() });
    expect(screen.getAllByText(/not sent/i)).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Resend" })).toHaveLength(1);
    expect(screen.getByText("And run the smoke test")).toBeInTheDocument();
  });
});
