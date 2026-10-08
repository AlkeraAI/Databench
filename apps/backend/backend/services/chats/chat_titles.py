"""A chat's name, derived from the first thing said in it."""

from __future__ import annotations

#: How much of a first prompt becomes the chat's name. Long enough to tell two
#: questions about the same table apart, short enough for a rail.
TITLE_MAX_CHARS = 60

#: What a name never ends on: the tail of a sentence that has been cut short.
#: The en dash, em dash and ellipsis are spelled by codepoint — on sight they
#: are indistinguishable from the ASCII hyphen and three periods beside them.
_TITLE_TAIL = " \t\n.,:;!?-\u2013\u2014\u2026"


def title_from_prompt(text: str) -> str:
    """A chat's name, derived from the first thing said in it.

    Whitespace collapses (a pasted prompt arrives with newlines in it), the
    reading is capped at :data:`TITLE_MAX_CHARS` on a word boundary where there
    is one, and the result never ends on punctuation — a name cut mid-sentence
    should not read as a sentence. Returns ``""`` for a prompt with nothing in
    it, and the caller leaves the chat unnamed rather than naming it nothing.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) > TITLE_MAX_CHARS:
        cut = collapsed[:TITLE_MAX_CHARS]
        boundary = cut.rfind(" ")
        # A word boundary only if it keeps most of the reading; a first "word"
        # longer than the cap (a URN, a pasted id) is truncated as it is.
        collapsed = cut[:boundary] if boundary >= TITLE_MAX_CHARS // 2 else cut
    collapsed = collapsed.strip(_TITLE_TAIL)
    # Capitalised to match what a client derives when it names a chat as it
    # creates one, so the same first prompt reads the same in the list however
    # the chat was opened.
    return collapsed[:1].upper() + collapsed[1:] if collapsed else ""


__all__ = ["TITLE_MAX_CHARS", "title_from_prompt"]
