import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Composer, type ComposerAttachment } from "./Composer";

// The composer's attach control. The package stays pure presentation, so the
// only thing these prove is the contract the host codes against: an accessible
// door named "Attach files", the picked `File` objects handed back untouched,
// and a chip per attachment stating the name the reader recognises, the
// progress of the bytes, and the refusal when there is one.

afterEach(cleanup);

const ATTACH = /attach/i;

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

function file(name: string, body = "x"): File {
  return new File([body], name, { type: "text/plain" });
}

describe("the composer's attach control", () => {
  it("offers no door where the host cannot keep bytes", () => {
    render(<Composer {...base()} />);
    expect(screen.queryByLabelText(ATTACH)).toBeNull();
  });

  it("is reachable by its accessible name and hands back what was picked", async () => {
    const onAttach = vi.fn();
    const { container } = render(<Composer {...base()} onAttach={onAttach} />);

    // The live spec clicks this exact name; the button, not the input, carries
    // it, so a screen reader is offered one control rather than two.
    const door = screen.getByLabelText(ATTACH);
    expect(door.tagName).toBe("BUTTON");

    const input = container.querySelector<HTMLInputElement>("input[type=file]");
    expect(input).not.toBeNull();
    expect(input?.multiple).toBe(true);
    expect(input?.getAttribute("aria-hidden")).toBe("true");

    const picked = [file("notes.json"), file("second.csv")];
    await userEvent.upload(input as HTMLInputElement, picked);

    expect(onAttach).toHaveBeenCalledTimes(1);
    expect(onAttach.mock.calls[0]?.[0]).toHaveLength(2);
    expect(
      (onAttach.mock.calls[0]?.[0] as File[]).map((f) => f.name),
    ).toEqual(["notes.json", "second.csv"]);
  });

  it("re-picking the same file is a fresh pick", async () => {
    const onAttach = vi.fn();
    const { container } = render(<Composer {...base()} onAttach={onAttach} />);
    const input = container.querySelector<HTMLInputElement>("input[type=file]");

    await userEvent.upload(input as HTMLInputElement, [file("same.json")]);
    await userEvent.upload(input as HTMLInputElement, [file("same.json")]);

    // The value is cleared after each change; without that the second pick
    // fires no event and the reader's click does nothing.
    expect(onAttach).toHaveBeenCalledTimes(2);
  });

  it("the pressed door opens the picker", async () => {
    const { container } = render(<Composer {...base()} onAttach={vi.fn()} />);
    const input = container.querySelector<HTMLInputElement>("input[type=file]");
    const clicked = vi.fn();
    (input as HTMLInputElement).addEventListener("click", clicked);

    await userEvent.click(screen.getByLabelText(ATTACH));

    expect(clicked).toHaveBeenCalledTimes(1);
  });

  it("shows a chip per attachment: the name, the progress, and the refusal", async () => {
    const onRemove = vi.fn();
    const attachments: ComposerAttachment[] = [
      { id: "u1", name: "going-up.json", progress: 40 },
      { id: "u2", name: "kept.csv" },
      { id: "u3", name: "refused.bin", error: "The upload failed." },
    ];
    render(
      <Composer
        {...base()}
        onAttach={vi.fn()}
        attachments={attachments}
        onRemoveAttachment={onRemove}
      />,
    );

    // The filename is visible before anything is sent — the whole point of the
    // chip.
    for (const one of attachments) expect(screen.getByText(one.name)).toBeTruthy();

    const rows = screen.getAllByRole("listitem");
    expect(rows.map((row) => row.getAttribute("data-state"))).toEqual([
      "sending",
      "kept",
      "error",
    ]);

    // Only the file whose bytes are still moving carries a progress readout,
    // and it reads the real percentage rather than an indeterminate bar.
    const bars = screen.getAllByRole("progressbar");
    expect(bars).toHaveLength(1);
    expect((bars[0] as HTMLProgressElement).value).toBe(40);

    expect(screen.getByText("The upload failed.")).toBeTruthy();

    await userEvent.click(
      within(rows[2] as HTMLElement).getByLabelText(/remove/i),
    );
    expect(onRemove).toHaveBeenCalledWith("u3");
  });

  it("a chip that cannot be withdrawn offers no remove", () => {
    render(
      <Composer
        {...base()}
        onAttach={vi.fn()}
        attachments={[{ id: "u1", name: "fixed.json" }]}
      />,
    );
    expect(screen.queryByLabelText(/remove/i)).toBeNull();
  });
});
