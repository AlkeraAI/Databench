"""The page cursor of a chat's attachment list, as the one string a client carries.

A cursor is ``"<place>:<node>"``: the place a page ended on and the node at
it. Both halves, because two links can hold one place and a page that named
only the place asked for everything past it.
"""

from __future__ import annotations

from uuid import UUID

from backend.services.chats.chat_service import MAX_ATTACHMENT_POSITION, AttachmentCursor


def encode_attachment_cursor(after: AttachmentCursor) -> str:
    """The cursor a page that ended on ``after`` hands back."""
    place, node = after
    return f"{place}:{node}"


def decode_attachment_cursor(cursor: str) -> AttachmentCursor:
    """The place and node a page resumes after, or ``ValueError``.

    A cursor this route did not mint is refused, never a silent read from the
    top: a client paging on a stale cursor would otherwise loop over the first
    page forever. The place must be a plain decimal from 1 up. Anything else is
    refused rather than coerced: ``int()`` accepts a sign, surrounding space,
    underscores and every decimal digit Unicode has, and it is unbounded, so a
    place past the column's ``BIGINT`` reached the driver as a bind it could
    not make. The same reading turns the old position-only cursor into a
    refusal rather than a resume past a shared place.
    """
    place, _, node = cursor.partition(":")
    if not (place.isascii() and place.isdigit()):
        raise ValueError("not a page cursor")
    position = int(place)
    if not 1 <= position <= MAX_ATTACHMENT_POSITION:
        raise ValueError("not a page cursor")
    return position, UUID(node)


__all__ = ["decode_attachment_cursor", "encode_attachment_cursor"]
