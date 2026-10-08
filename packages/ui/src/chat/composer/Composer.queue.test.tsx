// Enter is never a silent no-op.
//
// While the agent works, Send is Stop and the message cannot go — but the
// words the reader typed have to end up somewhere they can see. A host that
// can hold them (`onQueue`) gets them queued under the field, editable and
// removable until the turn ends. A host that cannot is told to say so, and the
// draft stays in the field either way rather than reading as sent.

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

function field(): HTMLTextAreaElement {
  return screen.getByRole("textbox", { name: "Message Databench" }) as HTMLTextAreaElement;
}

function press(text: string): void {
  fireEvent.change(field(), { target: { value: text } });
  fireEvent.keyDown(field(), { key: "Enter" });
}

describe("Enter while the agent is working", () => {
  it("queues the message instead of sending it", () => {
    const props = base();
    const onQueue = vi.fn();
    render(<Composer {...props} busy onQueue={onQueue} />);
    press("and then deploy it");
    expect(props.onSend).not.toHaveBeenCalled();
    expect(onQueue).toHaveBeenCalledWith("and then deploy it");
    // The words left the field: they are held on screen below it now.
    expect(field().value).toBe("");
  });

  it("shows the held message under the field, and takes it back", () => {
    const onRemoveQueued = vi.fn();
    render(
      <Composer
        {...base()}
        busy
        onQueue={vi.fn()}
        queued={[{ id: "q1", text: "and then deploy it" }]}
        onRemoveQueued={onRemoveQueued}
      />,
    );
    expect(screen.getByText("Queued · sends when the turn finishes")).toBeTruthy();
    expect(screen.getByDisplayValue("and then deploy it")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /remove queued message/i }));
    expect(onRemoveQueued).toHaveBeenCalledWith("q1");
  });

  it("reports an edit of a held message", () => {
    const onEditQueued = vi.fn();
    render(
      <Composer
        {...base()}
        busy
        onQueue={vi.fn()}
        queued={[{ id: "q1", text: "deploy it" }]}
        onEditQueued={onEditQueued}
      />,
    );
    fireEvent.change(screen.getByDisplayValue("deploy it"), {
      target: { value: "deploy it to staging" },
    });
    expect(onEditQueued).toHaveBeenCalledWith("q1", "deploy it to staging");
  });

  it("says why nothing went, and keeps the draft, where the host cannot hold it", () => {
    const props = base();
    render(<Composer {...props} busy />);
    press("and then deploy it");
    expect(props.onSend).not.toHaveBeenCalled();
    expect(field().value).toBe("and then deploy it");
    expect(
      screen.getByText("The agent is working. Your message will send when it finishes."),
    ).toBeTruthy();
  });

  it("keeps that promise: the kept draft goes out when the turn ends", () => {
    const props = base();
    const view = render(<Composer {...props} busy />);
    press("and then deploy it");
    expect(props.onSend).not.toHaveBeenCalled();

    view.rerender(<Composer {...props} />);
    expect(props.onSend).toHaveBeenCalledTimes(1);
    expect(props.onSend).toHaveBeenCalledWith("and then deploy it");
    expect(field().value).toBe("");
  });

  it("does not resend the kept draft on a later turn", () => {
    const props = base();
    const view = render(<Composer {...props} busy />);
    press("and then deploy it");
    view.rerender(<Composer {...props} />);
    expect(props.onSend).toHaveBeenCalledTimes(1);

    // A second turn comes and goes with nothing typed into the field.
    view.rerender(<Composer {...props} busy />);
    view.rerender(<Composer {...props} />);
    expect(props.onSend).toHaveBeenCalledTimes(1);
  });

  it("sends normally the moment the turn is over", () => {
    const props = base();
    const onQueue = vi.fn();
    const view = render(<Composer {...props} busy onQueue={onQueue} />);
    view.rerender(<Composer {...props} onQueue={onQueue} />);
    press("and then deploy it");
    expect(onQueue).not.toHaveBeenCalled();
    expect(props.onSend).toHaveBeenCalledWith("and then deploy it");
  });

  it("says a restored message has NOT been sent, and offers to send it", () => {
    // A message the tab was holding when it was last closed. It did not go out
    // on its own, and the card must not imply that it will.
    const onSendQueued = vi.fn();
    render(
      <Composer
        {...base()}
        onQueue={vi.fn()}
        queued={[{ id: "q1", text: "and then deploy it", restored: true }]}
        onSendQueued={onSendQueued}
        onRemoveQueued={vi.fn()}
      />,
    );
    expect(screen.getByText("Queued · not sent")).toBeTruthy();
    expect(screen.queryByText("Queued · sends when the turn finishes")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /send queued message/i }));
    expect(onSendQueued).toHaveBeenCalledWith("q1");
  });

  it("offers no send key for a message that goes on its own", () => {
    render(
      <Composer
        {...base()}
        busy
        onQueue={vi.fn()}
        queued={[{ id: "q1", text: "and then deploy it" }]}
        onSendQueued={vi.fn()}
        onRemoveQueued={vi.fn()}
      />,
    );
    expect(screen.getByText("Queued · sends when the turn finishes")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /send queued message/i })).toBeNull();
  });

  it("separates the two, so neither card lies about the other", () => {
    render(
      <Composer
        {...base()}
        busy
        onQueue={vi.fn()}
        queued={[
          { id: "q1", text: "the old one", restored: true },
          { id: "q2", text: "the new one" },
        ]}
        onSendQueued={vi.fn()}
        onRemoveQueued={vi.fn()}
      />,
    );
    expect(screen.getByText("Queued · not sent")).toBeTruthy();
    expect(screen.getByText("Queued · sends when the turn finishes")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: /send queued message/i })).toHaveLength(1);
  });

  // A message the server could not take is not a queued one: it was sent, it
  // did not go, and the reader needs to see that and retry it.
  it("draws a message the server could not take under Not sent, with a Retry", () => {
    const onSendQueued = vi.fn();
    render(
      <Composer
        {...base()}
        queued={[
          { id: "u1", text: "the message that did not go", unsent: true },
          { id: "q1", text: "and then deploy it" },
        ]}
        onSendQueued={onSendQueued}
        onRemoveQueued={vi.fn()}
      />,
    );
    expect(screen.getByText("Not sent")).toBeTruthy();
    // One key, on the row that did not go; the queued one still goes on its own.
    const retry = screen.getByRole("button", { name: "Retry sending message" });
    expect(retry).toHaveTextContent("Retry");
    expect(screen.queryByRole("button", { name: /send queued message/i })).toBeNull();
    fireEvent.click(retry);
    expect(onSendQueued).toHaveBeenCalledWith("u1");
  });

  it("offers no Retry while the composer cannot send at all", () => {
    render(
      <Composer
        {...base()}
        unavailable
        queued={[{ id: "u1", text: "the message that did not go", unsent: true }]}
        onSendQueued={vi.fn()}
        onRemoveQueued={vi.fn()}
      />,
    );
    expect(screen.getByText("Not sent")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry sending message" })).toBeNull();
  });

  it("queues nothing for an empty message", () => {
    const onQueue = vi.fn();
    render(<Composer {...base()} busy onQueue={onQueue} />);
    press("   ");
    expect(onQueue).not.toHaveBeenCalled();
  });
});

