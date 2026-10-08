// A picture the reader attached, in the message they attached it to.
//
// The composer writes its pill inline — `![Image 1](paste-1-rbm4.png) see
// this…` — so the reference sits in the middle of a line, which is precisely
// the form the block parser leaves as text. What the reader then saw was the
// markdown they never typed, in one long unbroken run that pushed their bubble
// out to its full share and left the tail of their own sentence stranded on the
// far left of it. These pin the other outcome: the picture, their words, one
// bubble.

import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ChatFilesProvider, type ChatFilesResolver } from "../../primitives/render";
import { OrderTicket, ticketPieces } from "./OrderTicket";
import { TranscriptBlock } from "./TranscriptBlock";

const AT = "12:04 AM";

/** A pasted image, then what the reader wants done with it. */
const PASTED = "![Image 1](paste-1-rbm4.png) see this weird lack of margin? fix";

const seeing = (url: string | null): ChatFilesResolver => ({ resolveUrl: vi.fn(async () => url) });

function ticket(text: string, resolver: ChatFilesResolver | null): HTMLElement {
  render(
    <div className="chat-root">
      <ChatFilesProvider resolver={resolver}>
        <TranscriptBlock kind="user">
          <OrderTicket text={text} at={AT} />
        </TranscriptBlock>
      </ChatFilesProvider>
    </div>,
  );
  const article = document.querySelector(".chat-ticket");
  if (!(article instanceof HTMLElement)) throw new Error("the user turn rendered no bubble");
  return article;
}

