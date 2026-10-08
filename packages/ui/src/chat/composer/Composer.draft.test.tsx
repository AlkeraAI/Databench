import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Composer } from "./Composer";

// The shared composer draft, from the field's side.
//
// The package stays pure presentation: it does not know that the draft comes
// off a chat document, only that the host hands it one and wants to hear about
// typing. What it DOES own is the rule that keeps two people from fighting over
// one field — a draft is adopted when its stamp is one this field has not
// already taken, so a re-render, a round trip, and the reader's own text coming
// back never move the caret.

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

const field = (): HTMLTextAreaElement => screen.getByLabelText("Message Databench");

describe("a composer with a shared draft", () => {
  it("opens with what the chat was holding", () => {
    render(<Composer {...base()} draft={{ text: "what about last quarter?", at: 10 }} />);
    expect(field().value).toBe("what about last quarter?");
  });

  it("adopts a newer draft, leaving what was there behind", () => {
    const { rerender } = render(<Composer {...base()} draft={{ text: "first", at: 10 }} />);
    rerender(<Composer {...base()} draft={{ text: "second", at: 11 }} />);
    expect(field().value).toBe("second");
  });

  it("ignores a draft whose stamp it has already taken", async () => {
    const { rerender } = render(<Composer {...base()} draft={{ text: "adopted", at: 10 }} />);
    await userEvent.type(field(), " and then some");
    expect(field().value).toBe("adopted and then some");

    // The same draft arrives again — a re-render, a reconnect's snapshot, the
    // reader's own write rebroadcast. Re-adopting it would eat what they have
    // typed since.
    rerender(<Composer {...base()} draft={{ text: "adopted", at: 10 }} />);

    expect(field().value).toBe("adopted and then some");
  });

  it("reports every keystroke to the host", async () => {
    const onDraftChange = vi.fn();
    render(<Composer {...base()} onDraftChange={onDraftChange} />);
    await userEvent.type(field(), "hey");
    expect(onDraftChange.mock.calls.map((c) => c[0])).toEqual(["h", "he", "hey"]);
  });

  it("does not report a draft it adopted from the host", () => {
    const onDraftChange = vi.fn();
    const { rerender } = render(
      <Composer {...base()} draft={{ text: "theirs", at: 10 }} onDraftChange={onDraftChange} />,
    );
    rerender(
      <Composer {...base()} draft={{ text: "theirs again", at: 11 }} onDraftChange={onDraftChange} />,
    );
    // Echoing an adopted draft back to the host is a loop between two
    // composers, and each pass writes a durable operation.
    expect(onDraftChange).not.toHaveBeenCalled();
  });

  it("clears the shared draft when the message is sent", async () => {
    const onSend = vi.fn();
    const onDraftChange = vi.fn();
    render(<Composer {...base()} onSend={onSend} onDraftChange={onDraftChange} />);
    await userEvent.type(field(), "ship it{Enter}");
    expect(onSend).toHaveBeenCalledWith("ship it");
    expect(onDraftChange).toHaveBeenLastCalledWith("");
  });

  it("tells the host to persist when the field loses focus", async () => {
    const onDraftBlur = vi.fn();
    render(<Composer {...base()} onDraftBlur={onDraftBlur} />);
    await userEvent.click(field());
    await userEvent.tab();
    expect(onDraftBlur).toHaveBeenCalled();
  });

  it("says why the field is the reader's alone, and still types and sends", async () => {
    const onSend = vi.fn();
    const onDraftChange = vi.fn();
    render(
      <Composer
        {...base()}
        onSend={onSend}
        onDraftChange={onDraftChange}
        draftNotice="This browser does not support our live sync."
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("This browser does not support our live sync.");
    await userEvent.type(field(), "ship it{Enter}");
    expect(onDraftChange).toHaveBeenCalledWith("ship it");
    expect(onSend).toHaveBeenCalledWith("ship it");
  });

  it("is an ordinary local field for a host that shares nothing", async () => {
    render(<Composer {...base()} />);
    await userEvent.type(field(), "just me");
    expect(field().value).toBe("just me");
  });
});
