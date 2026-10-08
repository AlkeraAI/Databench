import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Composer, type RemoteCaret } from "./Composer";
import type { BindingNotice, TextBinding, TextSelection, TextView } from "./textBinding";

// The composer as one view of a shared document.
//
// The binding here is a recording fake: it keeps a text, hears every edit and
// caret move the field reports, and can play somebody else's edit back into
// the field. What is pinned is the field's half of the contract — what it
// reports, what it shows, where it leaves the reader's caret, and what it
// refuses to do on its own (undo, adopt during a composition, outgrow the cap).

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

class FakeBinding implements TextBinding {
  readonly maxBytes: number;
  current: string;
  view: TextView | null = null;
  edits: { before: string; after: string; selection: TextSelection | null }[] = [];
  selections: (TextSelection | null)[] = [];
  undos = 0;
  redos = 0;
  compositionEnds = 0;
  private caretListeners = new Set<(carets: RemoteCaret[]) => void>();
  private noticeListeners = new Set<(notice: BindingNotice | null) => void>();

  constructor(text = "", maxBytes = 0) {
    this.current = text;
    this.maxBytes = maxBytes;
  }
  text(): string {
    return this.current;
  }
  attach(view: TextView): () => void {
    this.view = view;
    return () => {
      this.view = null;
    };
  }
  edit(before: string, after: string, selection: TextSelection | null): void {
    this.edits.push({ before, after, selection });
    this.current = after;
  }
  select(selection: TextSelection | null): void {
    this.selections.push(selection);
  }
  compositionEnded(): void {
    this.compositionEnds += 1;
  }
  undo(): boolean {
    this.undos += 1;
    return true;
  }
  redo(): boolean {
    this.redos += 1;
    return true;
  }
  subscribeCarets(listener: (carets: RemoteCaret[]) => void): () => void {
    this.caretListeners.add(listener);
    return () => this.caretListeners.delete(listener);
  }
  subscribeNotice(listener: (notice: BindingNotice | null) => void): () => void {
    this.noticeListeners.add(listener);
    return () => this.noticeListeners.delete(listener);
  }
  /** Somebody else's edit lands: show `text`, keeping the reader at `selection`. */
  remote(text: string, selection: TextSelection | null): void {
    this.current = text;
    act(() => this.view?.setText(text, selection));
  }
  carets(carets: RemoteCaret[]): void {
    act(() => this.caretListeners.forEach((l) => l(carets)));
  }
  notice(notice: BindingNotice | null): void {
    act(() => this.noticeListeners.forEach((l) => l(notice)));
  }
}

