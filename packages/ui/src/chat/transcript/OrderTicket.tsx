// The reader's own message. Their words stay their words — no markdown
// treatment beyond the two things they can put on a message that are not
// words: an image they pasted (`![Image 1](scratch/paste-1-x.png)`, rendered
// through the same resolver as the agent's charts) and a file they handed the
// chat (`[File 1: report.csv](scratch/file-1-x.csv)`, a link that opens it).
// Both are the transcript's one contract for a chat-folder path, so the reader
// sees the picture they pasted rather than the markdown the model was sent.

import { Fragment, type ReactNode } from "react";

import { ChatImage, chatImagePath, renderInline } from "../sharedUi";
import "./ticket.css";

/** What one ticket is made of: the pictures the reader attached, and the words
 *  around them. */
export type TicketPiece =
  | { kind: "image"; alt: string; path: string }
  | { kind: "text"; text: string };

// A reference to a picture, wherever in a line it sits. The composer writes its
// pill INLINE (`![Image 1](paste-1-x.png) have a look`), so a reader's own
// attachment is almost never on a line of its own — the form the block parser
// deliberately leaves as text, because for streamed agent prose splitting every
// paragraph would be a second pass on the hot path. A ticket is not streamed:
// it arrives whole, once, so it pays that pass and the reader sees the picture
// they pasted instead of the markdown it was sent as.
const INLINE_IMAGE = /!\[([^\]]*)\]\(([^)]+)\)/g;