describe("a picture the reader attached", () => {
  it("is shown, and the markdown that named it never is", async () => {
    const bubble = ticket(PASTED, seeing("blob:preview"));

    await waitFor(() => expect(screen.getByRole("img", { name: "Image 1" }).tagName).toBe("IMG"));
    expect(screen.getByRole("img", { name: "Image 1" }).getAttribute("src")).toBe("blob:preview");
    // The reader's own words, whole — and not one character of the reference.
    expect(bubble.textContent).toContain("see this weird lack of margin? fix");
    expect(bubble.textContent).not.toContain("![");
    expect(bubble.textContent).not.toContain("paste-1-rbm4.png");
  });

  it("stacks inside the ONE bubble with the words, never beside them", async () => {
    const bubble = ticket(PASTED, seeing("blob:preview"));
    await waitFor(() => expect(screen.getByRole("img", { name: "Image 1" }).tagName).toBe("IMG"));

    // One bubble, one body: the turn is not two boxes the reader's sentence can
    // be split across.
    expect(document.querySelectorAll(".chat-ticket")).toHaveLength(1);
    expect(bubble.querySelectorAll(".chat-ticket__body")).toHaveLength(1);
    const body = bubble.querySelector(".chat-ticket__body");
    if (!(body instanceof HTMLElement)) throw new Error("the bubble rendered no body");
    // The picture and the words are siblings in that one body, picture first —
    // stacked, because a figure is a block, not laid out in a row.
    const figure = body.querySelector("figure.alk-chat-image");
    if (!figure) throw new Error("the bubble rendered no picture");
    expect(figure.parentElement).toBe(body);
    expect(body.textContent?.trim()).toBe("see this weird lack of margin? fix");
    const tail = body.lastChild;
    if (!tail) throw new Error("the bubble rendered no words after the picture");
    expect(figure.compareDocumentPosition(tail) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("names the file when the chat has no bytes for it, rather than showing markdown", async () => {
    const bubble = ticket(PASTED, seeing(null));

    await waitFor(() => expect(document.querySelector('[data-state="missing"]')).not.toBeNull());
    // A chip carrying the file's own name — "Image 1" is the pill's number and
    // tells a reader nothing about which file went missing.
    const chip = screen.getByRole("img", { name: "paste-1-rbm4.png" });
    expect(chip.tagName).toBe("SPAN");
    expect(chip.classList.contains("alk-chat-image__missing")).toBe(true);
    expect(bubble.textContent).not.toContain("![");
    expect(bubble.textContent).toContain("see this weird lack of margin? fix");
  });

  it("is still a placeholder, not markdown, on a shell with nowhere to load from", () => {
    const bubble = ticket(PASTED, null);
    expect(screen.getByRole("img", { name: "paste-1-rbm4.png" }).tagName).toBe("SPAN");
    expect(bubble.textContent).not.toContain("![");
  });
});

describe("a reference that is code, not an attachment", () => {
  it("stays code inside a fence, fence marks and all", () => {
    const bubble = ticket("try this\n```md\n![x](a.png)\n```", seeing("blob:preview"));
    expect(screen.queryByRole("img")).toBeNull();
    expect(bubble.textContent).toContain("![x](a.png)");
    expect(bubble.textContent).toContain("```md");
  });

  it("stays code inside a backtick span, and the words around it stay one run", () => {
    const bubble = ticket("write `![x](a.png)` to embed", seeing("blob:preview"));
    expect(screen.queryByRole("img")).toBeNull();
    expect(bubble.textContent).toContain("write");
    expect(bubble.textContent).toContain("to embed");
    expect(bubble.textContent).toContain("![x](a.png)");
  });
});

describe("ticketPieces", () => {
  it.each([
    {
      id: "a fenced block, which is quoted text and never an attachment",
      text: "```md\n![x](a.png)\n```",
      want: [{ kind: "text", text: "```md\n![x](a.png)\n```" }],
    },
    {
      id: "a fenced block beside a real attachment, kept whole",
      text: "![Image 1](p.png)\n```md\n    ![x](a.png)\n```",
      want: [
        { kind: "image", alt: "Image 1", path: "p.png" },
        { kind: "text", text: "```md\n    ![x](a.png)\n```" },
      ],
    },
    {
      id: "an inline code span",
      text: "write `![x](a.png)` to embed",
      want: [{ kind: "text", text: "write `![x](a.png)` to embed" }],
    },
    {
      id: "display math, which is quoted the same way",
      text: "$$\n![x](a.png)\n$$",
      want: [{ kind: "text", text: "$$\n![x](a.png)\n$$" }],
    },
    {
      id: "a table beside an attachment, pipes and all",
      text: "![Image 1](p.png)\n| a | b |\n| --- | --- |\n| 1 | 2 |",
      want: [
        { kind: "image", alt: "Image 1", path: "p.png" },
        { kind: "text", text: "| a | b |\n| --- | --- |\n| 1 | 2 |" },
      ],
    },
    {
      id: "a list beside an attachment, still a list",
      text: "![Image 1](p.png)\n- one\n- two",
      want: [
        { kind: "image", alt: "Image 1", path: "p.png" },
        { kind: "text", text: "- one\n- two" },
      ],
    },
    {
      id: "a blank line the reader put between their own paragraphs",
      text: "![Image 1](p.png)\nfirst\n\nsecond",
      want: [
        { kind: "image", alt: "Image 1", path: "p.png" },
        { kind: "text", text: "first\n\nsecond" },
      ],
    },
    {
      id: "a reference written into the middle of a line",
      text: PASTED,
      want: [
        { kind: "image", alt: "Image 1", path: "paste-1-rbm4.png" },
        { kind: "text", text: "see this weird lack of margin? fix" },
      ],
    },
    {
      id: "words on both sides of it",
      text: "before ![Image 1](a.png) after",
      want: [
        { kind: "text", text: "before" },
        { kind: "image", alt: "Image 1", path: "a.png" },
        { kind: "text", text: "after" },
      ],
    },
    {
      id: "two pictures on one line",
      text: "![Image 1](a.png) and ![Image 2](b.png)",
      want: [
        { kind: "image", alt: "Image 1", path: "a.png" },
        { kind: "text", text: "and" },
        { kind: "image", alt: "Image 2", path: "b.png" },
      ],
    },
    {
      id: "a reference on its own line, as the parser already read it",
      text: "look\n![Image 1](a.png)",
      want: [
        { kind: "text", text: "look" },
        { kind: "image", alt: "Image 1", path: "a.png" },
      ],
    },
    {
      id: "a web image, which the chat never loads on the reader's behalf",
      text: "see ![x](https://example.com/x.png) here",
      want: [{ kind: "text", text: "see ![x](https://example.com/x.png) here" }],
    },
    {
      id: "an escape out of the chat folder",
      text: "see ![x](../x.png) here",
      want: [{ kind: "text", text: "see ![x](../x.png) here" }],
    },
    {
      id: "a file the reader handed over, which is a link and not a picture",
      text: "[File 1: q.csv](file-1-cd34.csv) have a look",
      want: [{ kind: "text", text: "[File 1: q.csv](file-1-cd34.csv) have a look" }],
    },
    {
      id: "words with nothing attached at all",
      text: "just words",
      want: [{ kind: "text", text: "just words" }],
    },
  ])("reads $id", ({ text, want }) => {
    expect(ticketPieces(text)).toEqual(want);
  });
});
