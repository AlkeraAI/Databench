// Renaming the chat by its own name.
//
// The header carries no rename key: the title IS the control, and only where a
// host wired one. What is pinned here is that seam — the plain heading a host
// that wired nothing still gets (the editor's webview is exactly that host),
// the field the name becomes, which of the four ways out of it write and which
// abandon, and that a name far longer than the row is both readable (clipped,
// with the whole of it in the tip) and editable (the field opens on the WHOLE
// name, selected).

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Chrome } from "./Chrome";

const TITLE = "Yesterday's orders";
/** Far past any header's width: the case where the reading on screen and the
 *  name being edited are not the same string. */
const LONG = `${"Every order placed in the western region since the migration ".repeat(5)}`.slice(0, 300);

const field = (): HTMLInputElement =>
  screen.getByRole("textbox", { name: "Chat title" }) as HTMLInputElement;

const openEditor = (title = TITLE): void => {
  fireEvent.click(screen.getByRole("button", { name: `Rename chat: ${title}` }));
};

/** Make jsdom, which lays nothing out, report the element as clipped — the one
 *  fact `tooltip="truncate"` decides on. */
function reportClipped(el: HTMLElement): void {
  Object.defineProperty(el, "scrollWidth", { configurable: true, value: 900 });
  Object.defineProperty(el, "clientWidth", { configurable: true, value: 200 });
}

describe("a chrome with no rename seam", () => {
  it("renders the title as plain text, with no control on it", () => {
    render(<Chrome title={TITLE} />);

    const heading = screen.getByRole("heading", { level: 1, name: TITLE });
    expect(heading).toBeInTheDocument();
    // This is the editor webview's shape: nothing here is pressable, and there
    // is no way into an editor.
    expect(screen.queryByRole("button", { name: /Rename chat/ })).not.toBeInTheDocument();
    expect(heading.querySelector("button")).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Chat title" })).not.toBeInTheDocument();
  });
});