// Quoted text: a fence, display math, a backtick span. What is inside them is
// something the reader is SHOWING, not something they attached, so a reference
// there is left exactly as typed — marks included. Same shapes the block and
// inline parsers read, so the two never disagree about where quoting starts.
const FENCE_OPEN = /^```([A-Za-z0-9_-]+)?\s*$/;
const FENCE_CLOSE = /^```\s*$/;
const MATH_FENCE = /^\$\$\s*$/;
const CODE_SPAN = /`[^`]+`/g;

/** Where a line quotes its own contents, as [start, end) offsets. */
function codeSpans(line: string): Array<[number, number]> {
  const spans: Array<[number, number]> = [];
  const pattern = new RegExp(CODE_SPAN.source, "g");
  for (let match = pattern.exec(line); match !== null; match = pattern.exec(line)) {
    spans.push([match.index, match.index + match[0].length]);
  }
  return spans;
}

/** The reader's message split into its pictures and its words, in order.
 *  Everything that is not a picture inside this chat stays the text it was
 *  typed as, character for character: a web URL, a data URI, an escape out of
 *  the folder, a file link, and anything quoted as code or math. The words are
 *  carried as the reader's own LINES rather than re-rendered from parsed
 *  blocks, so a table keeps its pipes, a list its markers and a fence its
 *  indentation. */
export function ticketPieces(text: string): TicketPiece[] {
  const pieces: TicketPiece[] = [];
  let run: string[] = [];
  const say = (): void => {
    const said = run.join("\n").trim();
    run = [];
    if (said !== "") pieces.push({ kind: "text", text: said });
  };
  let quoting: "none" | "fence" | "math" = "none";
  for (const line of text.replace(/\r\n/g, "\n").split("\n")) {
    if (quoting !== "none") {
      run.push(line);
      if (quoting === "fence" ? FENCE_CLOSE.test(line) : MATH_FENCE.test(line)) quoting = "none";
      continue;
    }
    if (FENCE_OPEN.test(line) || MATH_FENCE.test(line)) {
      run.push(line);
      quoting = MATH_FENCE.test(line) ? "math" : "fence";
      continue;
    }
    const spans = codeSpans(line);
    const pattern = new RegExp(INLINE_IMAGE.source, "g");
    let cursor = 0;
    let head = "";
    for (let match = pattern.exec(line); match !== null; match = pattern.exec(line)) {
      const path = chatImagePath(match[2]);
      if (path === null || spans.some(([from, to]) => match.index >= from && match.index < to)) continue;
      head += line.slice(cursor, match.index);
      run.push(head);
      head = "";
      say();
      const alt = match[1].trim();
      pieces.push({ kind: "image", alt: alt === "" ? fileName(path) : alt, path });
      cursor = match.index + match[0].length;
    }
    run.push(head + line.slice(cursor));
  }
  say();
  return pieces;
}

/** The file a chat-relative path names. */
function fileName(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

/** The reader's text with images shown and chat-file links live; every other
 *  character verbatim. Exported for the ticket test. */
export function ticketBody(text: string): ReactNode[] {
  const pieces = ticketPieces(text);
  if (!pieces.some((piece) => piece.kind === "image")) {
    return [<Fragment key="t">{renderTicketText(text)}</Fragment>];
  }
  return pieces.map((piece, i) =>
    piece.kind === "image" ? (
      <ChatImage key={i} alt={piece.alt} path={piece.path} name={fileName(piece.path)} />
    ) : (
      <Fragment key={i}>{renderTicketText(piece.text)}</Fragment>
    ),
  );
}

/** Only links get inline treatment (a file the reader handed over opens);
 *  emphasis marks and the rest stay as typed. */
function renderTicketText(text: string): ReactNode[] {
  return text.split("\n").flatMap((line, i, lines) => {
    const nodes = /\[[^\]]+\]\([^)]+\)/.test(line) ? renderInline(line, undefined, undefined, undefined) : [line];
    return i < lines.length - 1 ? [...nodes, <br key={`br${i}`} />] : nodes;
  });
}

/** A message that was taken and then dropped before any machine ran it, as the
 *  transcript says so: one line under the words, and — where the host can send
 *  again — the control that does. A host with no send path passes no `onResend`
 *  and the line states the fact alone, rather than offering a button that
 *  answers a click with nothing. */
export interface TicketCancellation {
  /** What happened to the message, in one line. */
  note: string;
  /** Put the same words through the chat's send path again. */
  onResend?: () => void;
}

/** `pending` is a message the server has taken that no machine has picked up
 *  yet: the words are already the transcript's, so they read as themselves —
 *  under nothing at all. A stamp would name a time the turn did not start at,
 *  and a word for the wait is a label on a message that is simply sent; the
 *  time appears the moment a box takes it.
 *
 *  `cancelled` is the other end of that: a message the box dropped instead of
 *  running. It carries no stamp either — the time it would name is a time the
 *  turn never started at — and says what became of it in its place. */
export function OrderTicket({
  text,
  at,
  pending,
  cancelled,
  fromTemplate,
  fromTemplateAuthor,
}: {
  text: string;
  at: string;
  pending?: boolean;
  cancelled?: TicketCancellation;
  /** Named when the words are a template's brief the server opened the chat
   *  with rather than something this reader typed. The message stays theirs to
   *  edit and re-send; the caption says where it came from, and the words are
   *  not altered to say so themselves. Both this and `fromTemplateAuthor` are
   *  somebody else's text: the caller clamps them to a label before passing
   *  them, so the caption cannot grow into a second message. */
  fromTemplate?: string;
  /** Who wrote the brief. A shared template means the first prompt of this
   *  reader's chat was written by another person, and saying whose words they
   *  are is the point of the caption. */
  fromTemplateAuthor?: string;
}) {
  return (
    <div className="chat-ticket-wrap">
      <article
        className="chat-ticket"
        data-pending={pending ? "" : undefined}
        data-cancelled={cancelled ? "" : undefined}
      >
        {/* A div, not a p: a pasted image is a figure, which a paragraph may not hold. */}
        <div className="chat-ticket__body">{ticketBody(text)}</div>
      </article>
      {fromTemplate ? (
        // The title is quoted so it reads as a name the caption is carrying
        // rather than as the caption's own words.
        <p className="chat-ticket__from">
          {`From template “${fromTemplate}”`}
          {fromTemplateAuthor ? ` by ${fromTemplateAuthor}` : ""}
        </p>
      ) : null}
      {cancelled ? (
        <p className="chat-ticket__cancelled">
          <span className="chat-ticket__cancelled-note">{cancelled.note}</span>
          {cancelled.onResend ? (
            <button type="button" className="chat-ticket__resend" onClick={cancelled.onResend}>
              Resend
            </button>
          ) : null}
        </p>
      ) : null}
      {pending || cancelled ? null : <p className="chat-ticket__at chat-num">{at}</p>}
    </div>
  );
}
