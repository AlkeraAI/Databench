// What an unavailable composer still does, and what it refuses.
//
// Unavailable means nothing can be sent from here now — no machine serves the
// chat. Every way INTO a send is closed: the key, an offer from outside the
// field, a held message's own key. The one way OUT stays open: a turn waiting
// on a machine that is not there is withdrawn with Stop, and the server records
// that withdrawal without any box to hear it — so a Stop dead for want of a
// machine left a queued message nobody could take back.

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

const REASON = "No machine can serve your organization right now";

describe("Stop on an unavailable composer", () => {
  it("stays live over a waiting turn and withdraws it", () => {
    const onStop = vi.fn();
    render(<Composer {...base()} busy unavailable unavailableReason={REASON} onStop={onStop} />);
    const stop = screen.getByRole("button", { name: "Stop" });
    expect(stop).not.toBeDisabled();
    fireEvent.click(stop);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("is still one press: a stop already on the wire takes no second", () => {
    render(<Composer {...base()} busy stopping unavailable unavailableReason={REASON} onStop={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Stopping…" })).toBeDisabled();
  });

  it("with no turn running, the key is Send and it is dead", () => {
    const onSend = vi.fn();
    render(<Composer {...base()} onSend={onSend} unavailable unavailableReason={REASON} />);
    const send = screen.getByRole("button", { name: /^Send/ });
    expect(send).toBeDisabled();
  });
});

describe("the other ways into a send, on an unavailable composer", () => {
  it("a message a Stop cancelled offers no key to send it", () => {
    render(
      <Composer
        {...base()}
        unavailable
        unavailableReason={REASON}
        queued={[{ id: "q1", text: "and then deploy it", stopped: true }]}
        onSendQueued={vi.fn()}
        onRemoveQueued={vi.fn()}
      />,
    );
    expect(screen.getByText("Not sent (stopped)")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send queued message" })).toBeNull();
  });

  it("an offer from outside the field is ignored, and not replayed once the composer is back", () => {
    const onSend = vi.fn();
    const view = render(
      <Composer {...base()} onSend={onSend} unavailable unavailableReason={REASON} offer={{ text: "what changed?", at: 1 }} />,
    );
    expect(onSend).not.toHaveBeenCalled();
    view.rerender(<Composer {...base()} onSend={onSend} offer={{ text: "what changed?", at: 1 }} />);
    expect(onSend).not.toHaveBeenCalled();
    expect((screen.getByRole("textbox", { name: "Message Databench" }) as HTMLTextAreaElement).value).toBe("");
  });
});

describe("an offer on a composer that can send", () => {
  it("is sent once per stamp", () => {
    const onSend = vi.fn();
    const view = render(<Composer {...base()} onSend={onSend} />);
    view.rerender(<Composer {...base()} onSend={onSend} offer={{ text: "what changed?", at: 1 }} />);
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith("what changed?");
    view.rerender(<Composer {...base()} onSend={onSend} offer={{ text: "what changed?", at: 1 }} />);
    expect(onSend).toHaveBeenCalledTimes(1);
    view.rerender(<Composer {...base()} onSend={onSend} offer={{ text: "what changed?", at: 2 }} />);
    expect(onSend).toHaveBeenCalledTimes(2);
  });

  it("already present when the composer mounts is not sent — it was made to the one before", () => {
    const onSend = vi.fn();
    render(<Composer {...base()} onSend={onSend} offer={{ text: "what changed?", at: 4 }} />);
    expect(onSend).not.toHaveBeenCalled();
  });
});