describe("what the queue says out loud", () => {
  // Enter during a turn moves the words out of the field. A reader who cannot
  // see the list under the composer has nothing else to go on, so the line that
  // names the group is a live region — and it carries the count, or a second
  // message would change nothing for it to announce.
  it("announces the group as a status, and again when a second message joins it", () => {
    const props = { ...base(), busy: true, onQueue: vi.fn(), onRemoveQueued: vi.fn() };
    const view = render(<Composer {...props} queued={[{ id: "q1", text: "one" }]} />);
    const region = screen.getByRole("status");
    expect(region).toHaveTextContent("Queued · sends when the turn finishes");

    view.rerender(
      <Composer {...props} queued={[{ id: "q1", text: "one" }, { id: "q2", text: "two" }]} />,
    );
    // The same node, saying something new: that is what a reader is told.
    expect(screen.getByRole("status")).toBe(region);
    expect(region).toHaveTextContent("Queued · sends when the turn finishes · 2");
  });

  it("gives the two groups their own regions, so neither speaks for the other", () => {
    render(
      <Composer
        {...base()}
        busy
        onQueue={vi.fn()}
        queued={[
          { id: "q1", text: "the old one", restored: true },
          { id: "q2", text: "the new one" },
        ]}
        onSendQueued={vi.fn()}
        onRemoveQueued={vi.fn()}
      />,
    );
    const said = screen.getAllByRole("status").map((node) => node.textContent);
    expect(said).toEqual(["Queued · not sent", "Queued · sends when the turn finishes"]);
  });
});

describe("a message that has already gone and is waiting for the turn", () => {
  // Nothing is held here: the message was accepted and the field cleared. The
  // line is the whole of what the reader gets, so it is a live region like the
  // queue's own, and it stands only while the host says the wait is on.
  it("says so under the field while the host says it is waiting", () => {
    const props = { ...base(), busy: true };
    const view = render(<Composer {...props} queuedBehindTurn />);
    expect(screen.getByRole("status")).toHaveTextContent("Queued behind the running turn");

    view.rerender(<Composer {...props} />);
    expect(screen.queryByText("Queued behind the running turn")).toBeNull();
  });

  it("says nothing on a busy composer the host has not said it about", () => {
    render(<Composer {...base()} busy />);
    expect(screen.queryByText("Queued behind the running turn")).toBeNull();
  });
});

describe("a long queued message", () => {
  const LONG = "select * from orders; ".repeat(1000);

  it("folds behind Show more, and opens to the whole message", () => {
    render(<Composer {...base()} busy onQueue={vi.fn()} queued={[{ id: "q1", text: LONG }]} />);
    const held = screen.getByRole("textbox", { name: "Queued message" });
    expect(held.hasAttribute("data-clamped")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(held.hasAttribute("data-clamped")).toBe(false);
    expect((held as HTMLTextAreaElement).value).toBe(LONG);
    fireEvent.click(screen.getByRole("button", { name: "Show less" }));
    expect(held.hasAttribute("data-clamped")).toBe(true);
  });

  it("folds a short message of many lines too", () => {
    render(<Composer {...base()} busy onQueue={vi.fn()} queued={[{ id: "q1", text: "a\nb\nc\nd\ne\nf" }]} />);
    expect(screen.getByRole("textbox", { name: "Queued message" }).hasAttribute("data-clamped")).toBe(true);
  });

  it("leaves a short message whole, with nothing to open", () => {
    render(<Composer {...base()} busy onQueue={vi.fn()} queued={[{ id: "q1", text: "and then deploy it" }]} />);
    expect(screen.getByRole("textbox", { name: "Queued message" }).hasAttribute("data-clamped")).toBe(false);
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
  });
});