describe("a chrome whose host wired a rename", () => {
  it("the title itself opens the editor, prefilled with the whole name", () => {
    render(<Chrome title={LONG} onRenameTitle={vi.fn()} />);

    // Still a heading: the name did not become a button-shaped thing beside it.
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(LONG);
    openEditor(LONG);

    expect(field().value).toBe(LONG);
    expect(field().value.length).toBe(300);
  });

  it("opens with the whole name selected, so a long one is replaced in one keystroke", () => {
    render(<Chrome title={LONG} onRenameTitle={vi.fn()} />);
    openEditor(LONG);

    expect(field().selectionStart).toBe(0);
    expect(field().selectionEnd).toBe(LONG.length);
  });

  it("Enter writes the trimmed name through the host and closes the editor", async () => {
    const rename = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(<Chrome title={TITLE} onRenameTitle={rename} />);
    openEditor();

    fireEvent.change(field(), { target: { value: "  Q3 orders  " } });
    fireEvent.keyDown(field(), { key: "Enter" });

    expect(rename).toHaveBeenCalledWith("Q3 orders");
    // The host answers by moving the title it hands back in.
    rerender(<Chrome title="Q3 orders" onRenameTitle={rename} />);
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "Chat title" })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Q3 orders");
  });

  it("Escape abandons the edit and writes nothing", () => {
    const rename = vi.fn().mockResolvedValue(undefined);
    render(<Chrome title={TITLE} onRenameTitle={rename} />);
    openEditor();

    fireEvent.change(field(), { target: { value: "Q3 orders" } });
    fireEvent.keyDown(field(), { key: "Escape" });

    expect(rename).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox", { name: "Chat title" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(TITLE);
  });

  it("an edit abandoned by Escape is not still in the field the next time it opens", () => {
    render(<Chrome title={TITLE} onRenameTitle={vi.fn()} />);
    openEditor();
    fireEvent.change(field(), { target: { value: "Q3 orders" } });
    fireEvent.keyDown(field(), { key: "Escape" });

    openEditor();
    expect(field().value).toBe(TITLE);
  });

  it("leaving the field keeps a name that changed", async () => {
    const rename = vi.fn().mockResolvedValue(undefined);
    render(<Chrome title={TITLE} onRenameTitle={rename} />);
    openEditor();

    fireEvent.change(field(), { target: { value: "Q3 orders" } });
    fireEvent.blur(field());

    expect(rename).toHaveBeenCalledWith("Q3 orders");
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "Chat title" })).not.toBeInTheDocument(),
    );
  });

  it("leaving the field abandons a name nobody touched, rather than writing a version for nothing", () => {
    const rename = vi.fn().mockResolvedValue(undefined);
    render(<Chrome title={TITLE} onRenameTitle={rename} />);
    openEditor();

    fireEvent.blur(field());

    expect(rename).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox", { name: "Chat title" })).not.toBeInTheDocument();
  });

  it("an emptied name is no rename at all: nothing is written and the chat keeps its name", () => {
    const rename = vi.fn().mockResolvedValue(undefined);
    render(<Chrome title={TITLE} onRenameTitle={rename} />);
    openEditor();

    fireEvent.change(field(), { target: { value: "   " } });
    fireEvent.keyDown(field(), { key: "Enter" });

    expect(rename).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(TITLE);
  });

  it("the field stays put and read-only while the write is in flight", async () => {
    let settle = (): void => {};
    const rename = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    );
    render(<Chrome title={TITLE} onRenameTitle={rename} />);
    openEditor();

    fireEvent.change(field(), { target: { value: "Q3 orders" } });
    fireEvent.keyDown(field(), { key: "Enter" });

    // Not swapped back to the heading mid-write, which would read as the
    // rename having failed.
    expect(field()).toHaveAttribute("readonly");
    expect(field().value).toBe("Q3 orders");
    // And a second Enter cannot double-write it.
    fireEvent.keyDown(field(), { key: "Enter" });
    expect(rename).toHaveBeenCalledTimes(1);

    settle();
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "Chat title" })).not.toBeInTheDocument(),
    );
  });

  it("a refused rename reverts to the name the chat had and reads the refusal out", async () => {
    const rename = vi.fn().mockRejectedValue(new Error("You cannot rename this chat."));
    render(<Chrome title={TITLE} onRenameTitle={rename} />);
    openEditor();

    fireEvent.change(field(), { target: { value: "Q3 orders" } });
    fireEvent.keyDown(field(), { key: "Enter" });

    expect(await screen.findByRole("alert")).toHaveTextContent("You cannot rename this chat.");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(TITLE);
    // And the abandoned draft is not waiting in the field on the next open.
    openEditor();
    expect(field().value).toBe(TITLE);
  });

  it("a refusal with nothing to say still says the rename did not happen", async () => {
    const rename = vi.fn().mockRejectedValue(new Error(""));
    render(<Chrome title={TITLE} onRenameTitle={rename} />);
    openEditor();
    fireEvent.change(field(), { target: { value: "Q3 orders" } });
    fireEvent.keyDown(field(), { key: "Enter" });

    expect(await screen.findByRole("alert")).toHaveTextContent("This chat could not be renamed.");
  });

  it("a name too long for the row is clipped on screen and whole in its tip", async () => {
    render(<Chrome title={LONG} onRenameTitle={vi.fn()} />);
    const control = screen.getByRole("button", { name: `Rename chat: ${LONG}` });

    // The clipping lives on the control, so the measurement behind the tip is
    // taken on the element that is actually cut off.
    expect(control.className).toContain("chat-chrome__title-button");
    reportClipped(control);
    fireEvent.pointerEnter(control);

    // The tip carries the WHOLE name, not the reading on screen.
    expect(await screen.findByRole("tooltip")).toHaveTextContent(LONG);
  });

  it("a name that fits gets no tip", () => {
    render(<Chrome title={TITLE} onRenameTitle={vi.fn()} />);
    const control = screen.getByRole("button", { name: `Rename chat: ${TITLE}` });

    Object.defineProperty(control, "scrollWidth", { configurable: true, value: 200 });
    Object.defineProperty(control, "clientWidth", { configurable: true, value: 200 });
    fireEvent.pointerEnter(control);

    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("a chat renamed elsewhere opens its editor on the new name, not the one this header booted with", () => {
    const { rerender } = render(<Chrome title={TITLE} onRenameTitle={vi.fn()} />);
    rerender(<Chrome title="Renamed by somebody else" onRenameTitle={vi.fn()} />);

    openEditor("Renamed by somebody else");
    expect(field().value).toBe("Renamed by somebody else");
  });
});