describe("a composer bound to a shared document", () => {
  it("reports a keystroke against the text the field showed, not a remote edit it had not drawn", () => {
    const binding = new FakeBinding("[]");
    render(<Composer {...base()} binding={binding} />);
    field().focus();
    // Somebody else's letter reaches the binding; React has not drawn it yet
    // when the reader's key lands on the field as it stood.
    binding.current = "[a]";
    binding.view?.setText("[a]", null);
    expect(field().value).toBe("[]");
    fireEvent.change(field(), { target: { value: "[]b", selectionStart: 3, selectionEnd: 3 } });
    expect(binding.edits.at(-1)).toMatchObject({ before: "[]", after: "[]b" });
  });

  it("keeps a caret the reader moves while a remote edit waits to be drawn", async () => {
    const binding = new FakeBinding("AAA BBB");
    render(<Composer {...base()} binding={binding} />);
    field().focus();
    field().setSelectionRange(7, 7);
    // Somebody types at the start; the binding asks for the caret it captured.
    binding.current = "zzAAA BBB";
    binding.view?.setText("zzAAA BBB", { start: 9, end: 9, direction: "none" });
    // Before React draws it, the reader clicks at the very start.
    field().setSelectionRange(0, 0);
    fireEvent.select(field());
    await act(async () => {});
    expect(field().value).toBe("zzAAA BBB");
    expect(field().selectionStart).toBe(0);
  });

  it("keeps what it holds, and reports it, when the live draft lets the field go", () => {
    const binding = new FakeBinding("typed live");
    const onDraftChange = vi.fn();
    const { rerender } = render(
      <Composer {...base()} binding={binding} />,
    );
    expect(field().value).toBe("typed live");
    // The live lane is gone; a draft the host still holds from before is stale.
    rerender(<Composer {...base()} draft={{ text: "", at: 1 }} onDraftChange={onDraftChange} />);
    expect(field().value).toBe("typed live");
    expect(onDraftChange).toHaveBeenLastCalledWith("typed live");
    // A newer draft the host hands over is still taken, as unbound always.
    rerender(<Composer {...base()} draft={{ text: "typed live, theirs", at: 2 }} onDraftChange={onDraftChange} />);
    expect(field().value).toBe("typed live, theirs");
  });

  it("keeps the binding's caret when the reader types inside text that changed around them", async () => {
    // Somebody's large edit touches both sides of where the reader types; the
    // field's own offsets cannot place the caret in it, the binding's can.
    const binding = new FakeBinding("aaaa|bbbb");
    render(<Composer {...base()} binding={binding} />);
    field().focus();
    field().setSelectionRange(5, 5);
    binding.current = "aaaXa|bbbbYYYY";
    binding.view?.setText("aaaXa|bbbbYYYY", { start: 6, end: 6, direction: "none" });
    // The reader's next selection event arrives before React draws it.
    fireEvent.select(field());
    await act(async () => {});
    expect(field().selectionStart).toBe(6);
  });

  it("takes the reader's caret away when the field loses focus", () => {
    const binding = new FakeBinding("draft");
    render(<Composer {...base()} binding={binding} />);
    field().focus();
    field().setSelectionRange(2, 2);
    fireEvent.select(field());
    expect(binding.selections.at(-1)).not.toBeNull();
    fireEvent.blur(field());
    expect(binding.selections.at(-1)).toBeNull();
  });

  it("tells the binding the text its selection counts in: what the field has drawn", async () => {
    const binding = new FakeBinding("[]");
    render(<Composer {...base()} binding={binding} />);
    field().focus();
    binding.current = "[a]";
    binding.view?.setText("[a]", null);
    expect(binding.view?.getText()).toBe("[]");
    await act(async () => {});
    expect(field().value).toBe("[a]");
    expect(binding.view?.getText()).toBe("[a]");
  });

  it("shows the document's text on open", () => {
    render(<Composer {...base()} binding={new FakeBinding("held for us")} />);
    expect(field().value).toBe("held for us");
  });

  it("reports each edit as the text before and after, with where the caret ended", async () => {
    const binding = new FakeBinding("ac");
    render(<Composer {...base()} binding={binding} />);
    field().focus();
    field().setSelectionRange(1, 1);
    await userEvent.keyboard("b");
    expect(binding.edits).toEqual([
      { before: "ac", after: "abc", selection: { start: 2, end: 2, direction: "none" } },
    ]);
  });

  it("shows somebody else's edit and keeps the reader's caret where the document says", () => {
    const binding = new FakeBinding("hello");
    render(<Composer {...base()} binding={binding} />);
    field().focus();
    field().setSelectionRange(5, 5);
    binding.remote(">> hello", { start: 8, end: 8, direction: "none" });
    expect(field().value).toBe(">> hello");
    expect([field().selectionStart, field().selectionEnd]).toEqual([8, 8]);
    // Shown, not reported back: echoing it would be a loop between two composers.
    expect(binding.edits).toEqual([]);
  });

  it("keeps a backwards selection backwards across a remote edit", () => {
    const binding = new FakeBinding("one two");
    render(<Composer {...base()} binding={binding} />);
    field().focus();
    binding.remote("zero one two", { start: 5, end: 8, direction: "backward" });
    expect([field().selectionStart, field().selectionEnd, field().selectionDirection]).toEqual([
      5,
      8,
      "backward",
    ]);
  });

  it("tells the binding when an input method is composing and when it ends", () => {
    const binding = new FakeBinding();
    render(<Composer {...base()} binding={binding} />);
    expect(binding.view?.isComposing()).toBe(false);
    fireEvent.compositionStart(field());
    expect(binding.view?.isComposing()).toBe(true);
    fireEvent.compositionEnd(field());
    expect(binding.view?.isComposing()).toBe(false);
    expect(binding.compositionEnds).toBe(1);
  });

  it.each([
    ["{Meta>}z{/Meta}", 1, 0],
    ["{Control>}z{/Control}", 1, 0],
    ["{Meta>}{Shift>}z{/Shift}{/Meta}", 0, 1],
    ["{Control>}y{/Control}", 0, 1],
  ])("hands undo and redo to the binding: %s", async (keys, undos, redos) => {
    const binding = new FakeBinding("typed");
    render(<Composer {...base()} binding={binding} />);
    field().focus();
    await userEvent.keyboard(keys);
    expect([binding.undos, binding.redos]).toEqual([undos, redos]);
    expect(field().value).toBe("typed");
  });

  it("refuses to grow past the shared cap and says so, but still lets the reader cut", async () => {
    const binding = new FakeBinding("1234567", 8);
    render(<Composer {...base()} binding={binding} />);
    field().focus();
    field().setSelectionRange(7, 7);
    await userEvent.keyboard("89");
    expect(field().value).toBe("12345678");
    expect(screen.getByRole("alert").textContent).toMatch(/at most/);
    await userEvent.keyboard("{Backspace}");
    expect(field().value).toBe("1234567");
    expect(binding.edits.map((e) => e.after)).toEqual(["12345678", "1234567"]);
  });

  it("refuses a same-length replacement that grows the draft past its cap in bytes", () => {
    const binding = new FakeBinding("xxxxxx", 8);
    render(<Composer {...base()} binding={binding} />);
    field().focus();
    // Three ASCII letters replaced by three CJK ones: same length, 6 more bytes.
    fireEvent.change(field(), { target: { value: "漢漢漢xxx", selectionStart: 3, selectionEnd: 3 } });
    expect(field().value).toBe("xxxxxx");
    expect(binding.edits).toEqual([]);
    expect(screen.getByRole("alert").textContent).toMatch(/at most/);
    // Shrinking a draft that is already over is always allowed.
    binding.remote("漢漢漢x", null);
    fireEvent.change(field(), { target: { value: "漢漢漢", selectionStart: 3, selectionEnd: 3 } });
    expect(binding.edits.at(-1)).toMatchObject({ before: "漢漢漢x", after: "漢漢漢" });
  });

  it("clears only through the binding when a message is sent", async () => {
    const onSend = vi.fn();
    const binding = new FakeBinding("send me");
    render(<Composer {...base()} onSend={onSend} binding={binding} />);
    field().focus();
    await userEvent.keyboard("{Enter}");
    expect(onSend).toHaveBeenCalled();
    expect(binding.edits.at(-1)).toMatchObject({ before: "send me", after: "" });
  });

  it("draws the carets the binding reports", () => {
    const binding = new FakeBinding("hello world");
    const { container } = render(<Composer {...base()} binding={binding} />);
    binding.carets([{ id: "5001", name: "Dana", hue: 200, offset: 5, anchor: 5 }]);
    expect(container.querySelector('[data-caret-peer="5001"]')).not.toBeNull();
    binding.carets([]);
    expect(container.querySelector('[data-caret-peer="5001"]')).toBeNull();
  });

  it("offers text the document could not keep back to the reader", async () => {
    const binding = new FakeBinding("kept");
    render(<Composer {...base()} binding={binding} />);
    binding.notice({ message: "Your last edit could not be shared.", restorable: " lost words" });
    expect(screen.getByRole("alert").textContent).toContain("could not be shared");
    await userEvent.click(screen.getByRole("button", { name: "Restore my text" }));
    expect(field().value).toBe("kept lost words");
    expect(binding.edits.at(-1)).toMatchObject({ before: "kept", after: "kept lost words" });
  });

  it("draws no draft notice over a live binding", () => {
    render(<Composer {...base()} binding={new FakeBinding("live")} draftNotice="Live sync is unavailable right now." />);
    expect(screen.queryByText("Live sync is unavailable right now.")).toBeNull();
  });

  it("stops driving the field and forgets the carets when the binding goes", () => {
    const binding = new FakeBinding("x");
    const { rerender, container } = render(<Composer {...base()} binding={binding} />);
    binding.carets([{ id: "5001", name: "Dana", hue: 200, offset: 0, anchor: 0 }]);
    rerender(<Composer {...base()} />);
    expect(binding.view).toBeNull();
    expect(container.querySelector('[data-caret-peer="5001"]')).toBeNull();
  });
});

describe("a bound composer handed its own words back", () => {
  it("puts a returned draft into the shared text as the reader's edit", () => {
    const binding = new FakeBinding("");
    const { rerender } = render(<Composer {...base()} binding={binding} />);
    rerender(<Composer {...base()} binding={binding} draft={{ text: "could not send", at: 5 }} />);
    expect(field().value).toBe("could not send");
    expect(binding.edits.at(-1)).toMatchObject({ before: "", after: "could not send" });
  });
});
