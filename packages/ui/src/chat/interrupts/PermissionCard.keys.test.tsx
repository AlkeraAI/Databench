// The ask's keys reach it wherever the reader's focus is. A permission card is
// the one thing on screen waiting on them, so Enter answers "allow once" and
// Escape "reject once" without asking them to click the card first. Pinned
// here: that they fire from the document, that they stand down where those
// keys already mean something (a field, a modifier, another menu), that a
// second card behind the front one never answers, and that the card stops
// listening once it has been answered or taken off screen.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PermissionCard, type PermissionCardProps } from "./PermissionCard";

afterEach(cleanup);

function props(overrides: Partial<PermissionCardProps> = {}): PermissionCardProps {
  return {
    title: "Run a command",
    pattern: "rm -rf build",
    kind: "shell",
    allow: { optionId: "allow_once", label: "Allow once" },
    deny: { optionId: "reject_once", label: "Reject once" },
    always: {
      label: "Always allow…",
      options: [{ optionId: "allow_always", label: "this exact command", scope: "exact" }],
    },
    onDecide: () => {},
    ...overrides,
  };
}

describe("the permission card answers from anywhere on the page", () => {
  it("allows on Enter pressed with nothing focused", () => {
    const onDecide = vi.fn();
    render(<PermissionCard {...props({ onDecide })} />);

    fireEvent.keyDown(document.body, { key: "Enter" });

    expect(onDecide).toHaveBeenCalledWith("allow_once");
  });

  it("rejects on Escape pressed with nothing focused", () => {
    const onDecide = vi.fn();
    render(<PermissionCard {...props({ onDecide })} />);

    fireEvent.keyDown(document.body, { key: "Escape" });

    expect(onDecide).toHaveBeenCalledWith("reject_once");
  });

  it("leaves both keys to a field the reader is typing in", () => {
    const onDecide = vi.fn();
    render(
      <>
        <textarea data-testid="composer" />
        <PermissionCard {...props({ onDecide })} />
      </>,
    );
    const field = screen.getByTestId("composer");
    field.focus();

    fireEvent.keyDown(field, { key: "Enter" });
    fireEvent.keyDown(field, { key: "Escape" });

    expect(onDecide).not.toHaveBeenCalled();
  });

  it("leaves both keys to a contenteditable the reader is typing in", () => {
    const onDecide = vi.fn();
    render(
      <>
        <div contentEditable data-testid="editor" />
        <PermissionCard {...props({ onDecide })} />
      </>,
    );
    const editor = screen.getByTestId("editor");
    editor.focus();

    fireEvent.keyDown(editor, { key: "Enter" });
    fireEvent.keyDown(editor, { key: "Escape" });

    expect(onDecide).not.toHaveBeenCalled();
  });

  it.each([
    ["meta", { metaKey: true }],
    ["ctrl", { ctrlKey: true }],
    ["alt", { altKey: true }],
    ["shift", { shiftKey: true }],
  ])("keeps Enter's native meaning while %s is held", (_name, modifier) => {
    const onDecide = vi.fn();
    render(<PermissionCard {...props({ onDecide })} />);

    fireEvent.keyDown(document.body, { key: "Enter", ...modifier });

    expect(onDecide).not.toHaveBeenCalled();
  });

  it.each([
    ["meta", { metaKey: true }],
    ["ctrl", { ctrlKey: true }],
    ["alt", { altKey: true }],
  ])("keeps Escape's native meaning while %s is held", (_name, modifier) => {
    const onDecide = vi.fn();
    render(<PermissionCard {...props({ onDecide })} />);

    fireEvent.keyDown(document.body, { key: "Escape", ...modifier });

    expect(onDecide).not.toHaveBeenCalled();
  });

  it.each([
    ["a link", <a key="c" href="#docs" data-testid="control">Docs</a>],
    [
      "a button",
      <button key="c" type="button" data-testid="control">
        Re-run
      </button>,
    ],
    [
      "a menu row",
      <div key="c" role="menuitem" tabIndex={0} data-testid="control">
        Rename
      </div>,
    ],
    [
      "a tool card's own control",
      <div key="c" role="button" tabIndex={0} data-testid="control">
        Show diff
      </div>,
    ],
  ])("leaves both keys to %s the reader has tabbed to outside the card", (_name, control) => {
    const onDecide = vi.fn();
    render(
      <>
        {control}
        <PermissionCard {...props({ onDecide })} />
      </>,
    );
    const focused = screen.getByTestId("control");
    focused.focus();

    // Enter on a focused control is that control's own activation: the ask
    // must neither answer itself nor swallow the press.
    expect(fireEvent.keyDown(focused, { key: "Enter" })).toBe(true);
    expect(fireEvent.keyDown(document.body, { key: "Enter" })).toBe(true);
    expect(fireEvent.keyDown(focused, { key: "Escape" })).toBe(true);
    expect(fireEvent.keyDown(document.body, { key: "Escape" })).toBe(true);

    expect(onDecide).not.toHaveBeenCalled();
  });

  it("still answers while focus is parked on a container that takes no key of its own", () => {
    const onDecide = vi.fn();
    render(
      <>
        <div tabIndex={-1} data-testid="pane" />
        <PermissionCard {...props({ onDecide })} />
      </>,
    );
    screen.getByTestId("pane").focus();

    fireEvent.keyDown(document.body, { key: "Enter" });

    expect(onDecide).toHaveBeenCalledWith("allow_once");
  });

  it("ignores a key still inside an IME composition", () => {
    const onDecide = vi.fn();
    render(<PermissionCard {...props({ onDecide })} />);

    fireEvent.keyDown(document.body, { key: "Enter", isComposing: true });
    fireEvent.keyDown(document.body, { key: "Escape", isComposing: true });

    expect(onDecide).not.toHaveBeenCalled();
  });

  it("answers again once the dock has emptied and a new ask has arrived", () => {
    const gone = vi.fn();
    render(<PermissionCard {...props({ onDecide: gone })} />).unmount();

    const fresh = vi.fn();
    render(<PermissionCard {...props({ onDecide: fresh })} />);
    fireEvent.keyDown(document.body, { key: "Enter" });

    expect(fresh).toHaveBeenCalledWith("allow_once");
    expect(gone).not.toHaveBeenCalled();
  });

  it("stands down while another dialog is open over the chat", () => {
    const onDecide = vi.fn();
    render(
      <>
        <PermissionCard {...props({ onDecide })} />
        <div role="dialog" aria-label="Settings" />
      </>,
    );

    fireEvent.keyDown(document.body, { key: "Enter" });
    fireEvent.keyDown(document.body, { key: "Escape" });

    expect(onDecide).not.toHaveBeenCalled();
  });

  it("leaves the always-allow menu its own keys while it is open", async () => {
    const onDecide = vi.fn();
    render(<PermissionCard {...props({ onDecide })} />);
    fireEvent.click(screen.getByRole("button", { name: "Always allow…" }));
    expect(await screen.findByRole("menu")).not.toBeNull();

    fireEvent.keyDown(document.body, { key: "Enter" });

    expect(onDecide).not.toHaveBeenCalled();
  });

  it("answers only the front card when a second is stacked behind it", () => {
    const front = vi.fn();
    const behind = vi.fn();
    render(
      <>
        <PermissionCard
          {...props({ onDecide: front, queue: { position: 1, of: 2 } })}
        />
        <PermissionCard
          {...props({
            title: "Run another command",
            onDecide: behind,
            queue: { position: 2, of: 2 },
          })}
        />
      </>,
    );

    fireEvent.keyDown(document.body, { key: "Enter" });

    expect(front).toHaveBeenCalledWith("allow_once");
    expect(behind).not.toHaveBeenCalled();
  });

  it("stops listening once the ask has been answered", () => {
    const onDecide = vi.fn();
    render(<PermissionCard {...props({ onDecide })} />);

    fireEvent.keyDown(document.body, { key: "Enter" });
    fireEvent.keyDown(document.body, { key: "Enter" });
    fireEvent.keyDown(document.body, { key: "Escape" });

    expect(onDecide).toHaveBeenCalledTimes(1);
    expect(onDecide).toHaveBeenCalledWith("allow_once");
  });

  it("stops listening once the card is off screen", () => {
    const onDecide = vi.fn();
    const view = render(<PermissionCard {...props({ onDecide })} />);
    view.unmount();

    fireEvent.keyDown(document.body, { key: "Enter" });
    fireEvent.keyDown(document.body, { key: "Escape" });

    expect(onDecide).not.toHaveBeenCalled();
  });

  it("does not approve on Enter where the ask offers no approval", () => {
    const onDecide = vi.fn();
    render(
      <PermissionCard
        {...props({ onDecide, allow: undefined, refusal: "This workspace is read-only." })}
      />,
    );

    fireEvent.keyDown(document.body, { key: "Enter" });
    expect(onDecide).not.toHaveBeenCalled();

    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onDecide).toHaveBeenCalledWith("reject_once");
  });

  it("answers nothing while the ask is waiting on its subject", () => {
    const onDecide = vi.fn();
    render(
      <PermissionCard
        {...props({ onDecide, pattern: "", waiting: "Waiting for the command…" })}
      />,
    );

    fireEvent.keyDown(document.body, { key: "Enter" });
    fireEvent.keyDown(document.body, { key: "Escape" });

    expect(onDecide).not.toHaveBeenCalled();
  });

  it("leaves a key aimed at the card itself to the card's own buttons", () => {
    const onDecide = vi.fn();
    render(<PermissionCard {...props({ onDecide })} />);
    const denyButton = screen.getByRole("button", { name: /Reject once/ });
    denyButton.focus();

    // A focused button activates natively on Enter; the document listener must
    // not turn that press into an approval as well.
    fireEvent.keyDown(denyButton, { key: "Enter" });

    expect(onDecide).not.toHaveBeenCalledWith("allow_once");
  });
});

