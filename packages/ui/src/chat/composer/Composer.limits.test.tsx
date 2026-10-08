import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Composer, messageTooLong } from "./Composer";
import { dedupeUploads, isAcceptedUpload, refuseUpload } from "./uploads";
import { CHAT_IMAGE_EXTENSIONS } from "../../primitives/render/Markdown/chatPaths";
import { MESSAGE_COUNTER_AT, UPLOAD_ACCEPTED_SUMMARY } from "../../theme/limits";

// What the composer refuses before the server has to: a message past the
// server's character ceiling, an attachment of a kind nothing will read, and
// the same file dropped twice.

afterEach(cleanup);

const MAX = 200;

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

/** A long message arrives as a paste in every real use, and `userEvent.type`
 *  would be one keystroke per character. */
function typeInto(text: string): void {
  const field = screen.getByRole("textbox") as HTMLTextAreaElement;
  fireEvent.change(field, { target: { value: text } });
}

async function pressEnter(): Promise<void> {
  const field = screen.getByRole("textbox");
  field.focus();
  await userEvent.keyboard("{Enter}");
}

describe("a message against the server's ceiling", () => {
  it("says nothing at all about an ordinary message", () => {
    render(<Composer {...base()} maxMessageChars={MAX} />);
    typeInto("hello");
    expect(screen.queryByText(/characters$/)).toBeNull();
  });

  it("counts out loud once the message is near the limit", () => {
    render(<Composer {...base()} maxMessageChars={MAX} />);
    typeInto("x".repeat(Math.ceil(MAX * MESSAGE_COUNTER_AT)));
    expect(screen.getByText(`${Math.ceil(MAX * MESSAGE_COUNTER_AT)} / ${MAX} characters`)).toBeTruthy();
  });

  it("refuses a message over it, in one sentence, and does not send", async () => {
    const props = base();
    render(<Composer {...props} maxMessageChars={MAX} />);
    typeInto("x".repeat(MAX + 1));

    await pressEnter();

    expect(props.onSend).not.toHaveBeenCalled();
    expect(screen.getByText(messageTooLong(MAX))).toBeTruthy();
    // And the words are still in the field: a refusal must not eat them.
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toHaveLength(MAX + 1);
  });

  it("sends a message exactly at the limit", async () => {
    const props = base();
    render(<Composer {...props} maxMessageChars={MAX} />);
    typeInto("x".repeat(MAX));

    await pressEnter();

    expect(props.onSend).toHaveBeenCalledTimes(1);
  });

  it("counts and refuses nothing when the host cannot say what the ceiling is", async () => {
    const props = base();
    render(<Composer {...props} />);
    typeInto("x".repeat(5_000));

    await pressEnter();

    expect(screen.queryByText(/characters$/)).toBeNull();
    expect(props.onSend).toHaveBeenCalledTimes(1);
  });
});

describe("what a message may carry", () => {
  const file = (name: string, size = 3, lastModified = 1): File => {
    const made = new File(["abc".slice(0, size)], name);
    Object.defineProperty(made, "size", { value: size });
    Object.defineProperty(made, "lastModified", { value: lastModified });
    return made;
  };

  it("takes the kinds a model can read", () => {
    for (const name of ["a.png", "b.PDF", "c.csv", "d.py", "notes.md", "sheet.xlsx"]) {
      expect(isAcceptedUpload(name)).toBe(true);
    }
  });

  it("takes every image type the transcript draws inline, SVG included", () => {
    for (const ext of CHAT_IMAGE_EXTENSIONS) {
      expect(isAcceptedUpload(`picture.${ext}`), ext).toBe(true);
      expect(refuseUpload(file(`picture.${ext.toUpperCase()}`)), ext).toBeNull();
    }
    expect(isAcceptedUpload("star.svg")).toBe(true);
  });

  it("refuses a kind nothing will read, naming the refused type and what it does take", () => {
    for (const [name, ext] of [
      ["setup.exe", "exe"],
      ["app.dmg", "dmg"],
      ["lib.so", "so"],
      ["archive.zip", "zip"],
    ]) {
      expect(isAcceptedUpload(name)).toBe(false);
      expect(refuseUpload(file(name))).toBe(
        `${name} can't be attached. A message takes ${UPLOAD_ACCEPTED_SUMMARY}, not a .${ext} file.`,
      );
    }
    expect(UPLOAD_ACCEPTED_SUMMARY).toContain("SVG");
  });

  it("says a name without an extension has none, rather than naming a type", () => {
    expect(refuseUpload(file("Makefile"))).toBe(
      `Makefile can't be attached. A message takes ${UPLOAD_ACCEPTED_SUMMARY}, not a file with no extension.`,
    );
  });

  it("refuses a name with no extension to decide on", () => {
    expect(isAcceptedUpload("Makefile")).toBe(false);
    expect(isAcceptedUpload("trailing.")).toBe(false);
    expect(isAcceptedUpload(".gitignore")).toBe(false);
  });

  it("lets a transport that knows better say what it takes", () => {
    expect(refuseUpload(file("setup.exe"), undefined, ["exe"])).toBeNull();
    // An empty list is a host declaring no opinion, which is not the same as
    // the default list.
    expect(refuseUpload(file("setup.exe"), undefined, [])).toBeNull();
    expect(refuseUpload(file("a.png"), undefined, ["csv"])).toBe(
      "a.png can't be attached. A message takes .csv, not a .png file.",
    );
  });

  it("still refuses on size first, so the reader reads the real reason", () => {
    expect(refuseUpload(file("big.png", 10), 5)).toContain("maximum upload size");
  });
});

describe("the same file picked twice", () => {
  const file = (name: string, size = 3, lastModified = 1): File => {
    const made = new File(["abc"], name);
    Object.defineProperty(made, "size", { value: size });
    Object.defineProperty(made, "lastModified", { value: lastModified });
    return made;
  };

  it("is staged once within one drop", () => {
    const a = file("q.csv");
    const again = file("q.csv");
    expect(dedupeUploads([a, again, file("r.csv")])).toHaveLength(2);
  });

  it("is not staged again beside one the field is already holding", () => {
    expect(dedupeUploads([file("q.csv")], [file("q.csv")])).toHaveLength(0);
  });

  it("keeps two files that only share a name", () => {
    expect(dedupeUploads([file("q.csv", 3), file("q.csv", 9)])).toHaveLength(2);
    expect(dedupeUploads([file("q.csv", 3, 1), file("q.csv", 3, 2)])).toHaveLength(2);
  });

  it("keeps the order they arrived in", () => {
    const picks = [file("a.csv"), file("b.csv"), file("a.csv"), file("c.csv")];
    expect(dedupeUploads(picks).map((f) => f.name)).toEqual(["a.csv", "b.csv", "c.csv"]);
  });
});
