// A held message the reader's Stop cancelled says what happened to it.
//
// The words never reached the server, and the press that ended the turn is
// what would otherwise have released them — so the line under the field must
// not go on promising that they go when the turn finishes. It says they were
// not sent, in the same words the transcript uses for a message the box
// dropped, and carries the key that sends them if the reader wants them after
// all.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Composer } from "./Composer";

afterEach(cleanup);

function base() {
  return {
    modes: [{ value: "ask", label: "Ask" }],
    mode: "ask",
    models: [{ value: "m1", label: "M1" }],
    model: "m1",
    onModelChange: vi.fn(),
    efforts: [{ value: "low", label: "Low", bars: 1 as const }],
    effort: "low",
    onEffortChange: vi.fn(),
    onSend: vi.fn(),
  };
}

describe("a held message a Stop cancelled", () => {
  it("says it was not sent, instead of promising it goes on the turn's end", () => {
    render(
      <Composer
        {...base()}
        onQueue={vi.fn()}
        queued={[{ id: "q1", text: "and then deploy it", stopped: true }]}
        onSendQueued={vi.fn()}
      />,
    );
    expect(screen.getByText("Not sent (stopped)")).toBeTruthy();
    expect(screen.queryByText(/sends when the turn finishes/)).toBeNull();
    // The words are still the reader's and still on screen.
    const held = screen.getByRole("textbox", { name: "Queued message" }) as HTMLTextAreaElement;
    expect(held.value).toBe("and then deploy it");
  });

  it("carries the key that sends it, and sends only that one", () => {
    const onSendQueued = vi.fn();
    render(
      <Composer
        {...base()}
        busy
        onQueue={vi.fn()}
        queued={[
          { id: "q1", text: "and then deploy it", stopped: true },
          { id: "q2", text: "typed after the stop" },
        ]}
        onSendQueued={onSendQueued}
      />,
    );
    const keys = screen.getAllByRole("button", { name: "Send queued message" });
    expect(keys).toHaveLength(1);
    fireEvent.click(keys[0]);
    expect(onSendQueued).toHaveBeenCalledWith("q1");
  });

  it("is drawn apart from one typed after the stop, which still goes on its own", () => {
    render(
      <Composer
        {...base()}
        busy
        onQueue={vi.fn()}
        queued={[
          { id: "q1", text: "and then deploy it", stopped: true },
          { id: "q2", text: "typed after the stop" },
        ]}
        onSendQueued={vi.fn()}
      />,
    );
    expect(screen.getByText("Not sent (stopped)")).toBeTruthy();
    expect(screen.getByText("Queued · sends when the turn finishes")).toBeTruthy();
  });

  it("leaves a message nobody stopped under the line that says it goes", () => {
    render(
      <Composer
        {...base()}
        busy
        onQueue={vi.fn()}
        queued={[{ id: "q1", text: "and then deploy it" }]}
        onSendQueued={vi.fn()}
      />,
    );
    expect(screen.queryByText("Not sent (stopped)")).toBeNull();
    expect(screen.getByText("Queued · sends when the turn finishes")).toBeTruthy();
  });
});