describe("the mode switch on the card", () => {
  const modes = (onChange: (value: string) => void) => ({
    label: "Permission mode",
    value: "default",
    options: [
      { value: "default", label: "Default" },
      { value: "read_only", label: "Read-only" },
    ],
    onChange,
  });

  it("moves the mode without answering the ask", () => {
    const onDecide = vi.fn();
    const onChange = vi.fn();
    render(<PermissionCard {...props({ onDecide, mode: modes(onChange) })} />);

    fireEvent.click(screen.getByRole("button", { name: "Permission mode: Default" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Read-only" }));

    expect(onChange).toHaveBeenCalledWith("read_only");
    expect(onDecide).not.toHaveBeenCalled();
  });

  it("closes its menu on Escape instead of rejecting the ask", () => {
    const onDecide = vi.fn();
    render(<PermissionCard {...props({ onDecide, mode: modes(() => {}) })} />);

    fireEvent.click(screen.getByRole("button", { name: "Permission mode: Default" }));
    const row = screen.getByRole("menuitemradio", { name: "Default" });
    fireEvent.keyDown(row, { key: "Escape" });
    expect(screen.queryByRole("menuitemradio")).toBeNull();
    expect(onDecide).not.toHaveBeenCalled();

    // With the menu shut, Escape is the ask's again.
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onDecide).toHaveBeenCalledWith("reject_once");
  });

  it("does not move the mode to the one already in force", () => {
    const onChange = vi.fn();
    render(<PermissionCard {...props({ mode: modes(onChange) })} />);
    fireEvent.click(screen.getByRole("button", { name: "Permission mode: Default" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Default" }));
    expect(onChange).not.toHaveBeenCalled();
  });
});
