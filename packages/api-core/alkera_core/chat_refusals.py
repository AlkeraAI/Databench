"""The kinds of refusal a box puts on a chat it will not run, in one place.

The box's own sentence says what happened in an operator's words and can name
the platform's hosts, so a reader never sees it. What a reader sees is chosen
by the refusal's KIND, which the box sends beside the sentence, the server
keeps on the chat and the chat read carries. Whether a kind is final (the box
will not run the chat, so a turn the page shows running is over) is decided
here too, so the page does not have to know.

A leaf module, like ``machine_refusals``: the box, the schemas and the
server all import it, so it imports nothing of theirs.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

ChatRefusalKind = Literal[
    # Waits: the box tries again on its own and the chat may run here yet.
    "workspace_elsewhere",
    "files_unreachable",
    "transcript_unopened",
    "start_failed",
    "resume_failed",
    # Verdicts: the box will not run the chat as things stand.
    "moving",
    "not_allowed",
    "sandbox_refused",
    "workspace_unservable",
    "folder_gone",
    "files_unsaved",
]

_KINDS: Final[dict[str, ChatRefusalKind]] = {kind: kind for kind in get_args(ChatRefusalKind)}
CHAT_REFUSAL_KINDS: Final[frozenset[str]] = frozenset(_KINDS)

#: The kinds under which no box will finish a turn the chat shows running.
FINAL_REFUSAL_KINDS: Final[frozenset[ChatRefusalKind]] = frozenset(
    {
        "moving",
        "not_allowed",
        "sandbox_refused",
        "workspace_unservable",
        "folder_gone",
        "files_unsaved",
    }
)


def refusal_kind_of(raw: object) -> ChatRefusalKind | None:
    """``raw`` as a kind this server knows, or ``None``: a box newer than the
    server may send one it does not, and that must not cost the report."""
    return _KINDS.get(raw) if isinstance(raw, str) else None


def refusal_is_final(kind: str | None) -> bool:
    """Whether a refusal of ``kind`` ends a running turn. An unknown or
    missing kind does not: reading a wait as final would retire a turn that
    is still running."""
    return kind in FINAL_REFUSAL_KINDS


__all__ = [
    "CHAT_REFUSAL_KINDS",
    "FINAL_REFUSAL_KINDS",
    "ChatRefusalKind",
    "refusal_is_final",
    "refusal_kind_of",
]
