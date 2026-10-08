"""The brief a saved chat starts life with, distilled from its transcript.

Saving a chat as a template asks a question nobody wants to answer by hand:
"what was this conversation for?". The answer that is always available is the
conversation itself, so the default brief is a digest of it — what the person
asked, verbatim, and enough of each reply to say what came back. The author
edits it afterwards; this is the first draft, not the last word.

Three rules make the digest safe to hand to the next reader:

* **The prompts are verbatim.** They are what the author wrote, and a template
  whose brief paraphrases the request is a template that starts the wrong chat.
* **The replies are cut short.** An assistant turn can be pages; the brief is a
  page. Each is trimmed to :data:`REPLY_MAX_CHARS`, marked where it was cut.
* **Tool rows never appear.** A command line, a file path, a query, a row of
  results — none of it says what the conversation was for, and all of it is the
  half of a transcript most likely to carry something private. Only the two
  roles that speak are read; everything else is skipped by not being matched.

The function is pure — rows in, text out, no database, no clock, no request —
so the routes that save a template and the tests that pin the shape call the
same code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

#: How much of one assistant reply the digest keeps, the cut marker included.
REPLY_MAX_CHARS = 600
#: The default ceiling on the whole digest. Roughly a long page: enough to
#: carry a real conversation's shape, short enough that a reader reads it.
DEFAULT_MAX_CHARS = 8000
#: What marks a reply that was cut, and what joins the blocks.
ELLIPSIS = "…"
BLOCK_SEPARATOR = "\n\n"
#: The labels. A digest a person edits reads as prose, so the labels are the
#: two sides of a conversation rather than field names.
ASKED_LABEL = "**Asked**"
ANSWERED_LABEL = "**Answered**"

#: The transcript row kinds the digest reads. A person's message arrives under
#: the prompt kind; an assistant's settled text arrives as a created part, the
#: streaming deltas never being persisted.
PROMPT_KIND = "prompt"
PART_CREATED_KIND = "part.created"
TEXT_PART_TYPE = "text"


class TranscriptRow(Protocol):
    """One persisted transcript row, structurally.

    A Protocol rather than an import: this module must not depend on the ORM,
    and the row a route passes (a ``chat_messages`` row) already has this shape.
    """

    @property
    def seq(self) -> int: ...
    @property
    def role(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def payload(self) -> Mapping[str, Any]: ...


def heading_for(title: str) -> str:
    """The digest's first line, naming the chat it was taken from."""
    named = title.strip() or "an untitled chat"
    return f'# Saved from the chat "{named}"'


def truncation_note(seq: int) -> str:
    """The last line of a digest that hit its ceiling, naming the row it stopped
    before so a reader knows the conversation carried on past it."""
    return f"(digest truncated at seq {seq})"


def brief_from_transcript(
    rows: Sequence[TranscriptRow], *, title: str, max_chars: int = DEFAULT_MAX_CHARS
) -> str:
    """The default brief for a template saved from the chat these rows are.

    Rows are read in ``seq`` order whatever order they arrive in — a caller
    paging a transcript backwards gets the same digest as one reading it
    forwards. An empty transcript, or one with nothing but tool traffic, yields
    the heading alone: a template with no brief is better than a brief that
    describes the wrong thing.
    """
    heading = heading_for(title)
    blocks = [
        (row.seq, block)
        for row in sorted(rows, key=lambda row: row.seq)
        if (block := _block_for(row)) is not None
    ]
    kept = _fit(heading, blocks, max_chars=max_chars)
    return BLOCK_SEPARATOR.join([heading, *kept])


def _fit(heading: str, blocks: list[tuple[int, str]], *, max_chars: int) -> list[str]:
    """As many blocks as the ceiling admits, plus the note when any were left.

    The note is part of the budget, not an overflow past it: a caller that
    sized ``max_chars`` against a column's limit gets a digest that fits that
    column even when it had to be cut.
    """
    whole = len(heading) + sum(len(BLOCK_SEPARATOR) + len(block) for _, block in blocks)
    if whole <= max_chars:
        return [block for _, block in blocks]
    used = len(heading)
    kept: list[str] = []
    for index, (seq, block) in enumerate(blocks):
        note = truncation_note(seq)
        room = used + len(BLOCK_SEPARATOR) + len(block)
        # Keeping this block is only allowed if what follows it can still be
        # marked as cut — which is the note for whichever row comes next, and
        # every note for a later row is at least as long as this one.
        if room + len(BLOCK_SEPARATOR) + _longest_note(blocks, index + 1) > max_chars:
            return [*kept, note]
        kept.append(block)
        used = room
    return kept


def _longest_note(blocks: list[tuple[int, str]], start: int) -> int:
    """The length of the longest truncation note the rows from ``start`` could
    need. Zero when nothing is left to cut."""
    return max((len(truncation_note(seq)) for seq, _ in blocks[start:]), default=0)


def _block_for(row: TranscriptRow) -> str | None:
    """One labelled block for a row the digest speaks for, or ``None``."""
    prompt = _prompt_text(row)
    if prompt is not None:
        return f"{ASKED_LABEL}\n\n{prompt}"
    reply = _reply_text(row)
    if reply is not None:
        return f"{ANSWERED_LABEL}\n\n{_clip(reply)}"
    return None


def _prompt_text(row: TranscriptRow) -> str | None:
    """A person's message, exactly as they wrote it."""
    if row.role != "user" or row.kind != PROMPT_KIND:
        return None
    text = row.payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text


def _reply_text(row: TranscriptRow) -> str | None:
    """The settled text of an assistant turn.

    A part the harness inserted for itself (``synthetic``) or told the UI to
    skip (``ignored``) is not the assistant speaking to the person, so it is
    not in the brief either.
    """
    if row.role != "assistant" or row.kind != PART_CREATED_KIND:
        return None
    part = row.payload.get("part")
    if not isinstance(part, Mapping) or part.get("type") != TEXT_PART_TYPE:
        return None
    if part.get("synthetic") or part.get("ignored"):
        return None
    text = part.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text


def _clip(text: str) -> str:
    """``text`` at most :data:`REPLY_MAX_CHARS` long, marked when it was cut."""
    if len(text) <= REPLY_MAX_CHARS:
        return text
    return text[: REPLY_MAX_CHARS - len(ELLIPSIS)] + ELLIPSIS


__all__ = [
    "ANSWERED_LABEL",
    "ASKED_LABEL",
    "DEFAULT_MAX_CHARS",
    "ELLIPSIS",
    "REPLY_MAX_CHARS",
    "TranscriptRow",
    "brief_from_transcript",
    "heading_for",
    "truncation_note",
]
